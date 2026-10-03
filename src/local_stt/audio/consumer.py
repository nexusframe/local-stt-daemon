"""The audio-consumer thread (docs/05-audio-and-vad.md §5.2, 04 §4.3).

Frames from AudioCapture and commands from the Controller share one FIFO queue. That
ordering gives the protocol for free: `begin_ptt` is enqueued before `capture.open()`, so it
precedes the stream's first frame, and `finish_ptt` is enqueued after `capture.close()` has
returned (no callback runs afterwards), so every frame of that stream precedes it.

v0.1 scope: PTT through Recorder. The Segmenter (continuous mode) arrives in task 2.2.
"""

import logging
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass

from local_stt.audio.capture import AudioFrame
from local_stt.audio.recorder import Recorder
from local_stt.events import Event, RecordingFinished, RecordingStarted
from local_stt.interfaces import Cut

log = logging.getLogger("local_stt.audio")


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


class _Stop:
    pass


Item = AudioFrame | _BeginPtt | _MaskStartSound | _FinishPtt | _Discard | _SetMaxDuration | _Stop


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
    ):
        self._items = items
        self._post = post
        self._max_duration_s = max_duration_s
        self._recorder: Recorder | None = None
        self._started = False  # RecordingStarted sent for the current recorder
        self._thread: threading.Thread | None = None

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
            case _SetMaxDuration(max_duration_s):
                self._max_duration_s = max_duration_s
            case _Stop():
                return False
        return True

    def _current(self, recording_id: int, capture_id: int) -> Recorder | None:
        rec = self._recorder
        return rec if rec is not None and rec.matches(recording_id, capture_id) else None

    def _on_frame(self, frame: AudioFrame) -> None:
        rec = self._current(frame.recording_id, frame.capture_id)
        if rec is None:
            return  # an old stream or no receiver: never enters a new recording
        if not self._started:
            self._started = True
            self._post(RecordingStarted(frame.recording_id, frame.capture_id))
        if (limit := rec.add(frame)) is not None:
            self._post(limit)
