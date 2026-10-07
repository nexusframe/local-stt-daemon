"""Model registry, download and checksum verification (docs/06-stt-engine.md §6.3).

This is the only module with Internet networking code. It is imported lazily, only by the
`local-stt models` command, so the daemon never loads it (docs/12-logging-privacy-errors.md §12.2).
"""

import hashlib
import os
import sys
import urllib.error
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
# Pinned revision: a directory model's files must come from one commit (ADR-018, task 4.5).
PARAKEET_URL = (
    "https://huggingface.co/istupakov/parakeet-tdt-0.6b-v3-onnx/resolve/"
    "8f23f0c03c8761650bdb5b40aaf3e40d2c15f1ce/"
)

WHISPER_MODELS = (
    "base-q5_1",
    "small-q5_1",
    "small-q8_0",
    "small",
    "medium-q5_0",
    "large-v3-turbo-q5_0",
)
SILERO_VAD = "silero-vad"
PARAKEET = "parakeet-tdt-0.6b-v3-int8"  # = stt.parakeet.PARAKEET_MODEL, the directory name

_CHUNK = 1 << 20
_TIMEOUT_S = 30.0
_ATTEMPTS = 3  # per file: the first request and two resumes


@dataclass(frozen=True)
class ModelFile:
    path: str  # relative to models_dir
    url: str
    sha256: str


@dataclass(frozen=True)
class Model:
    """A single file (`filename` = its path) or a directory of files (Parakeet)."""

    name: str
    filename: str  # file or directory under models_dir
    files: tuple[ModelFile, ...]


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

    def checksum(path: str) -> str:
        if path not in checksums:
            raise ValueError(f"models.sha256: no checksum for {path}")
        return checksums[path]

    def single(name: str, filename: str, url: str) -> Model:
        return Model(name, filename, (ModelFile(filename, url, checksum(filename)),))

    registry = {
        name: single(name, f"ggml-{name}.bin", WHISPER_BASE_URL + f"ggml-{name}.bin")
        for name in WHISPER_MODELS
    }
    registry[SILERO_VAD] = single(SILERO_VAD, "silero_vad.onnx", SILERO_URL)
    prefix = PARAKEET + "/"
    parts = tuple(
        ModelFile(path, PARAKEET_URL + path.removeprefix(prefix), digest)
        for path, digest in checksums.items()
        if path.startswith(prefix)
    )
    if not parts:
        raise ValueError(f"models.sha256: no checksum for {prefix}*")
    registry[PARAKEET] = Model(PARAKEET, PARAKEET, parts)
    return registry


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _valid(part: ModelFile, models_dir: Path) -> bool:
    path = models_dir / part.path
    return path.is_file() and file_sha256(path) == part.sha256


def model_status(model: Model, models_dir: Path) -> ModelStatus:
    """MISSING if any file is missing, else CORRUPT if any checksum differs."""
    if not all((models_dir / part.path).is_file() for part in model.files):
        return ModelStatus.MISSING
    if all(_valid(part, models_dir) for part in model.files):
        return ModelStatus.OK
    return ModelStatus.CORRUPT


def pull(model: Model, models_dir: Path, *, progress: TextIO | None = None) -> bool:
    """Download and verify a model. Returns False if a valid copy was already present.

    Every file goes to `<file>.part` and all are renamed only after every checksum matches,
    so an interrupted or corrupted download never leaves a file under its final name, and a
    directory model never ends up with files from different downloads mixed with missing ones.
    """
    todo = [part for part in model.files if not _valid(part, models_dir)]
    if not todo:
        return False

    staged: list[tuple[Path, Path]] = []
    try:
        for part in todo:
            target = models_dir / part.path
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".part")
            staged.append((tmp, target))
            _download(part, tmp, progress)
        for tmp, target in staged:
            os.replace(tmp, target)
    finally:
        for tmp, _ in staged:
            tmp.unlink(missing_ok=True)
    return True


def _download(part: ModelFile, tmp: Path, progress: TextIO | None) -> None:
    """Downloads `part` to `tmp`, resuming with a Range request when the connection drops.

    A dropped connection is not an error to `response.read(n)` (it just returns b""), and the
    Hugging Face CDN drops some long transfers (task 4.5: 1 of 3 tries of the 652 MB Parakeet
    encoder), so the byte count is checked against Content-Length.
    """
    digest = hashlib.sha256()
    done = total = 0
    try:
        out = tmp.open("wb")
    except OSError as e:
        raise ModelError(f"{part.path}: download failed: {e}") from e
    with out:
        for attempt in range(1, _ATTEMPTS + 1):
            headers = {"Range": f"bytes={done}-"} if done else {}
            try:
                request = urllib.request.Request(part.url, headers=headers)
                with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:
                    if done and response.status != 206:  # Range ignored: start over
                        out.seek(0)
                        out.truncate()
                        digest, done = hashlib.sha256(), 0
                    length = int(response.headers.get("Content-Length") or 0)
                    total = done + length if length else 0
                    while chunk := response.read(_CHUNK):
                        out.write(chunk)
                        digest.update(chunk)
                        done += len(chunk)
                        _report(progress, part.path, done, total)
            except urllib.error.HTTPError as e:  # an HTTP answer: retrying will not change it
                raise ModelError(f"{part.path}: download failed: {e}") from e
            except OSError as e:  # refused, reset or timed out: resume from `done`
                if attempt == _ATTEMPTS:
                    raise ModelError(f"{part.path}: download failed: {e}") from e
                _retrying(progress, part.path, done)
                continue
            if not total or done >= total:
                break
            if attempt == _ATTEMPTS:
                raise ModelError(
                    f"{part.path}: incomplete download ({done} of {total} bytes) "
                    f"after {_ATTEMPTS} attempts"
                )
            _retrying(progress, part.path, done)
    if progress is not None and progress.isatty():
        progress.write("\n")
    actual = digest.hexdigest()
    if actual != part.sha256:
        raise ModelError(f"{part.path}: checksum mismatch (expected {part.sha256}, got {actual})")


def _retrying(stream: TextIO | None, path: str, done: int) -> None:
    if stream is not None:
        stream.write(f"\n{path}: connection dropped after {done} bytes, resuming\n")


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


def _file_state(models_dir: Path, model: Model) -> str:
    paths = [models_dir / part.path for part in model.files]
    if not all(path.is_file() for path in paths):
        return "    missing"
    return f"{sum(path.stat().st_size for path in paths) / (1 << 20):7.1f} MiB"


def cmd_list(models_dir: Path) -> int:
    for model in load_registry().values():
        print(f"{model.name:26} {_file_state(models_dir, model)}  {model.filename}")
    return 0


def cmd_list_bench(
    models_dir: Path,
    results: Mapping[str, "ModelResult"],
    *,
    current: str,
    config_label: str,
    bench_dir: Path,
) -> int:
    """`models list --bench` (task 3.1): STT models with their latest benchmark results."""
    from local_stt.bench.report import config_label as measured_as

    registry = load_registry()
    stt_models = (*WHISPER_MODELS, PARAKEET)
    names = [*stt_models, *sorted(set(results) - set(stt_models))]
    print(
        f"  {'MODEL':25} {'FILE':>11}  {'WER %':>6} {'p90 s':>6} {'RAM MB':>7}  "
        f"{'MEASURED AS':22} RUN"
    )
    for name in names:
        mark = "*" if name == current else " "
        state = _file_state(models_dir, registry[name]) if name in registry else ""
        r = results.get(name)
        if r is None:
            print(f"{mark} {name:25} {state:>11}  {'—':>6} {'—':>6} {'—':>7}  {'not measured':22}")
            continue
        s = r.stats
        p90 = "—" if s.p90_text_ready_s is None else f"{s.p90_text_ready_s:.2f}"
        rss = "—" if s.peak_rss_mb is None else f"{s.peak_rss_mb:.0f}"
        measured = measured_as(s) + ("" if r.exact else " !")
        run = f"{r.date} {r.corpus}" + ("" if r.whole_corpus else " stage 1")
        print(
            f"{mark} {name:25} {state:>11}  {100 * s.wer_mean:6.1f} {p90:>6} {rss:>7}  "
            f"{measured:22} {run}"
        )
    if not results:
        print(f"\nno benchmark results in {bench_dir} (run: local-stt bench)")
        return 0
    print(
        f"\n* the selected engine's model; ! measured in another configuration than the config file"
        f" ({config_label})."
        "\nWER: mean over repeats, whole corpus (stage 1: medium group only); p90: text_ready,"
        "\nmedium group; RAM: peak server RSS (whisper-server or engine-server). Corpus A = your"
        "\nrecordings, B = the public interim corpus: results from different corpora or stages"
        "\nare not comparable."
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
        print(f"{model.name:26} {status.value}")
    return 1 if failed else 0
