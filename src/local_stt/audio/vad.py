"""Silero VAD through onnxruntime (docs/05-audio-and-vad.md §5.4) and the PTT trim (§5.3).

Task 2.1 (`SileroVad`) and task 2.6 (`VadTrimmer`, VAD in place of the RMS gate when
`vad.enabled`) were brought forward from v0.2 to v0.1 after the acceptance measurement in
docs/acceptance-v0.1.md: room noise is 10-20 dB above any usable RMS threshold (user decision
2026-10-03). The Segmenter (continuous mode, task 2.2) uses `SileroVad` too.
"""

import logging
import math
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

import numpy as np
import onnxruntime as ort
from numpy.typing import NDArray

from local_stt.config import Config, VadConfig

SAMPLE_RATE = 16000
FRAME_SAMPLES = 512
CONTEXT_SAMPLES = 64  # the final samples of the previous frame, prepended to each input
_SR = np.array(SAMPLE_RATE, dtype=np.int64)

log = logging.getLogger("local_stt.audio")


class Vad(Protocol):
    def reset(self) -> None: ...

    def __call__(self, frame: NDArray[np.float32]) -> float: ...


class SileroVad:
    """One stateful ONNX session: frames must come in order, `reset()` between streams."""

    def __init__(self, model_path: Path):
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1  # VAD must not compete with whisper.cpp for cores
        options.inter_op_num_threads = 1
        self._session = ort.InferenceSession(
            str(model_path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros(CONTEXT_SAMPLES, dtype=np.float32)

    def __call__(self, frame: NDArray[np.float32]) -> float:
        """Speech probability of one 512-sample frame (32 ms)."""
        if frame.shape != (FRAME_SAMPLES,):
            raise ValueError(f"expected {FRAME_SAMPLES} samples, got shape {frame.shape}")
        x = np.concatenate([self._context, frame.astype(np.float32, copy=False)])[np.newaxis]
        probability, self._state = self._session.run(
            None, {"input": x, "state": self._state, "sr": _SR}
        )
        self._context = x[0, -CONTEXT_SAMPLES:]
        return float(probability[0, 0])


def speech_frames(
    probabilities: Sequence[float], start_threshold: float, end_threshold: float
) -> tuple[int, int] | None:
    """Half-open frame range from the first frame with `p >= start_threshold` to the last
    frame with `p >= end_threshold` after it (the Segmenter's hysteresis, 05 §5.5), or None
    when no frame reaches `start_threshold`."""
    first = next((i for i, p in enumerate(probabilities) if p >= start_threshold), None)
    if first is None:
        return None
    last = max(i for i in range(first, len(probabilities)) if probabilities[i] >= end_threshold)
    return first, last + 1


class VadTrimmer:
    """The PTT speech gate with VAD (05 §5.3): a recording → its speech with `speech_pad_ms`
    on both sides, or None for no speech.

    Owned by PipelineWorker, with its own session (an ONNX session is stateful). `update()`
    runs on the Controller thread at IDLE (04 §4.6) while the worker may be trimming, so the
    session and settings are swapped under a lock.
    """

    def __init__(self, config: Config, load: Callable[[Path], Vad] = SileroVad):
        self._load = load
        self._lock = threading.Lock()
        self._vad: Vad | None = None
        self._path: Path | None = None
        self._config: VadConfig = config.vad
        self.update(config)

    @property
    def active(self) -> bool:
        """False with `vad.enabled = false` or without a loaded model: the RMS gate applies."""
        with self._lock:
            return self._vad is not None

    def update(self, config: Config) -> None:
        """Applies `vad.*` and `stt.models_dir`; reloads the model only if its path changed."""
        path = config.vad_model_path
        with self._lock:
            vad, current = self._vad, self._path
        if not config.vad.enabled:
            vad, path_loaded = None, current
        elif vad is None or path != current:
            try:
                vad, path_loaded = self._load(path), path
            except Exception as e:  # onnxruntime raises its own types; keep the daemon up
                log.error("cannot load the VAD model %s: %s; using the RMS gate", path, e)
                vad, path_loaded = None, None
        else:
            path_loaded = current
        with self._lock:
            self._vad, self._path, self._config = vad, path_loaded, config.vad

    def trim(self, audio: NDArray[np.float32]) -> NDArray[np.float32] | None:
        with self._lock:
            vad, config = self._vad, self._config
            if vad is None:
                raise RuntimeError("VAD is not active")
            probabilities = _probabilities(vad, audio)
        frames = speech_frames(probabilities, config.start_threshold, config.end_threshold)
        if frames is None:
            return None
        pad = config.speech_pad_ms * SAMPLE_RATE // 1000
        start = max(0, frames[0] * FRAME_SAMPLES - pad)
        end = min(len(audio), frames[1] * FRAME_SAMPLES + pad)
        return audio[start:end]


def _probabilities(vad: Vad, audio: NDArray[np.float32]) -> list[float]:
    vad.reset()
    count = math.ceil(len(audio) / FRAME_SAMPLES)
    padded = np.zeros(count * FRAME_SAMPLES, dtype=np.float32)  # last frame: zeros
    padded[: len(audio)] = audio
    return [vad(padded[i * FRAME_SAMPLES : (i + 1) * FRAME_SAMPLES]) for i in range(count)]
