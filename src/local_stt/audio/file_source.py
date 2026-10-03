"""`FileAudioSource`: a WAV file played in real time in place of the microphone.

Used by the E2E tests (docs/14-tests.md §14.3) and, from v0.2, by `bench --soak`. It has
the `AudioCapture` surface the daemon uses (`open`/`close`/`device`, 04 §4.3): every `open()`
plays the file from its start, as 512-sample frames paced by `time.monotonic()`, with the
identifiers given at `open()`. After the end of the file it sends silence until `close()`,
like a microphone in a quiet room, or starts the file again with `loop=True`.
"""

import threading
import time
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from local_stt.audio.capture import FRAME_SAMPLES, SAMPLE_RATE, AudioFrame, FrameSink
from local_stt.audio.wav import wav_bytes_to_float32
from local_stt.interfaces import AudioOpenError

FRAME_S = FRAME_SAMPLES / SAMPLE_RATE


class FileAudioSource:
    def __init__(self, frames: FrameSink, path: Path, *, loop: bool = False):
        audio, rate = wav_bytes_to_float32(path.read_bytes())
        if rate != SAMPLE_RATE:
            raise ValueError(f"{path}: {rate} Hz, expected {SAMPLE_RATE} Hz")
        padding = -len(audio) % FRAME_SAMPLES  # the last frame is completed with silence
        self._audio: NDArray[np.float32] = np.concatenate(
            [audio, np.zeros(padding, dtype=np.float32)]
        )
        self._frames = frames
        self.path = path
        self.device = str(path)  # replaced by a reload of audio.device; not used
        self.loop = loop
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def is_open(self) -> bool:
        return self._thread is not None

    def open(self, recording_id: int, capture_id: int) -> None:
        if self._thread is not None:
            raise AudioOpenError("stream is already open")
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._play,
            args=(recording_id, capture_id, time.monotonic()),
            name="file-audio-source",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        """Returns after the last frame was put, so no frame arrives afterwards."""
        thread, self._thread = self._thread, None
        if thread is None:
            return
        self._stop.set()
        thread.join()

    def _play(self, recording_id: int, capture_id: int, t0: float) -> None:
        count = len(self._audio) // FRAME_SAMPLES
        silence = np.zeros(FRAME_SAMPLES, dtype=np.float32)
        index = 0
        while True:
            started_at = t0 + index * FRAME_S
            # A frame is delivered once all of its samples have been "captured".
            if self._stop.wait(max(0.0, started_at + FRAME_S - time.monotonic())):
                return
            position = index % count if self.loop and count else index
            if position < count:
                start = position * FRAME_SAMPLES
                samples = self._audio[start : start + FRAME_SAMPLES].copy()
            else:
                samples = silence.copy()
            self._frames.put_nowait(AudioFrame(recording_id, capture_id, started_at, samples))
            index += 1
