"""`bench --soak`: continuous mode under load (docs/13-benchmark.md §13.4 stage 3, task 2.9).

The `long/` recording is played in a loop, in real time, through the daemon's own chain:
Controller → AudioConsumer with the Segmenter and its Silero session → PipelineWorker with
the real TextProcessor → a temporary `whisper-server`. Only the edges are replaced: the
microphone by `FileAudioSource`, the injector by one that enters nothing, sounds and
notifications by no-ops. The run measures what 13 §13.5 decides on (RTF, the queue trend,
CPU frequency), the VAD's CPU share (N4), and where the Segmenter cut the recording.
"""

import concurrent.futures
import json
import logging
import math
import os
import queue
import re
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from local_stt import events as ev
from local_stt.audio.capture import SAMPLE_RATE
from local_stt.audio.consumer import AudioConsumer
from local_stt.audio.file_source import FileAudioSource
from local_stt.audio.wav import wav_bytes_to_float32
from local_stt.bench.runner import (
    SystemSampler,
    concurrency_error,
    model_path,
    process_cpu_seconds,
    process_peak_rss_mb,
    system_info,
)
from local_stt.cancellation import CancellationToken
from local_stt.config import Config
from local_stt.controller import Controller
from local_stt.interfaces import EngineHealth, HotkeyProblem, InjectResult, Sound
from local_stt.pipeline import PipelineWorker
from local_stt.stt import whisper_server as ws
from local_stt.stt.parakeet import PARAKEET_MODEL, TemporaryParakeetServer
from local_stt.text.processor import DefaultTextProcessor

log = logging.getLogger("local_stt.bench")

DEFAULT_DURATION_S = 600.0
SAMPLE_INTERVAL_S = 1.0
DRAIN_TIMEOUT_S = 300.0  # after the stop: the queue must empty within this
# 13 §13.5
MAX_RTF = 0.5
TAIL_S = 300.0  # the queue trend is measured over the final 5 min
MAX_QUEUE_SLOPE = 0.05  # s of queued audio per minute
MAX_FREQ_DROP = 0.30
FREQ_WINDOW_S = 60.0  # "sustained": a 60 s moving average, against the first minute
# 13 §13.3
CUT_TOLERANCE_S = 0.040
FILE_EDGE_S = 0.5  # cuts this close to the loop joint are not Segmenter cuts
# 05 §5.4
VAD_CPU_BUDGET = 0.05  # N4: share of one core


# --- the daemon's edges ------------------------------------------------------------------


class NullInjector:
    """Enters nothing; reports the text as injected so the job completes normally."""

    def inject(self, text: str, *, cancel: CancellationToken) -> InjectResult:
        return InjectResult(True, "none", len(text), None, False, None)


class _Quiet:
    """Feedback, lifecycle and reload target of a daemon nobody sees."""

    def play(self, sound: Sound) -> float | None:
        return None

    def notify(self, key: str, title: str, body: str = "", *, informational: bool = False) -> None:
        log.info("notification: %s", title)

    def shutdown(self, *, x11_alive: bool) -> None:
        pass

    def apply_live(self, config: Config) -> None:
        pass

    def apply_at_idle(self, config: Config) -> list[HotkeyProblem]:
        return []

    def restart_server(self, config: Config) -> None:
        pass

    def use_server(self, config: Config) -> None:
        pass


# --- reference words and cut positions (13 §13.3) ------------------------------------------


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    text: str


def load_words(path: Path) -> tuple[list[Word], bool]:
    """Words of the `long/` recording and whether they were manually verified.

    Either a verified reference (`[{"word", "start", "end"}, ...]`, 13 §13.3) or, unverified,
    the `whisper-cli -ojf` output for the recording, combined from tokens into words.
    """
    doc = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(doc, dict) and "transcription" in doc:
        return words_from_whisper_json(doc), False
    return [Word(float(w["start"]), float(w["end"]), w["word"]) for w in doc], True


def words_from_whisper_json(doc: dict[str, Any]) -> list[Word]:
    """Tokens → words: a token starting with a space starts a word; special tokens
    (`[_BEG_]`, `[_TT_…]`) are skipped. Times are the tokens' `offsets` in ms."""
    words: list[Word] = []
    for segment in doc["transcription"]:
        for token in segment.get("tokens", []):
            text = token["text"]
            if text.startswith("[_") or not text.strip():
                continue
            start, end = token["offsets"]["from"] / 1000, token["offsets"]["to"] / 1000
            if text.startswith(" ") or not words:
                words.append(Word(start, end, text.strip()))
            else:
                last = words[-1]
                words[-1] = Word(last.start, max(last.end, end), last.text + text)
    return words


def incorrect_cuts(cuts: Sequence[float], words: Sequence[Word]) -> list[float]:
    """Cuts inside a word's interior: `start + 40 ms < t < end - 40 ms` (13 §13.3)."""
    return [
        t
        for t in cuts
        if any(w.start + CUT_TOLERANCE_S < t < w.end - CUT_TOLERANCE_S for w in words)
    ]


def locate_end(segment: NDArray[np.float32], audio: NDArray[np.float32]) -> float | None:
    """Where in the looped file a segment ends (seconds), found by its final samples, which
    the source copies unchanged; None for digital silence or an ambiguous match."""
    tail = segment[-64:]
    if len(tail) < 64 or not tail.any():
        return None
    candidates = np.flatnonzero(audio == tail[-1])
    matches = [i for i in candidates if i >= 63 and np.array_equal(audio[i - 63 : i + 1], tail)]
    return (matches[0] + 1) / SAMPLE_RATE if len(matches) == 1 else None


# --- trends -----------------------------------------------------------------------------


def slope_per_minute(times_s: Sequence[float], values: Sequence[float]) -> float:
    if len(times_s) < 2:
        return 0.0
    return float(np.polyfit(np.asarray(times_s) / 60, np.asarray(values), 1)[0])


def sustained_freq_drop(freqs_mhz: Sequence[float], window: int) -> float | None:
    """Largest drop of the `window`-sample moving average below the first window's mean."""
    if len(freqs_mhz) < 2 * window:
        return None
    series = np.asarray(freqs_mhz, dtype=np.float64)
    baseline = series[:window].mean()
    moving = np.convolve(series, np.ones(window) / window, mode="valid")
    return float(max(0.0, 1 - moving.min() / baseline))


def thread_cpu_seconds(native_id: int) -> float:
    stat = Path(f"/proc/self/task/{native_id}/stat").read_text()
    fields = stat[stat.rindex(")") + 2 :].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


# --- the run ----------------------------------------------------------------------------


@dataclass
class _Recorder:
    """Collects what the daemon publishes and the segments the consumer emits."""

    jobs: list[dict[str, Any]] = field(default_factory=list)
    segments: list[ev.SegmentReady] = field(default_factory=list)
    stopped_by: list[str] = field(default_factory=list)

    def publish(self, message: dict[str, Any]) -> None:
        if message["event"] == "job":
            self.jobs.append(message)


def _status(controller: Controller) -> dict[str, Any]:
    reply: concurrent.futures.Future[dict[str, Any]] = concurrent.futures.Future()
    controller.events.put(ev.StatusRequested(reply))
    status: dict[str, Any] = reply.result(10)["status"]
    return status


def _drained(status: dict[str, Any]) -> bool:
    pipeline = status["pipeline"]
    return bool(status["mode"] == "IDLE" and not pipeline["queued"] and not pipeline["busy"])


def run_soak(
    long_wav: Path,
    out_dir: Path,
    *,
    model: str,
    threads: int,
    audio_ctx: int,
    models_dir: Path,
    duration_s: float = DEFAULT_DURATION_S,
    words_path: Path | None = None,
    allow_concurrent: bool = False,
    on_progress: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Runs the soak test; writes and returns the result document."""
    error = None if allow_concurrent else concurrency_error()
    if error:
        raise RuntimeError(error)
    audio, rate = wav_bytes_to_float32(long_wav.read_bytes())
    if rate != SAMPLE_RATE:
        raise ValueError(f"{long_wav}: {rate} Hz, expected {SAMPLE_RATE} Hz")
    words, verified = load_words(words_path) if words_path else ([], False)

    parakeet = model == PARAKEET_MODEL  # task 4.8 follow-up: N3 for the default engine
    if parakeet:
        audio_ctx = 0  # Parakeet has no audio_ctx (13 §13.4)
    base = Config()
    config = replace(
        base,
        stt=replace(
            base.stt,
            engine="parakeet" if parakeet else "whisper-server",
            model=base.stt.model if parakeet else model,
            threads=threads,
            audio_ctx=audio_ctx,
            models_dir=models_dir,
        ),
        logging=replace(base.logging, timings=False),
    )
    server: ws.TemporaryWhisperServer
    if parakeet:
        server = TemporaryParakeetServer(model_path(model, models_dir), threads=threads)
    else:
        server = ws.TemporaryWhisperServer(
            config.stt.model_path, model=model, threads=threads, audio_ctx=audio_ctx
        )
    server.start()
    assert server.engine is not None and server.pid is not None
    recorder = _Recorder()
    frames: queue.SimpleQueue[Any] = queue.SimpleQueue()
    built: list[Controller] = []  # the consumer and pipeline post to it once it exists

    def post(event: ev.Event) -> None:
        if isinstance(event, ev.SegmentReady):
            recorder.segments.append(event)
        built[0].events.put(event)

    source = FileAudioSource(frames, long_wav, loop=True)
    consumer = AudioConsumer(frames, post, max_duration_s=config.ptt.max_duration_s)
    consumer.update_vad(config)
    if not consumer.continuous_available:
        server.stop()
        raise RuntimeError(f"VAD model unavailable: {config.vad_model_path}")
    pipeline = PipelineWorker(
        engine=server.engine,
        processor=DefaultTextProcessor(config),
        injector=NullInjector(),
        post=post,
        report_connection_failure=lambda: None,
        config=config,
    )
    quiet = _Quiet()
    controller = Controller(
        config,
        capture=source,
        consumer=consumer,
        pipeline=pipeline,
        feedback=quiet,
        lifecycle=quiet,
        reload_target=quiet,
        load_config=lambda: (config, []),
        on_publish=recorder.publish,
    )
    built.append(controller)
    controller_thread = threading.Thread(target=controller.run, name="controller", daemon=True)
    consumer.start()
    pipeline.start()
    controller_thread.start()
    timeline: list[dict[str, float]] = []
    try:
        controller.events.put(ev.EngineStateChanged(EngineHealth.READY))
        started: concurrent.futures.Future[dict[str, Any]] = concurrent.futures.Future()
        controller.events.put(ev.ContinuousToggle(started))
        if not started.result(10).get("ok"):
            raise RuntimeError(f"continuous mode did not start: {started.result()}")
        consumer_thread = consumer._thread
        assert consumer_thread is not None and consumer_thread.native_id is not None
        cpu0 = (process_cpu_seconds(server.pid), thread_cpu_seconds(consumer_thread.native_id))
        t0 = time.monotonic()
        with SystemSampler(SAMPLE_INTERVAL_S) as sampler:
            while (elapsed := time.monotonic() - t0) < duration_s:
                status = _status(controller)
                timeline.append(
                    {
                        "t": elapsed,
                        "queued_audio_s": status["pipeline"]["queued_audio_s"],
                        "busy": float(status["pipeline"]["busy"]),
                    }
                )
                if status["mode"] != "CONTINUOUS":
                    recorder.stopped_by.append(status["state"])
                    break
                if len(timeline) % 60 == 0:
                    on_progress(
                        f"{elapsed / 60:.0f}/{duration_s / 60:.0f} min: "
                        f"{len(recorder.jobs)} jobs, "
                        f"queue {status['pipeline']['queued_audio_s']:.1f} s"
                    )
                time.sleep(max(0.0, t0 + len(timeline) * SAMPLE_INTERVAL_S - time.monotonic()))
            wall = time.monotonic() - t0
            cpu1 = (process_cpu_seconds(server.pid), thread_cpu_seconds(consumer_thread.native_id))
            freqs = sampler.freqs_mhz
            system = sampler.summary()
        if not recorder.stopped_by:
            controller.events.put(ev.ContinuousToggle())
        deadline = time.monotonic() + DRAIN_TIMEOUT_S
        while not _drained(_status(controller)):
            if time.monotonic() > deadline:
                log.warning("the queue did not drain within %.0f s", DRAIN_TIMEOUT_S)
                break
            time.sleep(0.5)
        peak_rss = process_peak_rss_mb(server.pid)
    finally:
        controller.events.put(ev.ShutdownRequested())
        controller_thread.join(10)
        consumer.stop()
        pipeline.stop()
        server.stop()

    result = _summarize(
        recorder,
        timeline,
        audio,
        words,
        verified,
        wall=wall,
        server_cpu_s=cpu1[0] - cpu0[0],
        vad_cpu_s=cpu1[1] - cpu0[1],
        freqs=freqs,
        system=system,
        peak_rss_mb=peak_rss,
    )
    info = system_info(long_wav.parent.parent)
    result["config"] = {
        "engine": config.stt.engine,
        "model": model,
        "threads": threads,
        "audio_ctx": audio_ctx,
        "duration_s": duration_s,
        "long_wav": str(long_wav),
        "file_s": len(audio) / SAMPLE_RATE,
        "words": str(words_path) if words_path else None,
        "words_verified": verified,
    }
    keys = ("timestamp", "cpu", "governor", "platform_profile", "power_source")
    result["system"] = {k: info[k] for k in keys}
    out_dir.mkdir(parents=True, exist_ok=True)
    power = f"{info['power_source'] or 'unknown'}-{info['platform_profile'] or 'unknown'}"
    name = f"soak-{model}-t{threads}-ctx{audio_ctx}-{power}.json"
    path = out_dir / re.sub(r"[^A-Za-z0-9._-]", "_", name)
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    result["path"] = str(path)
    return result


def _summarize(
    recorder: _Recorder,
    timeline: list[dict[str, float]],
    audio: NDArray[np.float32],
    words: list[Word],
    verified: bool,
    *,
    wall: float,
    server_cpu_s: float,
    vad_cpu_s: float,
    freqs: list[float],
    system: dict[str, float | None],
    peak_rss_mb: float,
) -> dict[str, Any]:
    done = [j for j in recorder.jobs if j["processing_s"] is not None and j["audio_s"]]
    audio_s = sum(j["audio_s"] for j in done)
    rtf = sum(j["processing_s"] for j in done) / audio_s if audio_s else None
    tail = [p for p in timeline if p["t"] >= (timeline[-1]["t"] - TAIL_S if timeline else 0)]
    slope = slope_per_minute([p["t"] for p in tail], [p["queued_audio_s"] for p in tail])
    drop = sustained_freq_drop(freqs, int(FREQ_WINDOW_S / SAMPLE_INTERVAL_S))
    cuts = [e.segment.cut for e in recorder.segments]

    file_s = len(audio) / SAMPLE_RATE
    located = [locate_end(e.segment.samples, audio) for e in recorder.segments]
    internal = [t for t in located if t is not None and FILE_EDGE_S < t < file_s - FILE_EDGE_S]
    wrong = incorrect_cuts(internal, words) if words else []

    verdict = {
        "rtf": rtf is not None and rtf <= MAX_RTF,
        "queue_trend": slope <= MAX_QUEUE_SLOPE,
        "cpu_frequency": drop is None or drop <= MAX_FREQ_DROP,
        "completed": not recorder.stopped_by,
    }
    return {
        "verdict": {**verdict, "pass": all(verdict.values())},
        "wall_s": wall,
        "jobs": len(recorder.jobs),
        "jobs_by_result": {
            r: sum(1 for j in recorder.jobs if j["result"] == r)
            for r in sorted({j["result"] for j in recorder.jobs})
        },
        "audio_transcribed_s": audio_s,
        "rtf": rtf,
        "queue": {
            "max_s": max((p["queued_audio_s"] for p in timeline), default=0.0),
            "tail_slope_s_per_min": slope,
        },
        "segments": {
            "count": len(cuts),
            "by_cut": {c: cuts.count(c) for c in sorted(set(cuts))},
            "max_length_pct": 100 * cuts.count("max_length") / len(cuts) if cuts else None,
        },
        "cuts": {
            "internal": len(internal),
            "unlocated": sum(1 for t in located if t is None),
            "incorrect": len(wrong) if words else None,
            "incorrect_pct": 100 * len(wrong) / len(internal) if words and internal else None,
            "incorrect_at_s": [round(t, 3) for t in wrong],
            "reference_verified": verified if words else None,
            "short_words": sum(1 for w in words if w.end - w.start < 2 * CUT_TOLERANCE_S),
        },
        "cpu": {
            "server_cores": server_cpu_s / wall if wall else None,
            "vad_core_share": vad_cpu_s / wall if wall else None,
            "vad_within_n4": wall > 0 and vad_cpu_s / wall <= VAD_CPU_BUDGET,
            "freq_drop": drop,
        },
        "system_sampled": system,
        "server_peak_rss_mb": peak_rss_mb,
        "stopped_by": recorder.stopped_by,
        "timeline": timeline,
    }


def format_summary(result: dict[str, Any]) -> str:
    """A short text summary for the terminal."""
    c, v = result["config"], result["verdict"]

    def fmt(x: float | None, spec: str) -> str:
        return "-" if x is None or (isinstance(x, float) and math.isnan(x)) else format(x, spec)

    cuts = result["cuts"]
    reference = (
        "no reference"
        if cuts["incorrect"] is None
        else f"{cuts['incorrect']}/{cuts['internal']} incorrect"
        + ("" if cuts["reference_verified"] else " (unverified reference)")
    )
    lines = [
        f"soak {c['model']} t={c['threads']} audio_ctx={c['audio_ctx']} "
        f"on {result['system']['power_source']} ({result['system'].get('platform_profile')}): "
        f"{'PASS' if v['pass'] else 'FAIL'}",
        f"  RTF {fmt(result['rtf'], '.2f')} (≤ {MAX_RTF})  "
        f"queue max {result['queue']['max_s']:.1f} s, "
        f"tail slope {result['queue']['tail_slope_s_per_min']:+.3f} s/min (≤ {MAX_QUEUE_SLOPE})",
        f"  CPU frequency drop {fmt(result['cpu']['freq_drop'], '.0%')} (≤ {MAX_FREQ_DROP:.0%}), "
        f"temp max {fmt(result['system_sampled']['temp_max_c'], '.0f')} °C",
        f"  VAD {fmt(result['cpu']['vad_core_share'], '.1%')} of a core "
        f"(N4 ≤ {VAD_CPU_BUDGET:.0%}), "
        f"server {fmt(result['cpu']['server_cores'], '.2f')} cores, "
        f"peak RSS {fmt(result['server_peak_rss_mb'], '.0f')} MB",
        f"  {result['jobs']} jobs {result['jobs_by_result']}, "
        f"segments {result['segments']['by_cut']}, "
        f"cuts: {reference}",
    ]
    if result["stopped_by"]:
        lines.append(f"  stopped early: {result['stopped_by']}")
    return "\n".join(lines)
