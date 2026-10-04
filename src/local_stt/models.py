"""Model registry, download and checksum verification (docs/06-stt-engine.md §6.3).

This is the only module with Internet networking code. It is imported lazily, only by the
`local-stt models` command, so the daemon never loads it (docs/12-logging-privacy-errors.md §12.2).
"""

import hashlib
import os
import sys
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

if TYPE_CHECKING:
    from local_stt.bench.report import ModelResult

WHISPER_BASE_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
SILERO_URL = "https://github.com/snakers4/silero-vad/raw/v6.2.1/src/silero_vad/data/silero_vad.onnx"

WHISPER_MODELS = (
    "base-q5_1",
    "small-q5_1",
    "small-q8_0",
    "small",
    "medium-q5_0",
    "large-v3-turbo-q5_0",
)
SILERO_VAD = "silero-vad"

_CHUNK = 1 << 20
_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class Model:
    name: str
    filename: str
    url: str
    sha256: str


class ModelStatus(Enum):
    MISSING = "missing"
    OK = "ok"
    CORRUPT = "checksum mismatch"


class ModelError(Exception):
    """Download or verification failure with a user-facing message."""


def parse_checksums(text: str) -> dict[str, str]:
    """Parse sha256sum-format lines into {filename: sha256}."""
    checksums: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        digest, sep, filename = line.partition("  ")
        if not sep or len(digest) != 64 or not filename:
            raise ValueError(f"models.sha256:{lineno}: invalid line: {raw!r}")
        checksums[filename] = digest.lower()
    return checksums


def load_registry() -> dict[str, Model]:
    text = resources.files("local_stt").joinpath("models.sha256").read_text(encoding="utf-8")
    checksums = parse_checksums(text)

    entries = [
        (name, f"ggml-{name}.bin", WHISPER_BASE_URL + f"ggml-{name}.bin") for name in WHISPER_MODELS
    ]
    entries.append((SILERO_VAD, "silero_vad.onnx", SILERO_URL))

    registry: dict[str, Model] = {}
    for name, filename, url in entries:
        if filename not in checksums:
            raise ValueError(f"models.sha256: no checksum for {filename}")
        registry[name] = Model(name, filename, url, checksums[filename])
    return registry


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def model_status(model: Model, models_dir: Path) -> ModelStatus:
    path = models_dir / model.filename
    if not path.is_file():
        return ModelStatus.MISSING
    return ModelStatus.OK if file_sha256(path) == model.sha256 else ModelStatus.CORRUPT


def pull(model: Model, models_dir: Path, *, progress: TextIO | None = None) -> bool:
    """Download and verify a model. Returns False if a valid copy was already present.

    The download goes to `<file>.part` and is renamed only after the checksum matches,
    so an interrupted or corrupted download never leaves a file under the final name.
    """
    target = models_dir / model.filename
    if target.is_file() and file_sha256(target) == model.sha256:
        return False

    models_dir.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    digest = hashlib.sha256()
    try:
        with (
            urllib.request.urlopen(model.url, timeout=_TIMEOUT_S) as response,
            part.open("wb") as out,
        ):
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            while chunk := response.read(_CHUNK):
                out.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                _report(progress, model.filename, done, total)
        if progress is not None and progress.isatty():
            progress.write("\n")
        actual = digest.hexdigest()
        if actual != model.sha256:
            raise ModelError(
                f"{model.filename}: checksum mismatch (expected {model.sha256}, got {actual})"
            )
        os.replace(part, target)
    except OSError as e:
        raise ModelError(f"{model.filename}: download failed: {e}") from e
    finally:
        part.unlink(missing_ok=True)
    return True


def _report(stream: TextIO | None, filename: str, done: int, total: int) -> None:
    if stream is None or not stream.isatty():
        return
    mib = done / (1 << 20)
    if total:
        stream.write(f"\r{filename}: {mib:.0f}/{total / (1 << 20):.0f} MiB")
    else:
        stream.write(f"\r{filename}: {mib:.0f} MiB")
    stream.flush()


# --- CLI handlers (docs/10-cli-ipc-status.md §10.1) ---


def _file_state(models_dir: Path, filename: str) -> str:
    path = models_dir / filename
    return f"{path.stat().st_size / (1 << 20):7.1f} MiB" if path.is_file() else "    missing"


def cmd_list(models_dir: Path) -> int:
    for model in load_registry().values():
        print(f"{model.name:22} {_file_state(models_dir, model.filename)}  {model.filename}")
    return 0


def cmd_list_bench(
    models_dir: Path,
    results: Mapping[str, "ModelResult"],
    *,
    current: str,
    config_label: str,
    bench_dir: Path,
) -> int:
    """`models list --bench` (task 3.1): Whisper models with their latest benchmark results."""
    from local_stt.bench.report import config_label as measured_as

    registry = load_registry()
    names = [*WHISPER_MODELS, *sorted(set(results) - set(WHISPER_MODELS))]
    print(
        f"  {'MODEL':22} {'FILE':>11}  {'WER %':>6} {'p90 s':>6} {'RAM MB':>7}  "
        f"{'MEASURED AS':22} RUN"
    )
    for name in names:
        mark = "*" if name == current else " "
        state = _file_state(models_dir, registry[name].filename) if name in registry else ""
        r = results.get(name)
        if r is None:
            print(f"{mark} {name:22} {state:>11}  {'—':>6} {'—':>6} {'—':>7}  {'not measured':22}")
            continue
        s = r.stats
        p90 = "—" if s.p90_text_ready_s is None else f"{s.p90_text_ready_s:.2f}"
        rss = "—" if s.peak_rss_mb is None else f"{s.peak_rss_mb:.0f}"
        measured = measured_as(s) + ("" if r.exact else " !")
        run = f"{r.date} {r.corpus}" + ("" if r.whole_corpus else " stage 1")
        print(
            f"{mark} {name:22} {state:>11}  {100 * s.wer_mean:6.1f} {p90:>6} {rss:>7}  "
            f"{measured:22} {run}"
        )
    if not results:
        print(f"\nno benchmark results in {bench_dir} (run: local-stt bench)")
        return 0
    print(
        f"\n* stt.model; ! measured in another configuration than the config file ({config_label})."
        "\nWER: mean over repeats, whole corpus (stage 1: medium group only); p90: text_ready,"
        "\nmedium group; RAM: peak whisper-server RSS. Corpus A = your recordings, B = the public"
        "\ninterim corpus: results from different corpora or stages are not comparable."
        f"\nRuns: {bench_dir}"
    )
    return 0


def cmd_pull(name: str, models_dir: Path) -> int:
    registry = load_registry()
    model = registry.get(name)
    if model is None:
        print(f"unknown model {name!r}; available: {', '.join(registry)}", file=sys.stderr)
        return 2
    try:
        downloaded = pull(model, models_dir, progress=sys.stderr)
    except ModelError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"{model.filename}: {'downloaded, checksum OK' if downloaded else 'already present'}")
    return 0


def cmd_verify(models_dir: Path) -> int:
    failed = False
    for model in load_registry().values():
        status = model_status(model, models_dir)
        failed |= status is ModelStatus.CORRUPT
        print(f"{model.name:22} {status.value}")
    return 1 if failed else 0
