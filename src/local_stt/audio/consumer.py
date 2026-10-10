"""The audio-consumer thread (docs/05-audio-and-vad.md §5.2, 04 §4.3).

Frames from AudioCapture and commands from the Controller share one FIFO queue. That
ordering gives the protocol for free: `begin_ptt` is enqueued before `capture.open()`, so it
precedes the stream's first frame, and `finish_ptt` is enqueued after `capture.close()` has
returned (no callback runs afterwards), so every frame of that stream precedes it.

PTT goes through Recorder; continuous mode (task 2.3a) through the Segmenter, which has its
own Silero session (05 §5.4). The session is loaded on the Controller thread by
`update_vad()` (at startup and at IDLE, 04 §4.6) and handed over through the queue.
"""

import logging
import math
import queue
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from local_stt.audio.capture import SAMPLE_RATE, AudioFrame
from local_stt.audio.recorder import Recorder
from local_stt.audio.segmenter import (
    Segmenter,
    SegmenterOutput,
    SpeculationRetracted,
    SpeculativeSegment,
    SpeechEnd,
    SpeechStart,
)
from local_stt.audio.vad import SileroVad, Vad, VadModel
from local_stt.config import Config, VadConfig
from local_stt.events import (
    Event,
    FlushDone,
    FlushPurpose,
    MicrophoneSilent,
    RecordingFinished,
    RecordingStarted,
    SegmentReady,
    SpeculativeReady,
    SpeechEnded,
    SpeechStarted,
)
from local_stt.events import (
    SpeculationRetracted as SpeculationRetractedEvent,
)
from local_stt.interfaces import Cut

log = logging.getLogger("local_stt.audio")

# 05 §5.6
OVERFLOW_WARNING_INTERVAL_S = 5.0
OVERLOAD_WINDOW_S = 60.0
OVERLOAD_OVERFLOWS = 20  # more than this per minute: the CPU cannot keep up
SILENCE_DBFS = -80.0
SILENCE_S = 5.0


@dataclass(frozen=True)
class _BeginPtt:
    recording_id: int
    capture_id: int


@dataclass(frozen=True)
class _MaskStartSound:
    recording_id: int
    capture_id: int
    until: float


@dataclass(frozen=True)
class _FinishPtt:
    recording_id: int
    capture_id: int
    operation_id: int
    ended_at: float
    cut: Cut


@dataclass(frozen=True)
class _Discard:
    recording_id: int
    capture_id: int


@dataclass(frozen=True)
class _SetMaxDuration:
    max_duration_s: float


@dataclass(frozen=True)
class _ResetContinuous:
    recording_id: int
    capture_id: int
    speculative_ms: int = 0


@dataclass(frozen=True)
class _Flush:
    recording_id: int
    capture_id: int
    operation_id: int
    purpose: FlushPurpose
    at: float


@dataclass(frozen=True)
class _SetVad:
    vad: Vad | None
    config: VadConfig


class _Stop:
    pass


Item = (
    AudioFrame
    | _BeginPtt
    | _MaskStartSound
    | _FinishPtt
    | _Discard
    | _SetMaxDuration
    | _ResetContinuous
    | _Flush
    | _SetVad
    | _Stop
)


class AudioConsumer:
    """Implements `interfaces.AudioConsumerControl`.

    `queue` must be the same SimpleQueue the AudioCapture writes frames into.
    """

    def __init__(
        self,
        items: "queue.SimpleQueue[Item]",
        post: Callable[[Event], None],
        *,
        max_duration_s: float,
        load_vad: Callable[[Path], Vad] = SileroVad,
    ):
        self._items = items
        self._post = post
        self._max_duration_s = max_duration_s
        self._recorder: Recorder | None = None
        self._started = False  # RecordingStarted sent for the current recorder
        self._thread: threading.Thread | None = None
        # Controller thread: the model and whether continuous mode can start.
        self._vad_model = VadModel(load_vad)
        self._continuous_available = False
        # Consumer thread: the Segmenter and the stream it is fed from.
        self._segmenter: Segmenter | None = None
        self._stream: tuple[int, int] | None = None  # (recording_id, capture_id)
        self._silent_since: float | None = None  # digital silence in the continuous stream
        self._silence_reported: int | None = None  # session already reported as muted
        # Overflows (05 §5.6): the total is read by the Controller for status.
        self._overflows = 0
        self._unreported = 0
        self._last_overflow_warning: float | None = None
        self._recent_overflows: deque[float] = deque()
        self._last_overload_warning: float | None = None

    # --- AudioConsumerControl (called by the Controller) ---------------------------------

    def begin_ptt(self, recording_id: int, capture_id: int) -> None:
        self._items.put(_BeginPtt(recording_id, capture_id))

    def mask_start_sound(self, recording_id: int, capture_id: int, until: float) -> None:
        self._items.put(_MaskStartSound(recording_id, capture_id, until))

    def finish_ptt(
        self, recording_id: int, capture_id: int, operation_id: int, ended_at: float, cut: Cut
    ) -> None:
        self._items.put(_FinishPtt(recording_id, capture_id, operation_id, ended_at, cut))

    def discard(self, recording_id: int, capture_id: int) -> None:
        self._items.put(_Discard(recording_id, capture_id))

    def set_max_duration(self, max_duration_s: float) -> None:
        """Live reload of `ptt.max_duration_s`; applies to recordings begun after it, in
        command order (a recording already begun keeps its limit)."""
        self._items.put(_SetMaxDuration(max_duration_s))

    @property
    def continuous_available(self) -> bool:
        return self._continuous_available

    @property
    def overflows(self) -> int:
        return self._overflows

    def update_vad(self, config: Config) -> None:
        """Applies `vad.*` and `stt.models_dir` (at startup and at IDLE); the model is loaded
        here, on the caller's thread, and reloaded only when its path changes."""
        vad = self._vad_model.update(config)
        if config.vad.enabled and vad is None:
            log.error("VAD unavailable; continuous dictation cannot start")
        self._continuous_available = vad is not None
        self._items.put(_SetVad(vad, config.vad))

    def reset_continuous(
        self, recording_id: int, capture_id: int, *, speculative_ms: int = 0
    ) -> None:
        """`speculative_ms` > 0: speculative cuts for a conversation session (task 6.4)."""
        self._items.put(_ResetContinuous(recording_id, capture_id, speculative_ms))

    def flush(
        self,
        recording_id: int,
        capture_id: int,
        operation_id: int,
        purpose: FlushPurpose,
        at: float,
    ) -> None:
        self._items.put(_Flush(recording_id, capture_id, operation_id, purpose, at))

    # --- lifecycle ------------------------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, name="audio-consumer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._items.put(_Stop())
        if self._thread is not None:
            self._thread.join()

    def run(self) -> None:
        while self.handle(self._items.get()):
            pass

    # --- consumer thread ------------------------------------------------------------------

    def handle(self, item: Item) -> bool:
        """Processes one queue item; returns False on stop."""
        match item:
            case AudioFrame():
                self._on_frame(item)
            case _BeginPtt(rid, cid):
                if self._recorder is not None:
                    log.debug("begin_ptt %d/%d replaces an unfinished recording", rid, cid)
                self._recorder = Recorder(rid, cid, max_duration_s=self._max_duration_s)
                self._started = False
            case _MaskStartSound(rid, cid, until):
                if (rec := self._current(rid, cid)) is not None:
                    rec.mask_start_sound(until)
            case _FinishPtt(rid, cid, operation_id, ended_at, cut):
                if (rec := self._current(rid, cid)) is None:
                    log.debug("finish_ptt for a recording that is gone: %d/%d", rid, cid)
                else:
                    self._recorder = None
                    clip = rec.end(ended_at)
                    self._post(RecordingFinished(rid, cid, operation_id, clip, cut))
            case _Discard(rid, cid):
                if self._current(rid, cid) is not None:
                    self._recorder = None
                if self._stream == (rid, cid):
                    self._stream = None
            case _SetMaxDuration(max_duration_s):
                self._max_duration_s = max_duration_s
            case _ResetContinuous(rid, cid, speculative_ms):
                if self._segmenter is None:
                    log.error("reset_continuous %d/%d without a VAD model", rid, cid)
                    self._stream = None
                else:
                    self._segmenter.reset(session_id=rid, speculative_ms=speculative_ms)
                    self._stream = (rid, cid)
                    self._silent_since = None
            case _Flush(rid, cid, operation_id, purpose, at):
                self._on_flush(rid, cid, operation_id, purpose, at)
            case _SetVad(vad, config):
                self._segmenter = Segmenter(config, vad) if vad is not None else None
                self._stream = None
            case _Stop():
                return False
        return True

    def _current(self, recording_id: int, capture_id: int) -> Recorder | None:
        rec = self._recorder
        return rec if rec is not None and rec.matches(recording_id, capture_id) else None

    def _on_frame(self, frame: AudioFrame) -> None:
        if frame.overflow:
            self._count_overflow(frame.started_at)
        ids = (frame.recording_id, frame.capture_id)
        if self._stream == ids and self._segmenter is not None:
            self._check_silence(frame)
            self._post_outputs(self._segmenter.add(frame), *ids)
            return
        rec = self._current(frame.recording_id, frame.capture_id)
        if rec is None:
            return  # an old stream or no receiver: never enters a new recording
        if not self._started:
            self._started = True
            self._post(RecordingStarted(frame.recording_id, frame.capture_id))
        if (limit := rec.add(frame)) is not None:
            self._post(limit)

    def _on_flush(
        self, rid: int, cid: int, operation_id: int, purpose: FlushPurpose, at: float
    ) -> None:
        """Every flush is confirmed exactly once, even for a stream that never opened or is
        gone: the Controller waits for `FlushDone` (04 §4.3, *Stop(flush)*)."""
        if self._stream == (rid, cid) and self._segmenter is not None:
            self._post_outputs(self._segmenter.flush(at), rid, cid, operation_id)
            self._stream = None  # a reconnect continues with reset_continuous(new capture_id)
        self._post(FlushDone(rid, cid, operation_id, purpose))

    def _post_outputs(
        self,
        outputs: list[SegmenterOutput],
        rid: int,
        cid: int,
        operation_id: int | None = None,
    ) -> None:
        for out in outputs:
            if isinstance(out, SpeechStart):
                log.debug("VAD speech started")
                self._post(SpeechStarted(rid, cid, out.at, out.confirmed_at))
            elif isinstance(out, SpeechEnd):
                log.debug("VAD speech ended")
                self._post(SpeechEnded(rid, cid, out.start, out.end))
            elif isinstance(out, SpeculativeSegment):
                log.debug("VAD speculative cut seq=%d", out.segment.seq)
                self._post(SpeculativeReady(rid, cid, out.segment, out.speech_end))
            elif isinstance(out, SpeculationRetracted):
                log.debug("VAD speculative cut retracted")
                self._post(SpeculationRetractedEvent(rid, cid))
            else:
                log.debug(
                    "VAD segment seq=%d speech_ms=%d cut=%s audio=%.2fs",
                    out.seq,
                    out.speech_ms,
                    out.cut,
                    len(out.samples) / SAMPLE_RATE,
                )
                self._post(SegmentReady(rid, cid, out, operation_id))

    def _count_overflow(self, at: float) -> None:
        """05 §5.6: a WARNING with the count at most every 5 s; another one when more than
        20 overflows fall within a minute (CPU overload)."""
        self._overflows += 1
        self._unreported += 1
        last = self._last_overflow_warning
        if last is None or at - last >= OVERFLOW_WARNING_INTERVAL_S:
            log.warning("microphone input overflow: %d frame(s) lost", self._unreported)
            self._unreported = 0
            self._last_overflow_warning = at
        recent = self._recent_overflows
        recent.append(at)
        while recent and at - recent[0] > OVERLOAD_WINDOW_S:
            recent.popleft()
        last_overload = self._last_overload_warning
        if len(recent) > OVERLOAD_OVERFLOWS and (
            last_overload is None or at - last_overload >= OVERLOAD_WINDOW_S
        ):
            log.warning(
                "%d input overflows in the last minute: the CPU cannot keep up", len(recent)
            )
            self._last_overload_warning = at

    def _check_silence(self, frame: AudioFrame) -> None:
        """05 §5.6: RMS below -80 dBFS for 5 s → MicrophoneSilent, once per session."""
        rid, cid = frame.recording_id, frame.capture_id
        if self._silence_reported == rid:
            return
        rms = math.sqrt(float(np.mean(np.square(frame.samples, dtype=np.float64))))
        if rms > 10 ** (SILENCE_DBFS / 20):
            self._silent_since = None
            return
        if self._silent_since is None:
            self._silent_since = frame.started_at
        frame_end = frame.started_at + len(frame.samples) / SAMPLE_RATE
        if frame_end - self._silent_since >= SILENCE_S:
            log.warning("microphone appears to be muted (digital silence for %.0f s)", SILENCE_S)
            self._silence_reported = rid
            self._post(MicrophoneSilent(rid, cid))
