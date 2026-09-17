"""`local-stt bench`: model matrix on temporary servers (docs/13-benchmark.md §13.4).

Stage 1 (`--quick`): every model x threads x audio_ctx on the medium group only; a model is
eliminated for speed only if p50 text_ready_s > 6 s in all of its configurations.
Stage 2: surviving models on the whole corpus, medium included, on one server per configuration
in one fixed interleaved order (results depend on request history, 06 §6.7), plus beam search
(-bs 5) for the two best models by stage-1 WER in the same order.
Sanity: `whisper-bench -t 4` per model (raw encoder time).

Every measurement is appended to <out>/results.jsonl as soon as it is taken, so an interrupted run
continues with `--resume <out>`. Hypotheses are stored: the corpus is user-provided test content
(12 §12.2).
"""

import itertools
import json
import math
import os
import platform
import re
import subprocess
import sys
import threading
import time
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from local_stt.audio.wav import SAMPLE_RATE, wav_bytes_to_float32
from local_stt.bench import report, wer
from local_stt.stt import whisper_server as ws

GROUPS = ("short", "medium", "long_utt", "difficult")
DEFAULT_MODELS = (
    "base-q5_1",
    "small-q5_1",
    "small-q8_0",
    "small",
    "medium-q5_0",
    "large-v3-turbo-q5_0",
)
CONTROL_MODEL = "base-q5_1"  # measured as a control, never a production candidate
BEAM_TOP_N = 2
BEAM_SIZE = 5
REQUEST_TIMEOUT_S = 600.0
RMS_GATE_DBFS = -50.0  # ptt.silence_rms_dbfs default (09)
BENCH_DIR = ws.DATA_DIR / "bench"


@dataclass(frozen=True)
class BenchConfig:
    model: str
    threads: int
    audio_ctx: int  # 0 = full window; fixed value with full-window fallback (06 §6.7)
    beam_size: int = -1

    @property
    def key(self) -> str:
        return f"{self.model}|t{self.threads}|ctx{self.audio_ctx}|bs{self.beam_size}"


@dataclass(frozen=True)
class Item:
    group: str
    stem: str
    path: Path
    text: str


def load_dataset(root: Path, groups: Iterable[str] = GROUPS) -> list[Item]:
    items = []
    for group in groups:
        for wav in sorted((root / group).glob("*.wav")):
            txt = wav.with_suffix(".txt")
            if txt.is_file():
                items.append(Item(group, wav.stem, wav, txt.read_text(encoding="utf-8").strip()))
    return items


def interleave(items: Iterable[Item]) -> list[Item]:
    """Round-robin over groups (in first-seen order), keeping the file order within each group."""
    by_group: dict[str, list[Item]] = {}
    for it in items:
        by_group.setdefault(it.group, []).append(it)
    rounds = itertools.zip_longest(*by_group.values())
    return [it for round_ in rounds for it in round_ if it is not None]


def has_speech_rms(
    samples: NDArray[np.float32], threshold_dbfs: float = RMS_GATE_DBFS, window_ms: int = 100
) -> bool:
    """v0.1 PTT silence gate (05 §5.3): any 100 ms window above the threshold.

    TODO(1.6): move to pipeline.py and reuse it here, so the benchmark measures the same code.
    """
    window = SAMPLE_RATE * window_ms // 1000
    usable = len(samples) // window * window
    if usable == 0:
        return False
    rms = np.sqrt(np.mean(samples[:usable].reshape(-1, window) ** 2, axis=1))
    return bool(np.any(20 * np.log10(rms + 1e-12) > threshold_dbfs))


# --- process and system measurements (13 §13.3) ---


def process_cpu_seconds(pid: int) -> float:
    stat = Path(f"/proc/{pid}/stat").read_text()
    fields = stat[stat.rindex(")") + 2 :].split()  # comm may contain spaces
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


def process_peak_rss_mb(pid: int) -> float:
    match = re.search(r"^VmHWM:\s+(\d+) kB", Path(f"/proc/{pid}/status").read_text(), re.M)
    return int(match.group(1)) / 1024 if match else math.nan


class SystemSampler:
    """Samples package temperature and CPU frequencies once per second in a thread."""

    def __init__(self, interval_s: float = 1.0):
        self._interval_s = interval_s
        self._temps: list[float] = []
        self._freqs: list[float] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        zones = Path("/sys/class/thermal").glob("thermal_zone*")
        self._zone = next(
            (z for z in zones if (z / "type").read_text().strip() == "x86_pkg_temp"), None
        )
        self._freq_files = sorted(
            Path("/sys/devices/system/cpu").glob("cpu[0-9]*/cpufreq/scaling_cur_freq")
        )

    def __enter__(self) -> "SystemSampler":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join()

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._zone is not None:
                self._temps.append(int((self._zone / "temp").read_text()) / 1000)
            freqs = [int(f.read_text()) / 1000 for f in self._freq_files]
            if freqs:
                self._freqs.append(sum(freqs) / len(freqs))
            self._stop.wait(self._interval_s)

    def summary(self) -> dict[str, float | None]:
        return {
            "temp_max_c": max(self._temps) if self._temps else None,
            "temp_mean_c": sum(self._temps) / len(self._temps) if self._temps else None,
            "freq_mean_mhz": sum(self._freqs) / len(self._freqs) if self._freqs else None,
            "freq_min_mhz": min(self._freqs) if self._freqs else None,
        }


def system_info(dataset: Path) -> dict[str, Any]:
    def read(path: str) -> str | None:
        try:
            return Path(path).read_text().strip()
        except OSError:
            return None

    cpu = re.search(r"^model name\s*:\s*(.+)$", read("/proc/cpuinfo") or "", re.M)
    ac = [read(str(p)) for p in Path("/sys/class/power_supply").glob("AC*/online")]
    return {
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        "cpu": cpu.group(1) if cpu else None,
        "governor": read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"),
        "power_source": ("AC" if "1" in ac else "battery") if ac else None,
        "kernel": platform.release(),
        "whisper_cpp": read(str(ws.DATA_DIR / "bin/.whisper-tag")),
        "dataset": str(dataset),
        "dataset_is_public_interim": (dataset / "SOURCE.md").is_file(),
        "decoding": {
            "temperature": 0.0,
            "temperature_inc": 0.2,
            "language": "pl",
            "prompt": None,
            "rms_gate_dbfs": RMS_GATE_DBFS,
            "nice": 5,
        },
    }


# --- results file ---


class Results:
    def __init__(self, out_dir: Path):
        self.path = out_dir / "results.jsonl"
        self.lines: list[dict[str, Any]] = []
        self._done: set[tuple[Any, ...]] = set()
        if self.path.is_file():
            with self.path.open(encoding="utf-8") as f:
                for raw in f:
                    if raw.strip():
                        self._remember(json.loads(raw))

    @staticmethod
    def _file_key(stage: int, config_key: str, group: str, stem: str, rep: int) -> tuple[Any, ...]:
        return (stage, config_key, group, stem, rep)

    def _remember(self, line: dict[str, Any]) -> None:
        self.lines.append(line)
        if line.get("type") == "file":
            self._done.add(
                self._file_key(
                    line["stage"], line["config_key"], line["group"], line["stem"], line["rep"]
                )
            )

    def done(self, stage: int, cfg: BenchConfig, item: Item, rep: int) -> bool:
        return self._file_key(stage, cfg.key, item.group, item.stem, rep) in self._done

    def append(self, line: dict[str, Any]) -> None:
        self._remember(line)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")


def _log(message: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {message}", file=sys.stderr, flush=True)


def run_config(
    stage: int,
    cfg: BenchConfig,
    items: list[Item],
    repeats: int,
    results: Results,
    models_dir: Path,
) -> None:
    todo = [
        (rep, it)
        for rep in range(1, repeats + 1)
        for it in items
        if not results.done(stage, cfg, it, rep)
    ]
    if not todo:
        return
    audio = {it.path: wav_bytes_to_float32(it.path.read_bytes())[0] for it in items}
    server = ws.TemporaryWhisperServer(
        models_dir / f"ggml-{cfg.model}.bin",
        model=cfg.model,
        threads=cfg.threads,
        beam_size=cfg.beam_size,
        audio_ctx=cfg.audio_ctx,
    )
    started = time.monotonic()
    engine = server.start()
    try:
        pid = server.pid
        assert pid is not None
        startup_s = time.monotonic() - started
        warmup = audio[items[0].path]
        engine.transcribe(
            warmup, sample_rate=SAMPLE_RATE, language="pl", prompt=None, timeout_s=REQUEST_TIMEOUT_S
        )
        cpu0, wall0 = process_cpu_seconds(pid), time.monotonic()
        with SystemSampler() as sampler:
            for n, (rep, it) in enumerate(todo, start=1):
                progress = f"rep {rep}/{repeats} {it.group}/{it.stem} ({n}/{len(todo)})"
                _log(f"stage {stage} {cfg.key} {progress}")
                results.append(_measure(stage, cfg, it, rep, audio[it.path], engine))
            cpu_s, wall_s = process_cpu_seconds(pid) - cpu0, time.monotonic() - wall0
            peak_rss = process_peak_rss_mb(pid)
        results.append(
            {
                "type": "config",
                "stage": stage,
                "config_key": cfg.key,
                "config": asdict(cfg),
                "startup_s": startup_s,
                "cpu_util": cpu_s / wall_s if wall_s else None,
                "peak_rss_mb": peak_rss,
                "wall_s": wall_s,
                **sampler.summary(),
            }
        )
    finally:
        server.stop()


def _measure(
    stage: int,
    cfg: BenchConfig,
    it: Item,
    rep: int,
    samples: NDArray[np.float32],
    engine: ws.WhisperServerEngine,
) -> dict[str, Any]:
    line: dict[str, Any] = {
        "type": "file",
        "stage": stage,
        "config_key": cfg.key,
        "config": asdict(cfg),
        "group": it.group,
        "stem": it.stem,
        "rep": rep,
        "audio_s": len(samples) / SAMPLE_RATE,
        "reference": it.text,
    }
    t0 = time.monotonic()
    speech = has_speech_rms(samples)
    gate_s = time.monotonic() - t0
    if not speech:
        return line | {"result": "no_speech", "gate_s": gate_s, "text_ready_s": gate_s}
    try:
        transcript = engine.transcribe(
            samples,
            sample_rate=SAMPLE_RATE,
            language="pl",
            prompt=None,
            timeout_s=REQUEST_TIMEOUT_S,
        )
    except ws.EngineError as e:
        return line | {"result": "error", "error": str(e)}
    t1 = time.monotonic()
    # TODO(1.7): run TextProcessor instead of whitespace normalization once it exists.
    hypothesis = " ".join(transcript.text.split())
    text_s = time.monotonic() - t1
    counts = wer.error_counts(it.text, hypothesis)
    return line | {
        "result": "ok",
        "hypothesis": hypothesis,
        "gate_s": gate_s,
        "processing_s": transcript.processing_s,
        "text_s": text_s,
        "text_ready_s": gate_s + transcript.processing_s + text_s,
        "rtf": transcript.processing_s / transcript.audio_duration_s,
        **asdict(counts),
        "numeric_mismatch": wer.numeric_mismatch(it.text, hypothesis),
    }


def run_whisper_bench(model: str, models_dir: Path, results: Results) -> None:
    if any(
        line.get("type") == "whisper_bench" and line["model"] == model for line in results.lines
    ):
        return
    _log(f"whisper-bench {model} -t 4")
    proc = subprocess.run(
        [
            "nice",
            "-n",
            "5",
            str(ws.DATA_DIR / "bin/whisper-bench"),
            "-m",
            str(models_dir / f"ggml-{model}.bin"),
            "-t",
            "4",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    output = proc.stdout + proc.stderr
    encode = re.search(r"encode time =\s+([\d.]+) ms", output)
    results.append(
        {
            "type": "whisper_bench",
            "model": model,
            "threads": 4,
            "exit_code": proc.returncode,
            "encode_ms": float(encode.group(1)) if encode else None,
        }
    )


def service_active() -> bool:
    proc = subprocess.run(
        ["systemctl", "--user", "is-active", "local-stt-whisper.service"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() == "active"


def _configs(
    models: Iterable[str], threads: Iterable[int], audio_ctx: Iterable[int]
) -> Iterator[BenchConfig]:
    for model, t, a in itertools.product(models, threads, audio_ctx):
        yield BenchConfig(model, t, a)


def run(
    dataset: Path,
    out_dir: Path,
    *,
    models: list[str],
    threads: list[int],
    audio_ctx: list[int],
    repeats: int,
    quick: bool,
    beam: bool,
    sanity: bool,
    allow_concurrent: bool = False,
    models_dir: Path = ws.DATA_DIR / "models",
) -> int:
    if not allow_concurrent and service_active():
        print(
            "error: local-stt-whisper.service is running and would distort CPU/RAM results; run\n"
            "  systemctl --user stop local-stt-whisper local-stt\n"
            "or pass --allow-concurrent",
            file=sys.stderr,
        )
        return 1
    items = load_dataset(dataset)
    medium = [it for it in items if it.group == "medium"]
    if not medium:
        print(f"error: no medium/*.wav with .txt in {dataset}", file=sys.stderr)
        return 1
    missing = [m for m in models if not (models_dir / f"ggml-{m}.bin").is_file()]
    if missing:
        print(f"error: models not downloaded: {', '.join(missing)}", file=sys.stderr)
        return 1

    out_dir.mkdir(parents=True, exist_ok=True)
    info_path = out_dir / "system.json"
    if not info_path.is_file():
        info = system_info(dataset) | {
            "models": models,
            "threads": threads,
            "audio_ctx": audio_ctx,
            "repeats": repeats,
        }
        info_path.write_text(json.dumps(info, indent=2, ensure_ascii=False) + "\n")
    results = Results(out_dir)
    _log(f"results: {results.path}")

    for cfg in _configs(models, threads, audio_ctx):
        run_config(1, cfg, medium, repeats, results, models_dir)
    if quick:
        return 0

    stage1 = report.summarize(results.lines, stage=1)
    eliminated = report.eliminated_models(stage1, control=CONTROL_MODEL)
    survivors = [m for m in models if m not in eliminated]
    _log(f"stage 1 eliminated: {sorted(eliminated) or 'none'}")
    corpus = interleave(items)
    for cfg in _configs(survivors, threads, audio_ctx):
        run_config(2, cfg, corpus, repeats, results, models_dir)
    if beam:
        for model in report.top_models_by_wer(
            stage1, survivors, BEAM_TOP_N, exclude={CONTROL_MODEL}
        ):
            cfg = BenchConfig(model, min(threads), 0, BEAM_SIZE)
            run_config(2, cfg, corpus, repeats, results, models_dir)
    if sanity:
        for model in models:
            run_whisper_bench(model, models_dir, results)
    _log("done")
    return 0


def default_out_dir() -> Path:
    return BENCH_DIR / datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
