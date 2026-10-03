import queue
import threading

import numpy as np

from local_stt.audio.capture import FRAME_SAMPLES, SAMPLE_RATE, AudioFrame
from local_stt.audio.consumer import AudioConsumer, Item
from local_stt.events import Event, RecordingFinished, RecordingLimitReached, RecordingStarted

FRAME_S = FRAME_SAMPLES / SAMPLE_RATE
T0 = 50.0


def frame(i: int, rid: int = 1, cid: int = 1) -> AudioFrame:
    return AudioFrame(rid, cid, T0 + i * FRAME_S, np.full(FRAME_SAMPLES, i, dtype=np.float32))


class World:
    def __init__(self, max_duration_s: float = 120) -> None:
        self.items: queue.SimpleQueue[Item] = queue.SimpleQueue()
        self.events: list[Event] = []
        self.consumer = AudioConsumer(self.items, self.events.append, max_duration_s=max_duration_s)

    def drain(self) -> None:
        """Runs the consumer synchronously over everything queued so far."""
        while not self.items.empty():
            assert self.consumer.handle(self.items.get())

    def frames(self, count: int, rid: int = 1, cid: int = 1, start: int = 0) -> None:
        for i in range(start, start + count):
            self.items.put(frame(i, rid, cid))

    def finished(self) -> list[RecordingFinished]:
        return [e for e in self.events if isinstance(e, RecordingFinished)]


def test_ptt_recording_start_to_finish() -> None:
    w = World()
    w.consumer.begin_ptt(1, 1)
    w.frames(10)
    w.consumer.finish_ptt(1, 1, operation_id=7, ended_at=T0 + 10 * FRAME_S, cut="release")
    w.drain()
    assert w.events[0] == RecordingStarted(1, 1)
    (done,) = w.finished()
    assert (done.recording_id, done.capture_id, done.operation_id, done.cut) == (1, 1, 7, "release")
    assert len(done.clip.samples) == 10 * FRAME_SAMPLES
    assert len(w.events) == 2


def test_recording_started_once_on_first_frame() -> None:
    w = World()
    w.consumer.begin_ptt(1, 1)
    w.drain()
    assert w.events == []  # nothing before the first frame
    w.frames(3)
    w.drain()
    assert w.events == [RecordingStarted(1, 1)]


def test_final_frames_queued_before_finish_are_included() -> None:
    # finish_ptt is enqueued after capture.close(): the stream's last frames precede it
    # and are still in the queue when the command is issued.
    w = World()
    w.consumer.begin_ptt(1, 1)
    w.frames(5)
    w.drain()
    w.frames(5, start=5)
    w.consumer.finish_ptt(1, 1, 1, ended_at=T0 + 100, cut="release")
    w.drain()
    (done,) = w.finished()
    assert len(done.clip.samples) == 10 * FRAME_SAMPLES


def test_frames_after_ended_at_are_trimmed() -> None:
    w = World()
    w.consumer.begin_ptt(1, 1)
    w.frames(10)
    w.consumer.finish_ptt(1, 1, 1, ended_at=T0 + 5 * FRAME_S, cut="release")
    w.drain()
    (done,) = w.finished()
    assert len(done.clip.samples) == 5 * FRAME_SAMPLES


def test_mask_start_sound() -> None:
    w = World()
    w.consumer.begin_ptt(1, 1)
    w.frames(2)
    w.consumer.mask_start_sound(1, 1, until=T0 + 4 * FRAME_S)
    w.consumer.mask_start_sound(1, 2, until=T0 + 100)  # foreign capture: ignored
    w.frames(8, start=2)
    w.consumer.finish_ptt(1, 1, 1, ended_at=T0 + 100, cut="release")
    w.drain()
    (done,) = w.finished()
    assert len(done.clip.samples) == 6 * FRAME_SAMPLES
    assert done.clip.samples[0] == 4


def test_frames_from_foreign_or_old_streams_are_dropped() -> None:
    w = World()
    w.frames(3, rid=0, cid=0)  # no receiver yet
    w.consumer.begin_ptt(2, 3)
    w.frames(3, rid=2, cid=2)  # old stream of the same recording
    w.frames(3, rid=1, cid=3)  # old recording
    w.frames(4, rid=2, cid=3)
    w.consumer.finish_ptt(2, 3, 1, ended_at=T0 + 100, cut="release")
    w.drain()
    assert w.events[0] == RecordingStarted(2, 3)
    (done,) = w.finished()
    assert len(done.clip.samples) == 4 * FRAME_SAMPLES


def test_discard_old_recording_does_not_remove_new_one() -> None:
    w = World()
    w.consumer.begin_ptt(1, 1)
    w.frames(3)
    w.consumer.discard(1, 1)
    w.consumer.begin_ptt(2, 2)
    w.consumer.discard(1, 1)  # late duplicate for the old recording
    w.frames(4, rid=2, cid=2)
    w.consumer.finish_ptt(2, 2, 9, ended_at=T0 + 100, cut="release")
    w.consumer.finish_ptt(1, 1, 8, ended_at=T0 + 100, cut="release")  # gone: ignored
    w.drain()
    (done,) = w.finished()
    assert (done.recording_id, done.operation_id) == (2, 9)
    assert len(done.clip.samples) == 4 * FRAME_SAMPLES
    assert RecordingStarted(1, 1) in w.events and RecordingStarted(2, 2) in w.events


def test_discard_creates_no_clip() -> None:
    w = World()
    w.consumer.begin_ptt(1, 1)
    w.frames(3)
    w.consumer.discard(1, 1)
    w.frames(3, start=3)  # would-be stragglers
    w.drain()
    assert w.finished() == []


def test_limit_event_posted_once() -> None:
    w = World(max_duration_s=0.5)
    w.consumer.begin_ptt(1, 1)
    w.frames(40)
    w.drain()
    limits = [e for e in w.events if isinstance(e, RecordingLimitReached)]
    assert limits == [RecordingLimitReached(1, 1, ended_at=T0 + 0.5)]


def test_max_duration_reload_applies_to_next_recording() -> None:
    w = World(max_duration_s=120)
    w.consumer.begin_ptt(1, 1)
    w.consumer.set_max_duration(0.1)
    w.frames(10)
    w.drain()
    assert not any(isinstance(e, RecordingLimitReached) for e in w.events)
    w.consumer.begin_ptt(2, 2)
    w.frames(10, rid=2, cid=2)
    w.drain()
    assert any(isinstance(e, RecordingLimitReached) for e in w.events)


def test_begin_ptt_replaces_unfinished_recording() -> None:
    w = World()
    w.consumer.begin_ptt(1, 1)
    w.frames(3)
    w.consumer.begin_ptt(2, 2)
    w.frames(2, rid=2, cid=2)
    w.consumer.finish_ptt(2, 2, 1, ended_at=T0 + 100, cut="release")
    w.drain()
    (done,) = w.finished()
    assert len(done.clip.samples) == 2 * FRAME_SAMPLES


def test_thread_processes_queue_and_stops() -> None:
    events: queue.SimpleQueue[Event] = queue.SimpleQueue()
    items: queue.SimpleQueue[Item] = queue.SimpleQueue()
    consumer = AudioConsumer(items, events.put, max_duration_s=120)
    consumer.start()
    consumer.begin_ptt(1, 1)
    for i in range(3):
        items.put(frame(i))
    consumer.finish_ptt(1, 1, 1, ended_at=T0 + 100, cut="release")
    assert events.get(timeout=5) == RecordingStarted(1, 1)
    assert isinstance(events.get(timeout=5), RecordingFinished)
    consumer.stop()
    assert not any(t.name == "audio-consumer" for t in threading.enumerate())
