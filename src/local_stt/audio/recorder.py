"""PTT recording buffer (docs/05-audio-and-vad.md §5.3).

Used only in the audio-consumer thread. Masking and trimming are applied in `end()` to the
whole buffer, because `mask_start_sound` and `finish_ptt` arrive after some frames have
already been collected (04 §4.3).
"""

from typing import TYPE_CHECKING

import numpy as np
from numpy.typing import NDArray

from local_stt.events import RecordingLimitReached
from local_stt.interfaces import AudioClip

if TYPE_CHECKING:
    from local_stt.audio.capture import AudioFrame

SAMPLE_RATE = 16000


class Recorder:
    def __init__(self, recording_id: int, capture_id: int, *, max_duration_s: float):
        self.recording_id = recording_id
        self.capture_id = capture_id
        self._max_duration_s = max_duration_s
        self._frames: list[AudioFrame] = []
        self._first_at: float | None = None
        self._mask_until: float | None = None
        self._limit_reached = False

    def matches(self, recording_id: int, capture_id: int) -> bool:
        return recording_id == self.recording_id and capture_id == self.capture_id

    def mask_start_sound(self, until: float) -> None:
        """Discard samples before monotonic time `until` (end of the start sound + 80 ms)."""
        self._mask_until = until

    def add(self, frame: "AudioFrame") -> RecordingLimitReached | None:
        """Buffers a frame; returns the limit event once, when `max_duration_s` is crossed.

        Frames from another recording or stream are ignored. Frames keep being buffered
        after the limit; `end()` trims them to the limit time.
        """
        if not self.matches(frame.recording_id, frame.capture_id):
            return None
        self._frames.append(frame)
        if self._first_at is None:
            self._first_at = frame.started_at
        limit_at = self._first_at + self._max_duration_s
        frame_end = frame.started_at + len(frame.samples) / SAMPLE_RATE
        if not self._limit_reached and frame_end >= limit_at:
            self._limit_reached = True
            return RecordingLimitReached(self.recording_id, self.capture_id, ended_at=limit_at)
        return None

    def end(self, ended_at: float) -> AudioClip:
        """Joins the buffer, keeping only samples in [mask end, `ended_at`)."""
        start = self._mask_until if self._mask_until is not None else float("-inf")
        parts: list[NDArray[np.float32]] = []
        clip_start: float | None = None
        for frame in self._frames:
            n = len(frame.samples)
            lo = _sample_index(start, frame.started_at, n)
            hi = _sample_index(ended_at, frame.started_at, n)
            if hi <= lo:
                continue
            if clip_start is None:
                clip_start = frame.started_at + lo / SAMPLE_RATE
            parts.append(frame.samples[lo:hi])
        self._frames.clear()
        samples = np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)
        return AudioClip(
            samples=samples,
            sample_rate=SAMPLE_RATE,
            duration_s=len(samples) / SAMPLE_RATE,
            started_at=clip_start if clip_start is not None else ended_at,
            ended_at=ended_at,
        )


def _sample_index(t: float, frame_start: float, n: int) -> int:
    """Index of the first sample at or after time `t` within a frame, clamped to [0, n]."""
    if t == float("-inf"):
        return 0
    return min(max(round((t - frame_start) * SAMPLE_RATE), 0), n)
