"""Continuous-mode segmenter (docs/05-audio-and-vad.md §5.5).

Divides the frame stream into utterances with hysteresis: speech starts above
`start_threshold` (after `min_speech_ms` without a frame below `end_threshold`) and ends
after `min_silence_ms` of frames below `end_threshold`; frames between the thresholds change
nothing. Used only in the audio-consumer thread, with its own `SileroVad` session.

Not in the spec, decided by the user 2026-10-04: a segment with less than `min_speech_ms` of
speech is dropped on every cut, not only on `flush()`. After a `max_length` split the
remainder can hold only silence or a fragment of a word, and Whisper hallucinates on those.
"""

import math
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

import numpy as np
from numpy.typing import NDArray

from local_stt.audio.vad import FRAME_SAMPLES, SAMPLE_RATE, Vad
from local_stt.config import VadConfig
from local_stt.interfaces import AudioSegment, SegmentCut

if TYPE_CHECKING:
    from local_stt.audio.capture import AudioFrame

FRAME_MS = FRAME_SAMPLES * 1000 // SAMPLE_RATE  # 32
MIN_SPLIT_RUN_MS = 96  # shortest silence run worth splitting in (05 §5.5 rule 5)


@dataclass(frozen=True)
class SpeechStart:
    """The VAD confirmed speech (rule 2). Stream times: `at` is the start of the first speech
    frame, `confirmed_at` the end of the frame that confirmed it (task 6.1)."""

    at: float
    confirmed_at: float


@dataclass(frozen=True)
class SpeechEnd:
    """The utterance ended. Stream times of the start of its first speech frame and the end
    of its last one (`p >= end_threshold`), not of the silence that ended it (task 6.1)."""

    start: float
    end: float


@dataclass(frozen=True)
class SpeculativeSegment:
    """Conversation mode (task 6.4): `speculative_ms` of silence ended the speech so far. The
    segment is what a cut at this frame would give (the trailing pad is the silence that is
    there); its `seq` is the one that the final segment will get. `speech_end`: the stream
    time of the end of the last speech frame, as in SpeechEnd."""

    segment: AudioSegment
    speech_end: float


@dataclass(frozen=True)
class SpeculationRetracted:
    """A frame with `p >= end_threshold` came after the speculative cut (task 6.4)."""


# The audio consumer turns these into SpeechStarted / SpeechEnded / SegmentReady (task 2.3) and
# SpeculativeReady / SpeculationRetracted (task 6.4).
SegmenterOutput = SpeechStart | SpeechEnd | AudioSegment | SpeculativeSegment | SpeculationRetracted


class _State(Enum):
    SILENCE = "SILENCE"
    CANDIDATE = "CANDIDATE"
    SPEECH = "SPEECH"
    TRAILING = "TRAILING"


class Segmenter:
    def __init__(self, config: VadConfig, vad: Vad):
        self._config = config
        self._vad = vad
        self._pad = config.speech_pad_ms * SAMPLE_RATE // 1000
        self._max_samples = round(config.max_segment_s * SAMPLE_RATE)
        self._search_frames = max(1, round(config.split_search_s * SAMPLE_RATE / FRAME_SAMPLES))
        self._min_split_run = math.ceil(MIN_SPLIT_RUN_MS / FRAME_MS)
        self._session_id: int | None = None
        self._seq = 0
        # Frames before speech: the last `speech_pad_ms` of SILENCE (rule 1).
        self._preroll: deque[NDArray[np.float32]] = deque(
            maxlen=math.ceil(self._pad / FRAME_SAMPLES)
        )
        self._state = _State.SILENCE
        self._head: NDArray[np.float32]  # pre-roll samples of the current segment
        self._frames: list[NDArray[np.float32]] = []
        self._probs: list[float] = []
        self._silence_ms = 0
        # Stream time of the last emitted segment's speech end, and the pause before the
        # utterance in progress (AudioSegment.pause_before_s, task 3.4).
        self._last_speech_end: float | None = None
        self._pause: float | None = None
        self._frame_end = 0.0
        # Stream times of the utterance in progress (SpeechStart.at, SpeechEnd.end).
        self._speech_start = 0.0
        self._speech_end = 0.0
        # Speculative cuts (task 6.4): 0 = off; set for each session by reset().
        self._speculative_ms = 0
        self._speculating = False  # a speculative cut is valid (no speech frame after it)
        self._split = False  # the utterance was split at max_segment_s: no speculation
        self._reset_buffers()

    @property
    def state(self) -> str:
        return self._state.value

    def reset(self, session_id: int, *, speculative_ms: int = 0) -> None:
        """Clears buffers and the VAD state (rule 8). `seq` restarts only for a new session:
        after a reconnect the same session continues its numbering. `speculative_ms` > 0
        turns speculative cuts on for this session (conversation mode, task 6.4)."""
        self._speculative_ms = speculative_ms
        if session_id != self._session_id:
            self._session_id, self._seq = session_id, 0
        self._preroll.clear()
        self._reset_buffers()
        self._last_speech_end = None
        self._vad.reset()

    def add(self, frame: "AudioFrame") -> list[SegmenterOutput]:
        p = self._vad(frame.samples)
        frame_end = frame.started_at + len(frame.samples) / SAMPLE_RATE
        self._frame_end = frame_end
        c = self._config
        if self._state is _State.SILENCE:
            if p < c.start_threshold:
                self._preroll.append(frame.samples)
                return []
            self._state = _State.CANDIDATE
            self._append(frame.samples, p)
            return self._accept_candidate()
        if self._state is _State.CANDIDATE:
            if p < c.end_threshold:  # rule 2: a single frame below returns to SILENCE
                self._preroll.extend(self._frames)
                self._reset_buffers()
                return []
            self._append(frame.samples, p)
            return self._accept_candidate()
        # SPEECH / TRAILING (rule 3)
        self._append(frame.samples, p)
        out: list[SegmenterOutput] = []
        if p >= c.end_threshold and self._speculating:
            self._speculating = False
            out.append(SpeculationRetracted())
        if p >= c.start_threshold:
            self._silence_ms, self._state = 0, _State.SPEECH
        elif p < c.end_threshold:
            self._silence_ms += FRAME_MS
            self._state = _State.TRAILING
            if self._silence_ms >= c.min_silence_ms:
                return [*out, *self._end(frame_end, "silence")]
            if self._speculation_due():
                out += self._speculate(frame_end)
        return [*out, *self._split_if_too_long(frame_end)]

    def flush(self, at: float) -> list[SegmenterOutput]:
        """Ends the utterance in progress (rule 6); `at` is the flush request time."""
        if self._state in (_State.SPEECH, _State.TRAILING):
            return self._end(at, "flush")
        self._reset_buffers()
        return []

    # --- internals ---------------------------------------------------------------------------

    def _speculation_due(self) -> bool:
        return (
            self._speculative_ms > 0
            and not self._speculating
            and not self._split
            and self._silence_ms >= self._speculative_ms
            and self._silence_ms - FRAME_MS < self._speculative_ms  # only at the threshold
        )

    def _speculate(self, ended_at: float) -> list[SegmenterOutput]:
        """Task 6.4: the segment that a cut here would give, with the silence that is there
        as the trailing pad. Emitted once for each pause; `_seq` does not change."""
        speech = self._speech_range(len(self._frames))
        if speech is None or (speech[1] - speech[0]) * FRAME_MS < self._config.min_speech_ms:
            return []
        audio = np.concatenate([self._head, *self._frames])
        end = min(len(audio), len(self._head) + speech[1] * FRAME_SAMPLES + self._pad)
        assert self._session_id is not None, "reset(session_id) must precede add()"
        speech_ms = (speech[1] - speech[0]) * FRAME_MS
        segment = AudioSegment(
            audio[:end],
            self._session_id,
            self._seq + 1,
            ended_at,
            speech_ms,
            "silence",
            self._pause,
        )
        self._speculating = True
        return [SpeculativeSegment(segment, self._speech_end)]

    def _reset_buffers(self) -> None:
        self._speculating = False
        self._state = _State.SILENCE
        self._head = np.zeros(0, dtype=np.float32)
        self._frames = []
        self._probs = []
        self._silence_ms = 0

    def _append(self, samples: NDArray[np.float32], p: float) -> None:
        self._frames.append(samples)
        self._probs.append(p)
        if p >= self._config.end_threshold:
            self._speech_end = self._frame_end

    def _accept_candidate(self) -> list[SegmenterOutput]:
        if len(self._frames) * FRAME_MS < self._config.min_speech_ms:
            return []
        self._state = _State.SPEECH
        self._split = False
        # Every CANDIDATE frame has p >= end_threshold, so speech began with the first one.
        start = self._frame_end - len(self._frames) * FRAME_MS / 1000
        self._speech_start = start
        self._pause = None if self._last_speech_end is None else start - self._last_speech_end
        if self._pad and self._preroll:
            self._head = np.concatenate(self._preroll)[-self._pad :]
        self._preroll.clear()
        return [SpeechStart(start, self._frame_end)]

    def _speech_range(self, frames: int) -> tuple[int, int] | None:
        """Half-open range of frames[:frames] from the first to the last frame with
        `p >= end_threshold`. Starts at 0 for a segment that began in CANDIDATE."""
        speech = [i for i in range(frames) if self._probs[i] >= self._config.end_threshold]
        return (speech[0], speech[-1] + 1) if speech else None

    def _segment(
        self,
        samples: NDArray[np.float32],
        ended_at: float,
        speech: tuple[int, int],
        cut: SegmentCut,
    ) -> list[SegmenterOutput]:
        """`speech`: the speech frame range within `self._frames`."""
        speech_ms = (speech[1] - speech[0]) * FRAME_MS
        if speech_ms < self._config.min_speech_ms:
            return []
        trailing = len(self._frames) - speech[1]
        self._last_speech_end = self._frame_end - trailing * FRAME_MS / 1000
        self._seq += 1
        assert self._session_id is not None, "reset(session_id) must precede add()"
        pause, self._pause = self._pause, 0.0  # the rest of a max_length split follows at once
        reuses = self._speculating and cut != "max_length"
        return [
            AudioSegment(
                samples, self._session_id, self._seq, ended_at, speech_ms, cut, pause, reuses
            )
        ]

    def _end(self, ended_at: float, cut: SegmentCut) -> list[SegmenterOutput]:
        """Rule 4: the segment runs through the last speech frame plus `speech_pad_ms`; the
        rest of the silence is discarded, its final `speech_pad_ms` kept as the next pre-roll."""
        out: list[SegmenterOutput] = []
        audio = np.concatenate([self._head, *self._frames])
        end = 0
        speech = self._speech_range(len(self._frames))
        if speech is not None:
            end = min(len(audio), len(self._head) + speech[1] * FRAME_SAMPLES + self._pad)
            out += self._segment(audio[:end], ended_at, speech, cut)
        tail = audio[end:][-self._pad :] if self._pad else audio[:0]
        if cut == "silence" and len(tail):
            self._preroll.extend(np.array_split(tail, math.ceil(len(tail) / FRAME_SAMPLES)))
        self._reset_buffers()
        return [*out, SpeechEnd(self._speech_start, self._speech_end)]

    def _split_if_too_long(self, ended_at: float) -> list[SegmenterOutput]:
        """Rule 5: at `max_segment_s`, split in the middle of the longest silence run (at least
        96 ms) in the last `split_search_s`, else before the frame with the lowest p. The
        remainder starts a new segment; SPEECH/TRAILING and the silence counter carry on."""
        if len(self._head) + len(self._frames) * FRAME_SAMPLES < self._max_samples:
            return []
        n = len(self._frames)
        first = max(1, n - self._search_frames)
        split = self._longest_silence_middle(first, n)
        if split is None:
            split = min(range(first, n), key=lambda i: self._probs[i], default=n)
        speech = self._speech_range(split)
        out: list[SegmenterOutput] = []
        if self._speculating:  # the speculative text would miss the rest (task 6.4)
            self._speculating = False
            out.append(SpeculationRetracted())
        self._split = True
        if speech is not None:
            audio = np.concatenate([self._head, *self._frames[:split]])
            out += self._segment(audio, ended_at, speech, "max_length")
        self._head = np.zeros(0, dtype=np.float32)
        del self._frames[:split], self._probs[:split]
        return out

    def _longest_silence_middle(self, first: int, n: int) -> int | None:
        best: tuple[int, int] | None = None  # (length, start); later runs win ties
        run_start = None
        for i in range(first, n + 1):
            silent = i < n and self._probs[i] < self._config.end_threshold
            if silent and run_start is None:
                run_start = i
            elif not silent and run_start is not None:
                length = i - run_start
                if length >= self._min_split_run and (best is None or length >= best[0]):
                    best = (length, run_start)
                run_start = None
        return None if best is None else best[1] + best[0] // 2
