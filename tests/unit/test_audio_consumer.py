import queue
import threading
from pathlib import Path

import numpy as np
import pytest
from numpy.typing import NDArray
from pytest import approx

from local_stt.audio.capture import FRAME_SAMPLES, SAMPLE_RATE, AudioFrame
from local_stt.audio.consumer import AudioConsumer, Item
from local_stt.config import Config, VadConfig
from local_stt.events import (
    Event,
    FlushDone,
    MicrophoneSilent,
    RecordingFinished,
    RecordingLimitReached,
    RecordingStarted,
    SegmentReady,
    SpeechEnded,
    SpeechStarted,
)

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


# --- continuous mode (task 2.3a; 05 §5.5, 04 §4.3) -------------------------------------------

S, Q = 0.9, 0.1  # speech / silence probability


class FirstSampleVad:
    """Returns the frame's first sample as its speech probability."""

    def reset(self) -> None:
        pass

    def __call__(self, frame: NDArray[np.float32]) -> float:
        return float(frame[0])


class ContinuousWorld(World):
    def __init__(self) -> None:
        self.items = queue.SimpleQueue()
        self.events = []
        self.loaded: list[Path] = []
        self.consumer = AudioConsumer(
            self.items, self.events.append, max_duration_s=120, load_vad=self.load
        )
        self.consumer.update_vad(Config())
        self.next = 0

    def load(self, path: Path) -> FirstSampleVad:
        self.loaded.append(path)
        return FirstSampleVad()

    def speech(self, probabilities: list[float], rid: int = 1, cid: int = 1) -> None:
        for p in probabilities:
            samples = np.full(FRAME_SAMPLES, p, dtype=np.float32)
            self.items.put(AudioFrame(rid, cid, T0 + self.next * FRAME_S, samples))
            self.next += 1

    def kinds(self) -> list[str]:
        return [type(e).__name__ for e in self.events]

    def segments(self) -> list[SegmentReady]:
        return [e for e in self.events if isinstance(e, SegmentReady)]


def test_vad_availability_follows_the_config_and_the_model(
    caplog: pytest.LogCaptureFixture,
) -> None:
    w = ContinuousWorld()
    assert w.consumer.continuous_available and len(w.loaded) == 1
    w.consumer.update_vad(Config(vad=VadConfig(enabled=False)))
    assert not w.consumer.continuous_available

    def broken(path: Path) -> FirstSampleVad:
        raise RuntimeError("bad model")

    consumer = AudioConsumer(queue.SimpleQueue(), print, max_duration_s=120, load_vad=broken)
    consumer.update_vad(Config())
    assert not consumer.continuous_available
    assert "continuous dictation cannot start" in caplog.text


def test_continuous_session_emits_speech_and_segments() -> None:
    w = ContinuousWorld()
    w.consumer.reset_continuous(1, 1)
    w.speech([Q] * 5 + [S] * 10 + [Q] * 22)
    w.drain()
    assert w.kinds() == ["SpeechStarted", "SegmentReady", "SpeechEnded"]
    (ready,) = w.segments()
    assert (ready.recording_id, ready.capture_id, ready.operation_id) == (1, 1, None)
    assert (ready.segment.session_id, ready.segment.seq, ready.segment.cut) == (1, 1, "silence")
    assert all(not isinstance(e, RecordingStarted) for e in w.events)


def test_speech_events_carry_monotonic_speech_times() -> None:
    """Task 6.1: frames 5..14 are speech; it is confirmed at the 8th frame (256 ms)."""
    w = ContinuousWorld()
    w.consumer.reset_continuous(1, 1)
    w.speech([Q] * 5 + [S] * 10 + [Q] * 22)
    w.drain()
    started = next(e for e in w.events if isinstance(e, SpeechStarted))
    ended = next(e for e in w.events if isinstance(e, SpeechEnded))
    assert started == SpeechStarted(1, 1, approx(T0 + 5 * FRAME_S), approx(T0 + 13 * FRAME_S))
    assert ended == SpeechEnded(1, 1, approx(T0 + 5 * FRAME_S), approx(T0 + 15 * FRAME_S))


def test_stop_flush_emits_the_utterance_then_flush_done() -> None:
    w = ContinuousWorld()
    w.consumer.reset_continuous(1, 1)
    w.speech([S] * 10)
    w.consumer.flush(1, 1, operation_id=5, purpose="stop", at=77.0)
    w.speech([S] * 30)  # a stopped stream feeds nothing
    w.drain()
    assert w.kinds() == ["SpeechStarted", "SegmentReady", "SpeechEnded", "FlushDone"]
    (ready,) = w.segments()
    assert (ready.operation_id, ready.segment.cut, ready.segment.ended_at) == (5, "flush", 77.0)
    assert w.events[-1] == FlushDone(1, 1, 5, "stop")


def test_flush_is_confirmed_even_without_a_stream() -> None:
    w = ContinuousWorld()
    w.consumer.flush(3, 4, operation_id=9, purpose="stop", at=1.0)  # stop before open
    w.drain()
    assert w.events == [FlushDone(3, 4, 9, "stop")]


def test_reconnect_keeps_the_session_numbering_and_drops_the_old_stream() -> None:
    w = ContinuousWorld()
    w.consumer.reset_continuous(1, 1)
    w.speech([S] * 10 + [Q] * 22)
    w.consumer.flush(1, 1, operation_id=2, purpose="reconnect", at=1.0)
    w.consumer.reset_continuous(1, 2)
    w.speech([S] * 10 + [Q] * 22, cid=1)  # late frames of the lost stream
    w.speech([S] * 10 + [Q] * 22, cid=2)
    w.drain()
    assert [(e.capture_id, e.segment.seq) for e in w.segments()] == [(1, 1), (2, 2)]
    assert FlushDone(1, 1, 2, "reconnect") in w.events


def test_discard_stops_the_session_without_segments() -> None:
    w = ContinuousWorld()
    w.consumer.reset_continuous(1, 1)
    w.speech([S] * 10)
    w.consumer.discard(1, 1)
    w.speech([Q] * 30)
    w.consumer.discard(1, 1)  # repeated: nothing to do
    w.drain()
    assert w.kinds() == ["SpeechStarted"]


def test_without_vad_reset_feeds_nothing(caplog: pytest.LogCaptureFixture) -> None:
    w = ContinuousWorld()
    w.consumer.update_vad(Config(vad=VadConfig(enabled=False)))
    w.consumer.reset_continuous(1, 1)
    w.speech([S] * 40)
    w.drain()
    assert w.events == []
    assert "without a VAD model" in caplog.text


def test_new_vad_settings_apply_to_the_next_session() -> None:
    w = ContinuousWorld()
    w.consumer.update_vad(Config(vad=VadConfig(min_speech_ms=32)))
    assert len(w.loaded) == 1  # same model path: no reload
    w.consumer.reset_continuous(1, 1)
    w.speech([S])
    w.drain()
    assert w.kinds() == ["SpeechStarted"]


# --- audio errors (task 2.7; 05 §5.6) --------------------------------------------------------


def overflow_frame(at: float, rid: int = 9, cid: int = 9) -> AudioFrame:
    return AudioFrame(rid, cid, at, np.zeros(FRAME_SAMPLES, dtype=np.float32), overflow=True)


def test_overflows_are_counted_and_warned_every_5_s(caplog: pytest.LogCaptureFixture) -> None:
    w = World()
    for at in (0.0, 1.0, 2.0, 5.0, 5.5):  # frames of any stream count
        w.items.put(overflow_frame(at))
    w.drain()
    assert w.consumer.overflows == 5
    warnings = [r.getMessage() for r in caplog.records if "overflow" in r.getMessage()]
    assert warnings == [
        "microphone input overflow: 1 frame(s) lost",
        "microphone input overflow: 3 frame(s) lost",  # 1.0, 2.0, 5.0
    ]
    assert w.events == []


def test_more_than_20_overflows_a_minute_is_an_overload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    w = World()
    for i in range(21):
        w.items.put(overflow_frame(i * 2.0))  # 21 within 40 s
    w.items.put(overflow_frame(45.0))  # still overloaded: warned at most once a minute
    w.drain()
    overloads = [r for r in caplog.records if "CPU cannot keep up" in r.getMessage()]
    assert [r.getMessage() for r in overloads] == [
        "21 input overflows in the last minute: the CPU cannot keep up"
    ]


def test_slow_overflows_are_not_an_overload(caplog: pytest.LogCaptureFixture) -> None:
    w = World()
    for i in range(30):
        w.items.put(overflow_frame(i * 4.0))  # 15 a minute
    w.drain()
    assert "CPU cannot keep up" not in caplog.text


def test_digital_silence_is_reported_once_per_session(caplog: pytest.LogCaptureFixture) -> None:
    w = ContinuousWorld()
    w.consumer.reset_continuous(1, 1)
    frames_5s = int(5.0 / FRAME_S) + 1
    w.speech([0.0] * (frames_5s - 2))
    w.drain()
    assert not any(isinstance(e, MicrophoneSilent) for e in w.events)
    w.speech([0.0] * 2)
    w.drain()
    assert [e for e in w.events if isinstance(e, MicrophoneSilent)] == [MicrophoneSilent(1, 1)]
    w.consumer.reset_continuous(1, 2)  # reconnect: the same session is not reported again
    w.speech([0.0] * frames_5s * 2, cid=2)
    w.consumer.reset_continuous(2, 3)  # a new session is
    w.speech([0.0] * frames_5s, rid=2, cid=3)
    w.drain()
    assert [e for e in w.events if isinstance(e, MicrophoneSilent)] == [
        MicrophoneSilent(1, 1),
        MicrophoneSilent(2, 3),
    ]


def test_room_noise_is_not_digital_silence() -> None:
    w = ContinuousWorld()
    w.consumer.reset_continuous(1, 1)
    frames_5s = int(5.0 / FRAME_S) + 1
    w.speech([0.0] * (frames_5s - 5) + [0.0005] + [0.0] * 20)  # -66 dBFS breaks the run
    w.drain()
    assert not any(isinstance(e, MicrophoneSilent) for e in w.events)
