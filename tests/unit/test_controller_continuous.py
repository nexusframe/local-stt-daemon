"""Controller in CONTINUOUS mode (docs/04-state-machine.md §4.3, task 2.3b), one test per row."""

import dataclasses
from typing import Any

import numpy as np
import pytest

from local_stt import events as ev
from local_stt.config import Config, ContinuousConfig, VadConfig
from local_stt.controller import Controller, Mode
from local_stt.interfaces import (
    AudioSegment,
    CancelResult,
    EngineHealth,
    InjectResult,
    SegmentCut,
)

from .test_controller import (
    WHISPER,
    Consumer,
    Pipeline,
    Reload,
    World,
    make,
    reply,
    status_of,
)


@pytest.fixture
def w() -> World:
    return World()


@pytest.fixture
def c(w: World) -> Controller:
    return make(w)


def start(c: Controller, w: World) -> tuple[int, int]:
    """IDLE → CONTINUOUS with the microphone open; returns (recording_id, capture_id)."""
    c.handle(ev.ContinuousToggle())
    cont = c._cont
    assert cont is not None
    c.handle(ev.CaptureOpenDue(cont.recording_id))
    assert cont.opened
    w.calls.clear()
    return cont.recording_id, cont.capture_id


def segment(seq: int = 1, seconds: float = 2.0, cut: SegmentCut = "silence") -> AudioSegment:
    samples = np.zeros(int(seconds * 16000), dtype=np.float32)
    return AudioSegment(samples, 1, seq, ended_at=99.0, speech_ms=1500, cut=cut)


def ops(w: World, name: str) -> list[tuple[Any, ...]]:
    return [call for call in w.calls if call[0] == name]


def stop_op(c: Controller) -> int:
    assert c._cont is not None and c._cont.stop_op is not None
    return c._cont.stop_op


# --- IDLE: ContinuousToggle -----------------------------------------------------------------


def test_toggle_plays_start_resets_the_consumer_and_delays_the_open(
    c: Controller, w: World
) -> None:
    r = reply()
    c.handle(ev.ContinuousToggle(r))
    assert w.calls == [
        ("sound", "start"),
        ("consumer.reset_continuous", 1, 1, 0),
        ("schedule", 0.150, ev.CaptureOpenDue(1)),
    ]
    assert c.mode is Mode.CONTINUOUS
    assert not r.done()  # answered after the open attempt
    c.handle(ev.CaptureOpenDue(1))
    assert ("capture.open", 1, 1) in w.calls
    assert r.result() == {"ok": True}


@pytest.mark.parametrize(
    ("engine", "code"),
    [(EngineHealth.STARTING, "engine_starting"), (EngineHealth.DOWN, "engine_down")],
)
def test_toggle_without_engine_is_rejected(w: World, engine: EngineHealth, code: str) -> None:
    c = make(w, engine=engine)
    r = reply()
    c.handle(ev.ContinuousToggle(r))
    assert w.sounds() == ["error"]
    assert r.result()["error"] == code
    assert c.mode is Mode.IDLE


def test_toggle_with_vad_disabled_is_rejected(c: Controller, w: World) -> None:
    c.config = dataclasses.replace(c.config, vad=VadConfig(enabled=False))
    r = reply()
    c.handle(ev.ContinuousToggle(r))
    assert w.sounds() == ["error"]
    assert r.result()["error"] == "vad_disabled"
    assert c.mode is Mode.IDLE


def test_toggle_without_a_vad_model_is_rejected(c: Controller, w: World) -> None:
    w.vad_available = False
    r = reply()
    c.handle(ev.ContinuousToggle(r))
    assert w.sounds() == ["error"]
    assert w.notifications() == ["VAD model unavailable"]
    assert r.result()["error"] == "vad_disabled"
    assert not any(name.startswith("consumer") for name in w.names())


def test_toggle_during_ptt_is_invalid_in_mode(c: Controller, w: World) -> None:
    c.handle(ev.PttPressed(100.0))
    r = reply()
    c.handle(ev.ContinuousToggle(r))
    assert r.result()["error"] == "invalid_in_mode"
    assert c.mode is Mode.PTT_RECORDING


def test_ptt_during_continuous_is_invalid_in_mode(c: Controller, w: World) -> None:
    start(c, w)
    r = reply()
    c.handle(ev.PttPressed(100.0, r))
    assert r.result()["error"] == "invalid_in_mode"


# --- CONTINUOUS: start ----------------------------------------------------------------------


def test_open_failure_rejects_the_start(c: Controller, w: World) -> None:
    w.open_error = "no such device"
    r = reply()
    c.handle(ev.ContinuousToggle(r))
    c.handle(ev.CaptureOpenDue(1))
    assert r.result()["error"] == "audio_error"
    assert w.sounds() == ["start", "error"]
    assert ("consumer.discard", 1, 1) in w.calls
    assert c.mode is Mode.IDLE


def test_stop_before_the_open_cancels_the_start(c: Controller, w: World) -> None:
    started = reply()
    c.handle(ev.ContinuousToggle(started))
    stopped = reply()
    c.handle(ev.ContinuousToggle(stopped))
    assert started.result()["error"] == "cancelled"
    assert stopped.result() == {"ok": True}
    c.handle(ev.CaptureOpenDue(1))  # the timer fires anyway: nothing opens
    assert ops(w, "capture.open") == []
    c.handle(ev.FlushDone(1, 1, stop_op(c), "stop"))
    assert c.mode is Mode.IDLE


def test_stale_open_timer_is_ignored(c: Controller, w: World) -> None:
    start(c, w)
    c.handle(ev.CaptureOpenDue(1))  # already open
    c.handle(ev.CaptureOpenDue(99))
    assert w.calls == []


# --- CONTINUOUS: speech and segments --------------------------------------------------------


def test_speech_flag_and_listening_status(c: Controller, w: World) -> None:
    rid, cid = start(c, w)
    assert c.display_status() == "LISTENING"
    c.handle(ev.SpeechStarted(rid, cid, 10.0, 10.3))
    assert c.display_status() == "LISTENING (speech)"
    status = status_of(c)
    assert (status["mode"], status["speech"], status["audio"]["open"]) == (
        "CONTINUOUS",
        True,
        True,
    )
    c.handle(ev.SpeechEnded(rid, cid, 10.0, 12.0))
    assert c.display_status() == "LISTENING"


def test_segment_is_submitted_as_a_continuous_job(c: Controller, w: World) -> None:
    rid, cid = start(c, w)
    c.handle(ev.SegmentReady(rid, cid, segment(seq=3, cut="max_length")))
    (job,) = w.jobs
    assert (job.source, job.session_id, job.seq, job.cut, job.ended_at) == (
        "continuous",
        1,
        3,
        "max_length",
        99.0,
    )
    assert job.generation == 7
    assert c.display_status() == "LISTENING, transcribing 1"
    assert c.snapshot().queued_audio_s == 2.0


def test_segments_of_another_session_or_stream_are_ignored(c: Controller, w: World) -> None:
    rid, cid = start(c, w)
    c.handle(ev.SegmentReady(rid + 1, cid, segment()))
    c.handle(ev.SegmentReady(rid, cid + 1, segment()))
    c.handle(ev.SegmentReady(rid, cid, segment(), operation_id=42))
    assert w.jobs == []


def test_backlog_stops_dictation_once(w: World) -> None:
    c = make(w)
    c.config = dataclasses.replace(c.config, continuous=ContinuousConfig(max_backlog_s=5.0))
    rid, cid = start(c, w)
    c.handle(ev.SegmentReady(rid, cid, segment(seconds=3)))
    assert c._cont is not None and not c._cont.stopping
    c.handle(ev.SegmentReady(rid, cid, segment(seq=2, seconds=3)))
    assert w.sounds() == ["stop", "error"]
    assert w.notifications() == ["Transcription cannot keep up — dictation stopped"]
    op = stop_op(c)
    c.handle(ev.SegmentReady(rid, cid, segment(seq=3, cut="flush"), operation_id=op))
    assert len(ops(w, "consumer.flush")) == 1  # no second flush
    assert len(w.jobs) == 3  # the queue is finished, the flushed segment included
    c.handle(ev.FlushDone(rid, cid, op, "stop"))
    assert c.mode is Mode.IDLE


# --- CONTINUOUS: stop, cancel, engine DOWN --------------------------------------------------


def test_toggle_stops_with_a_flush(c: Controller, w: World) -> None:
    rid, cid = start(c, w)
    w.now = 120.0
    r = reply()
    c.handle(ev.ContinuousToggle(r))
    op = stop_op(c)
    assert w.calls == [
        ("capture.close",),
        ("consumer.flush", rid, cid, op, "stop", 120.0),
        ("sound", "stop"),
    ]
    assert r.result() == {"ok": True}
    again = reply()
    c.handle(ev.ContinuousToggle(again))  # while stopping
    assert again.result()["error"] == "invalid_in_mode"
    c.handle(ev.SegmentReady(rid, cid, segment(cut="flush"), operation_id=op))
    assert len(w.jobs) == 1
    c.handle(ev.FlushDone(rid, cid, op, "stop"))
    assert c.mode is Mode.IDLE
    c.handle(ev.SegmentReady(rid, cid, segment(), operation_id=op))  # late: ignored
    c.handle(ev.FlushDone(rid, cid, op, "stop"))
    assert len(w.jobs) == 1 and c.mode is Mode.IDLE


def test_cancel_discards_the_session_and_the_queue(c: Controller, w: World) -> None:
    started = reply()
    c.handle(ev.ContinuousToggle(started))
    w.calls.clear()
    r = reply()
    c.handle(ev.CancelRequested(r))
    assert w.calls == [
        ("capture.close",),
        ("consumer.discard", 1, 1),
        ("pipeline.cancel_all",),
        ("sound", "cancel"),
    ]
    assert started.result()["error"] == "cancelled"
    assert r.result()["ok"] is True
    assert c.mode is Mode.IDLE


def test_cancel_while_stopping_ends_at_once(c: Controller, w: World) -> None:
    rid, cid = start(c, w)
    c.handle(ev.ContinuousToggle())
    op = stop_op(c)
    c.handle(ev.CancelRequested())
    assert c.mode is Mode.IDLE
    c.handle(ev.SegmentReady(rid, cid, segment(cut="flush"), operation_id=op))
    assert w.jobs == []


def test_engine_down_stops_dictation(c: Controller, w: World) -> None:
    start(c, w)
    c.handle(ev.EngineStateChanged(EngineHealth.DOWN))
    assert ("pipeline.pause",) in w.calls
    assert len(ops(w, "consumer.flush")) == 1
    assert w.sounds() == ["stop", "error"]
    assert w.notifications() == ["STT engine stopped working — dictation stopped"]
    c.handle(ev.EngineStateChanged(EngineHealth.DOWN))  # repeated: no second flush
    assert len(ops(w, "consumer.flush")) == 1


def test_job_failure_keeps_dictating(c: Controller, w: World) -> None:
    rid, cid = start(c, w)
    c.handle(ev.SegmentReady(rid, cid, segment()))
    c.handle(ev.JobFailed(w.jobs[0].id, "continuous", 2.0, "HTTP 500"))
    assert c.mode is Mode.CONTINUOUS
    assert w.notifications() == ["Could not transcribe segment (2 s)"]


@pytest.mark.parametrize("event", [ev.ShutdownRequested(), ev.X11ConnectionLost()])
def test_shutdown_discards_the_session(c: Controller, w: World, event: ev.Event) -> None:
    start(c, w)
    c.handle(event)
    assert ("capture.close",) in w.calls
    assert ("consumer.discard", 1, 1) in w.calls
    assert c.exit_code == 0


# --- CONTINUOUS: reconnect ------------------------------------------------------------------


def lose(c: Controller, w: World) -> tuple[int, int, int]:
    """CONTINUOUS(reconnecting) after device loss; returns (rid, old cid, operation_id)."""
    rid, cid = start(c, w)
    c.handle(ev.AudioError(rid, cid, "device_lost", "stream stopped"))
    assert c._cont is not None and c._cont.reconnect_op is not None
    op = c._cont.reconnect_op
    assert w.calls == [("capture.close",), ("consumer.flush", rid, cid, op, "reconnect", 100.0)]
    w.calls.clear()
    return rid, cid, op


def test_device_loss_flushes_then_reopens(c: Controller, w: World) -> None:
    rid, cid, op = lose(c, w)
    assert c.display_status() == "LISTENING (reconnecting)"
    assert status_of(c)["reconnecting"] is True
    c.handle(ev.AudioError(rid, cid, "device_lost", "again"))  # while reconnecting: ignored
    c.handle(ev.SegmentReady(rid, cid, segment(cut="flush"), operation_id=op))
    assert len(w.jobs) == 1
    c.handle(ev.FlushDone(rid, cid, op, "reconnect"))
    assert c.mode is Mode.CONTINUOUS
    assert w.calls[-1] == ("schedule", 1.0, ev.ReconnectTick(rid, op, 1))
    w.calls.clear()
    c.handle(ev.ReconnectTick(rid, op, 1))
    new_cid = cid + 1
    assert w.calls == [
        ("consumer.reset_continuous", rid, new_cid, 0),
        ("capture.open", rid, new_cid),
    ]
    assert c.display_status() == "LISTENING, transcribing 1"  # the reconnect segment
    c.handle(ev.SegmentReady(rid, new_cid, segment(seq=2)))
    c.handle(ev.SegmentReady(rid, cid, segment(seq=9)))  # the old stream: ignored
    assert [job.seq for job in w.jobs] == [1, 2]


def test_three_failed_reopens_stop_dictation(c: Controller, w: World) -> None:
    rid, cid, op = lose(c, w)
    c.handle(ev.FlushDone(rid, cid, op, "reconnect"))
    w.open_error = "gone"
    for attempt in (1, 2):
        w.calls.clear()
        c.handle(ev.ReconnectTick(rid, op, attempt))
        assert w.calls[-1] == ("schedule", 1.0, ev.ReconnectTick(rid, op, attempt + 1))
    w.calls.clear()
    c.handle(ev.ReconnectTick(rid, op, 3))
    assert w.sounds() == ["stop", "error"]
    assert w.notifications() == ["Microphone lost — dictation stopped"]
    assert c._cont is not None
    stop = stop_op(c)
    c.handle(ev.FlushDone(rid, c._cont.capture_id, stop, "stop"))
    assert c.mode is Mode.IDLE


def test_stop_during_the_reconnect_flush_waits_for_it(c: Controller, w: World) -> None:
    rid, cid, op = lose(c, w)
    r = reply()
    c.handle(ev.ContinuousToggle(r))
    assert ops(w, "consumer.flush") == []  # the reconnect flush is still running
    c.handle(ev.SegmentReady(rid, cid, segment(cut="flush"), operation_id=op))
    c.handle(ev.FlushDone(rid, cid, op, "reconnect"))
    stop = stop_op(c)
    assert ops(w, "consumer.flush") == [("consumer.flush", rid, cid, stop, "stop", 100.0)]
    assert ops(w, "schedule") == []  # no reopen timer
    c.handle(ev.FlushDone(rid, cid, stop, "stop"))
    assert c.mode is Mode.IDLE
    assert len(w.jobs) == 1  # the reconnect segment exactly once


def test_stop_while_waiting_for_a_reopen(c: Controller, w: World) -> None:
    rid, cid, op = lose(c, w)
    c.handle(ev.FlushDone(rid, cid, op, "reconnect"))
    c.handle(ev.ContinuousToggle())
    stop = stop_op(c)
    c.handle(ev.ReconnectTick(rid, op, 1))  # blocked by stopping
    assert ops(w, "capture.open") == []
    c.handle(ev.FlushDone(rid, cid, op, "reconnect"))  # duplicate: ignored
    c.handle(ev.FlushDone(rid, cid, stop, "stop"))
    assert c.mode is Mode.IDLE


# --- reload ---------------------------------------------------------------------------------


def test_vad_reload_waits_for_idle(c: Controller, w: World) -> None:
    rid, cid = start(c, w)
    w.next_config = dataclasses.replace(WHISPER, vad=VadConfig(min_silence_ms=500))
    r = reply()
    c.handle(ev.ReloadRequested(r))
    assert r.result()["deferred"] == ["vad.min_silence_ms"]
    assert ops(w, "reload.idle") == []
    c.handle(ev.ContinuousToggle())
    c.handle(ev.FlushDone(rid, cid, stop_op(c), "stop"))
    assert len(ops(w, "reload.idle")) == 1
    assert c.config.vad.min_silence_ms == 500


def test_stale_speech_and_reconnect_events_are_ignored(c: Controller, w: World) -> None:
    rid, cid, op = lose(c, w)
    c.handle(ev.SpeechStarted(rid, cid + 5, 10.0, 10.3))
    c.handle(ev.ReconnectTick(rid, op, 1))  # the reconnect flush has not finished
    c.handle(ev.AudioError(rid, cid, "open_failed", "x"))
    assert w.calls == []
    assert status_of(c)["speech"] is False


# --- VAD events for subscribers (task 6.1; 10 §10.2) ----------------------------------------


def speech_events(published: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m for m in published if m["event"] in ("speech_start", "speech_end")]


def test_speech_start_and_end_are_published_with_an_utterance_number(w: World) -> None:
    published: list[dict[str, Any]] = []
    c = make(w)
    c._on_publish = published.append
    rid, cid = start(c, w)
    c.handle(ev.SpeechStarted(rid, cid, 10.0, 10.3))
    c.handle(ev.SpeechEnded(rid, cid, 10.0, 12.5))
    c.handle(ev.SpeechStarted(rid, cid, 14.0, 14.25))
    c.handle(ev.SpeechEnded(rid, cid, 14.0, 15.0))
    assert speech_events(published) == [
        {"event": "speech_start", "utt": 1, "session_id": rid, "t": 10.3, "t_start": 10.0},
        {"event": "speech_end", "utt": 1, "session_id": rid, "t_start": 10.0, "t_end": 12.5},
        {"event": "speech_start", "utt": 2, "session_id": rid, "t": 14.25, "t_start": 14.0},
        {"event": "speech_end", "utt": 2, "session_id": rid, "t_start": 14.0, "t_end": 15.0},
    ]


def test_stale_speech_events_are_not_published(w: World) -> None:
    published: list[dict[str, Any]] = []
    c = make(w)
    c._on_publish = published.append
    rid, cid = start(c, w)
    c.handle(ev.SpeechStarted(rid, cid + 5, 10.0, 10.3))
    c.handle(ev.SpeechEnded(rid + 1, cid, 10.0, 12.0))
    assert speech_events(published) == []


def test_dropping_a_session_ends_an_open_utterance(w: World) -> None:
    """A client must not wait for a `speech_end` that never comes (cancel, no flush)."""
    published: list[dict[str, Any]] = []
    c = make(w)
    c._on_publish = published.append
    rid, cid = start(c, w)
    c.handle(ev.SpeechStarted(rid, cid, 10.0, 10.3))
    c.handle(ev.CancelRequested())
    assert speech_events(published)[-1] == {
        "event": "speech_end",
        "utt": 1,
        "session_id": rid,
        "t_start": 10.0,
        "t_end": None,
    }


def test_utterance_numbers_continue_across_sessions(w: World) -> None:
    published: list[dict[str, Any]] = []
    c = make(w)
    c._on_publish = published.append
    rid, cid = start(c, w)
    c.handle(ev.SpeechStarted(rid, cid, 10.0, 10.3))
    c.handle(ev.SpeechEnded(rid, cid, 10.0, 11.0))
    c.handle(ev.ContinuousToggle())
    c.handle(ev.FlushDone(rid, cid, stop_op(c), "stop"))
    rid2, cid2 = start(c, w)
    c.handle(ev.SpeechStarted(rid2, cid2, 20.0, 20.3))
    assert [m["utt"] for m in speech_events(published)] == [1, 1, 2]


# --- transcripts for subscribers (task 6.2; 10 §10.2) ---------------------------------------

DONE = InjectResult(True, "clipboard", 10, None, False, None)


def finished(job_id: int, text: str, text_at: float, audio: float, stt: float) -> ev.JobFinished:
    timings = {"audio": audio, "stt": stt}
    return ev.JobFinished(job_id, "continuous", DONE, timings, False, 1, text, text_at)


def text_events(published: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m for m in published if m["event"] in ("transcript", "utterance_dropped")]


def listening(w: World) -> tuple[Controller, list[dict[str, Any]], int, int]:
    published: list[dict[str, Any]] = []
    c = make(w)
    c._on_publish = published.append
    rid, cid = start(c, w)
    return c, published, rid, cid


def test_one_utterance_gives_one_transcript(w: World) -> None:
    c, published, rid, cid = listening(w)
    c.handle(ev.SpeechStarted(rid, cid, 10.0, 10.3))
    c.handle(ev.SegmentReady(rid, cid, segment(seconds=2.5)))
    c.handle(ev.SpeechEnded(rid, cid, 10.0, 12.0))
    assert text_events(published) == []  # the text is not ready yet
    job = w.jobs[0].id
    c.handle(finished(job, "Jaka jest pogoda? ", 13.1, 2.5, 0.4))
    assert text_events(published) == [
        {
            "event": "transcript",
            "utt": 1,
            "session_id": rid,
            "job_ids": [job],
            "final": True,
            "text": "Jaka jest pogoda?",
            "language": "pl",
            "t_start": 10.0,
            "t_end": 12.0,
            "t_ready": 13.1,
            "audio_s": 2.5,
            "stt_s": 0.4,
        }
    ]


def test_a_split_utterance_is_joined_into_one_transcript(w: World) -> None:
    c, published, rid, cid = listening(w)
    c.handle(ev.SpeechStarted(rid, cid, 10.0, 10.3))
    c.handle(ev.SegmentReady(rid, cid, segment(seq=1, seconds=15, cut="max_length")))
    c.handle(ev.SegmentReady(rid, cid, segment(seq=2, seconds=3)))
    c.handle(ev.SpeechEnded(rid, cid, 10.0, 27.5))
    first, second = (j.id for j in w.jobs)
    c.handle(finished(first, "Pierwsza część, ", 26.0, 15.0, 1.5))
    assert text_events(published) == []
    c.handle(ev.JobDiscarded(second, "continuous", "filtered"))
    (transcript,) = text_events(published)
    assert (transcript["text"], transcript["job_ids"]) == ("Pierwsza część,", [first, second])
    assert (transcript["t_ready"], transcript["audio_s"], transcript["stt_s"]) == (26.0, 15.0, 1.5)


def test_parts_with_text_are_joined_with_one_space(w: World) -> None:
    c, published, rid, cid = listening(w)
    c.handle(ev.SpeechStarted(rid, cid, 10.0, 10.3))
    c.handle(ev.SegmentReady(rid, cid, segment(seq=1, seconds=15, cut="max_length")))
    c.handle(ev.SegmentReady(rid, cid, segment(seq=2, seconds=3)))
    c.handle(ev.SpeechEnded(rid, cid, 10.0, 27.5))
    first, second = (j.id for j in w.jobs)
    c.handle(finished(first, "Ala ma kota ", 26.0, 15.0, 1.5))
    c.handle(finished(second, "i psa. ", 28.0, 3.0, 0.4))
    (transcript,) = text_events(published)
    assert transcript["text"] == "Ala ma kota i psa."
    assert (transcript["t_ready"], transcript["audio_s"]) == (28.0, 18.0)


def test_an_utterance_without_text_is_dropped_with_the_last_reason(w: World) -> None:
    c, published, rid, cid = listening(w)
    c.handle(ev.SpeechStarted(rid, cid, 10.0, 10.3))
    c.handle(ev.SegmentReady(rid, cid, segment()))
    c.handle(ev.SpeechEnded(rid, cid, 10.0, 12.0))
    c.handle(ev.JobDiscarded(w.jobs[0].id, "continuous", "filtered"))
    assert text_events(published) == [
        {"event": "utterance_dropped", "utt": 1, "session_id": rid, "reason": "filtered"}
    ]


def test_a_failed_job_drops_its_utterance(w: World) -> None:
    c, published, rid, cid = listening(w)
    c.handle(ev.SpeechStarted(rid, cid, 10.0, 10.3))
    c.handle(ev.SegmentReady(rid, cid, segment()))
    c.handle(ev.SpeechEnded(rid, cid, 10.0, 12.0))
    c.handle(ev.JobFailed(w.jobs[0].id, "continuous", 2.0, "timeout"))
    assert text_events(published)[0]["reason"] == "failed"


def test_speech_without_a_segment_is_dropped_at_speech_end(w: World) -> None:
    """The segmenter drops an utterance with less than min_speech_ms of speech."""
    c, published, rid, cid = listening(w)
    c.handle(ev.SpeechStarted(rid, cid, 10.0, 10.3))
    c.handle(ev.SpeechEnded(rid, cid, 10.0, 10.2))
    assert text_events(published) == [
        {"event": "utterance_dropped", "utt": 1, "session_id": rid, "reason": "no_speech"}
    ]


def test_a_job_that_finishes_before_speech_end_waits_for_it(w: World) -> None:
    c, published, rid, cid = listening(w)
    c.handle(ev.SpeechStarted(rid, cid, 10.0, 10.3))
    c.handle(ev.SegmentReady(rid, cid, segment(seq=1, seconds=15, cut="max_length")))
    c.handle(finished(w.jobs[0].id, "Długo mówię ", 26.0, 15.0, 1.5))
    assert text_events(published) == []  # speech goes on: more parts can come
    c.handle(ev.SpeechEnded(rid, cid, 10.0, 25.5))
    assert text_events(published)[0]["text"] == "Długo mówię"


def test_cancel_drops_an_utterance_with_pending_jobs(w: World) -> None:
    c, published, rid, cid = listening(w)
    c.handle(ev.SpeechStarted(rid, cid, 10.0, 10.3))
    c.handle(ev.SegmentReady(rid, cid, segment(seq=1, seconds=15, cut="max_length")))
    c.handle(ev.CancelRequested())
    assert text_events(published) == []  # the pipeline still reports the cancelled job
    c.handle(ev.JobDiscarded(w.jobs[0].id, "continuous", "cancelled"))
    assert text_events(published)[0]["reason"] == "cancelled"


def test_cancel_drops_an_utterance_whose_jobs_were_drained(w: World) -> None:
    """Drained queued jobs report nothing; the utterance must still end."""
    c, published, rid, cid = listening(w)
    c.handle(ev.SpeechStarted(rid, cid, 10.0, 10.3))
    c.handle(ev.SegmentReady(rid, cid, segment()))
    w.cancel_result = CancelResult((w.jobs[0].id,), False, False)
    c.handle(ev.CancelRequested())
    assert text_events(published)[-1] == {
        "event": "utterance_dropped",
        "utt": 1,
        "session_id": rid,
        "reason": "cancelled",
    }
    assert c._utterances == {} and c._job_utt == {}


def test_transcript_language_is_auto_under_parakeet(w: World) -> None:
    published: list[dict[str, Any]] = []
    c = make(w, config=Config())
    c._on_publish = published.append
    rid, cid = start(c, w)
    c.handle(ev.SpeechStarted(rid, cid, 10.0, 10.3))
    c.handle(ev.SegmentReady(rid, cid, segment()))
    c.handle(ev.SpeechEnded(rid, cid, 10.0, 12.0))
    c.handle(finished(w.jobs[0].id, "Hello. ", 13.0, 2.0, 0.3))
    assert text_events(published)[0]["language"] == "auto"


def test_ptt_jobs_give_no_transcript(w: World) -> None:
    published: list[dict[str, Any]] = []
    c = make(w)
    c._on_publish = published.append
    c.handle(ev.JobFinished(7, "ptt", DONE, {"audio": 1.0}, False, None, "Tekst. ", 5.0))
    assert text_events(published) == []


# --- conversation mode (task 6.3; ADR-019) --------------------------------------------------


def converse(w: World, owner: int = 7) -> tuple[Controller, list[dict[str, Any]], int, int]:
    """IDLE → conversation mode with the microphone open; returns (c, published, rid, cid)."""
    published: list[dict[str, Any]] = []
    c = make(w)
    c._on_publish = published.append
    answer = reply()
    c.handle(ev.ConversationStart(owner, answer))
    cont = c._cont
    assert cont is not None
    c.handle(ev.CaptureOpenDue(cont.recording_id))
    assert answer.result(0) == {"ok": True}
    w.calls.clear()
    return c, published, cont.recording_id, cont.capture_id


def conversation_events(published: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m for m in published if m["event"] == "conversation"]


def test_conversation_starts_with_one_notification_and_no_sound(w: World) -> None:
    """The microphone must not open unseen (user decision 2026-10-10); no sound: echo."""
    c = make(w)
    published: list[dict[str, Any]] = []
    c._on_publish = published.append
    c.handle(ev.ConversationStart(7, reply()))
    assert c._cont is not None
    rid = c._cont.recording_id
    c.handle(ev.CaptureOpenDue(rid))
    assert c.mode is Mode.CONTINUOUS
    assert [call for call in w.calls if call[0] in ("sound", "notify")] == [
        ("notify", "conversation", "Conversation mode enabled", False)
    ]
    assert conversation_events(published) == [
        {"event": "conversation", "on": True, "session_id": rid}
    ]
    assert status_of(c)["conversation"] is True


def test_conversation_jobs_go_to_the_subscriber(w: World) -> None:
    c, _, rid, cid = converse(w)
    c.handle(ev.SegmentReady(rid, cid, segment()))
    assert w.jobs[0].sink == "subscriber"


def test_dictation_jobs_are_injected(c: Controller, w: World) -> None:
    rid, cid = start(c, w)
    c.handle(ev.SegmentReady(rid, cid, segment()))
    assert w.jobs[0].sink == "inject"
    assert status_of(c)["conversation"] is False


def test_conversation_is_busy_unless_idle(c: Controller, w: World) -> None:
    start(c, w)
    answer = reply()
    c.handle(ev.ConversationStart(7, answer))
    assert answer.result(0)["error"] == "busy"


def test_conversation_needs_a_ready_engine(w: World) -> None:
    c = make(w, engine=EngineHealth.DOWN)
    answer = reply()
    c.handle(ev.ConversationStart(7, answer))
    assert answer.result(0)["ok"] is False
    assert c.mode is Mode.IDLE


def test_owner_disconnect_stops_quietly(w: World) -> None:
    c, published, rid, cid = converse(w)
    c.handle(ev.ConversationEnd(8))  # another connection: nothing happens
    assert c._cont is not None and not c._cont.stopping
    c.handle(ev.ConversationEnd(7))
    c.handle(ev.FlushDone(rid, cid, stop_op(c), "stop"))
    assert c.mode is Mode.IDLE
    assert [call for call in w.calls if call[0] in ("sound", "notify")] == []
    assert conversation_events(published)[-1] == {
        "event": "conversation",
        "on": False,
        "session_id": rid,
        "reason": "client",
    }


def test_the_toggle_hotkey_ends_the_conversation(w: World) -> None:
    c, published, rid, cid = converse(w)
    c.handle(ev.ContinuousToggle())
    c.handle(ev.FlushDone(rid, cid, stop_op(c), "stop"))
    assert c.mode is Mode.IDLE
    assert conversation_events(published)[-1]["reason"] == "user"
    c.handle(ev.ConversationEnd(7))  # the owner disconnects later: no second event
    assert len(conversation_events(published)) == 2


def test_cancel_ends_the_conversation(w: World) -> None:
    c, published, _, _ = converse(w)
    c.handle(ev.CancelRequested())
    assert c.mode is Mode.IDLE
    assert conversation_events(published)[-1]["reason"] == "user"
    assert [call for call in w.calls if call[0] == "sound"] == []


def test_a_new_dictation_after_a_conversation_is_ordinary(w: World) -> None:
    c, _, rid, cid = converse(w)
    c.handle(ev.ConversationEnd(7))
    c.handle(ev.FlushDone(rid, cid, stop_op(c), "stop"))
    rid2, cid2 = start(c, w)
    c.handle(ev.SegmentReady(rid2, cid2, segment()))
    assert w.jobs[-1].sink == "inject"


def test_errors_still_notify_in_conversation(w: World) -> None:
    c, *_ = converse(w)
    c.handle(ev.JobFailed(1, "continuous", 2.0, "timeout"))
    assert ("notify", "job_failed", "Could not transcribe segment (2 s)", False) in w.calls


# --- speculative transcripts (task 6.4) -------------------------------------------------------


def spec_segment(seq: int = 1, seconds: float = 2.0, reuses: bool = False) -> AudioSegment:
    samples = np.zeros(int(seconds * 16000), dtype=np.float32)
    return AudioSegment(samples, 1, seq, 99.0, 1500, "silence", None, reuses)


def transcripts(published: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        m
        for m in published
        if m["event"] in ("transcript", "transcript_retracted", "utterance_dropped")
    ]


def test_conversation_session_resets_the_consumer_with_speculation(w: World) -> None:
    c = make(w)
    c.handle(ev.ConversationStart(7, reply()))
    assert c._cont is not None
    assert ("consumer.reset_continuous", c._cont.recording_id, c._cont.capture_id, 250) in w.calls


def test_dictation_session_resets_the_consumer_without_speculation(c: Controller, w: World) -> None:
    c.handle(ev.ContinuousToggle())
    assert c._cont is not None
    assert ("consumer.reset_continuous", c._cont.recording_id, c._cont.capture_id, 0) in w.calls


def test_speculative_text_then_reused_final(w: World) -> None:
    c, published, rid, cid = converse(w)
    c.handle(ev.SpeechStarted(rid, cid, 10.0, 10.3))
    c.handle(ev.SpeculativeReady(rid, cid, spec_segment(), 12.0))
    (job,) = w.jobs
    assert (job.speculative, job.sink, job.cut) == (True, "subscriber", "silence")
    c.handle(finished(job.id, "Jaka pogoda? ", 12.6, 2.0, 0.3))
    (spec,) = transcripts(published)
    assert (spec["final"], spec["text"], spec["t_end"], spec["t_ready"]) == (
        False,
        "Jaka pogoda?",
        12.0,
        12.6,
    )
    assert spec["job_ids"] == [job.id]
    c.handle(ev.SegmentReady(rid, cid, spec_segment(reuses=True)))
    assert len(w.jobs) == 1  # no second engine request
    c.handle(ev.SpeechEnded(rid, cid, 10.0, 12.0))
    final = transcripts(published)[-1]
    assert (final["final"], final["text"], final["job_ids"], final["t_ready"]) == (
        True,
        "Jaka pogoda?",
        [job.id],
        12.6,
    )


def test_final_waits_for_a_speculative_job_still_running(w: World) -> None:
    c, published, rid, cid = converse(w)
    c.handle(ev.SpeechStarted(rid, cid, 10.0, 10.3))
    c.handle(ev.SpeculativeReady(rid, cid, spec_segment(), 12.0))
    c.handle(ev.SegmentReady(rid, cid, spec_segment(reuses=True)))
    c.handle(ev.SpeechEnded(rid, cid, 10.0, 12.0))
    assert transcripts(published) == []
    c.handle(finished(w.jobs[0].id, "Jaka pogoda? ", 12.9, 2.0, 0.3))
    # The speech has ended: only the final text, no speculative one before it.
    assert [(m["event"], m["final"]) for m in transcripts(published)] == [("transcript", True)]


def test_resumed_speech_retracts_and_the_final_is_new(w: World) -> None:
    c, published, rid, cid = converse(w)
    c.handle(ev.SpeechStarted(rid, cid, 10.0, 10.3))
    c.handle(ev.SpeculativeReady(rid, cid, spec_segment(), 12.0))
    spec_job = w.jobs[0].id
    c.handle(finished(spec_job, "Jaka ", 12.6, 2.0, 0.3))
    c.handle(ev.SpeculationRetracted(rid, cid))
    assert transcripts(published)[-1] == {
        "event": "transcript_retracted",
        "utt": 1,
        "session_id": rid,
        "job_ids": [spec_job],
    }
    c.handle(ev.SegmentReady(rid, cid, spec_segment(seconds=4)))
    final_job = w.jobs[-1].id
    assert final_job != spec_job and not w.jobs[-1].speculative
    c.handle(ev.SpeechEnded(rid, cid, 10.0, 14.0))
    c.handle(finished(final_job, "Jaka jest pogoda? ", 15.0, 4.0, 0.5))
    final = transcripts(published)[-1]
    assert (final["text"], final["job_ids"]) == ("Jaka jest pogoda?", [final_job])


def test_a_retracted_queued_job_is_withdrawn(w: World) -> None:
    c, _, rid, cid = converse(w)
    c.handle(ev.SpeechStarted(rid, cid, 10.0, 10.3))
    c.handle(ev.SpeculativeReady(rid, cid, spec_segment(), 12.0))
    job = w.jobs[0].id
    c.handle(ev.SpeculationRetracted(rid, cid))
    assert ("pipeline.withdraw", job) in w.calls
    assert c._spec_jobs == {} and job not in c._outstanding


def test_a_retracted_job_that_finishes_later_is_ignored(w: World) -> None:
    c, published, rid, cid = converse(w)
    c.handle(ev.SpeechStarted(rid, cid, 10.0, 10.3))
    c.handle(ev.SpeculativeReady(rid, cid, spec_segment(), 12.0))
    w.queued.clear()  # already in the engine: it cannot be withdrawn
    c.handle(ev.SpeculationRetracted(rid, cid))
    assert transcripts(published) == []  # nothing was sent, so nothing to retract
    c.handle(finished(w.jobs[0].id, "Jaka ", 12.6, 2.0, 0.3))
    assert transcripts(published) == []


def test_a_speculative_job_without_text_gives_no_event(w: World) -> None:
    c, published, rid, cid = converse(w)
    c.handle(ev.SpeechStarted(rid, cid, 10.0, 10.3))
    c.handle(ev.SpeculativeReady(rid, cid, spec_segment(), 12.0))
    c.handle(ev.JobDiscarded(w.jobs[0].id, "continuous", "filtered"))
    assert transcripts(published) == []
    c.handle(ev.SegmentReady(rid, cid, spec_segment(reuses=True)))
    c.handle(ev.SpeechEnded(rid, cid, 10.0, 12.0))
    assert transcripts(published)[-1]["reason"] == "filtered"


def job_results(published: list[dict[str, Any]]) -> list[tuple[int, str]]:
    return [(m["job_id"], m["result"]) for m in published if m["event"] == "job"]


def test_a_reused_speculative_job_reports_once_its_text_is_final(w: World) -> None:
    """The `job` event of a speculative job waits until its text is final or retracted."""
    c, published, rid, cid = converse(w)
    c.handle(ev.SpeechStarted(rid, cid, 10.0, 10.3))
    c.handle(ev.SpeculativeReady(rid, cid, spec_segment(), 12.0))
    job = w.jobs[0].id
    c.handle(finished(job, "Jaka pogoda? ", 12.6, 2.0, 0.3))
    assert job_results(published) == []  # the text is not final yet
    c.handle(ev.SegmentReady(rid, cid, spec_segment(reuses=True)))
    assert job_results(published) == [(job, "injected")]  # the outcome of the job itself
    c.handle(ev.SpeechEnded(rid, cid, 10.0, 12.0))
    assert job_results(published) == [(job, "injected")]


def test_a_speculative_job_that_finishes_after_the_reuse_reports_at_once(w: World) -> None:
    c, published, rid, cid = converse(w)
    c.handle(ev.SpeechStarted(rid, cid, 10.0, 10.3))
    c.handle(ev.SpeculativeReady(rid, cid, spec_segment(), 12.0))
    c.handle(ev.SegmentReady(rid, cid, spec_segment(reuses=True)))
    c.handle(ev.SpeechEnded(rid, cid, 10.0, 12.0))
    job = w.jobs[0].id
    c.handle(finished(job, "Jaka pogoda? ", 12.9, 2.0, 0.3))
    assert job_results(published) == [(job, "injected")]


def test_a_sent_and_retracted_speculative_job_reports_retracted(w: World) -> None:
    c, published, rid, cid = converse(w)
    c.handle(ev.SpeechStarted(rid, cid, 10.0, 10.3))
    c.handle(ev.SpeculativeReady(rid, cid, spec_segment(), 12.0))
    spec_job = w.jobs[0].id
    c.handle(finished(spec_job, "Jaka ", 12.6, 2.0, 0.3))
    c.handle(ev.SpeculationRetracted(rid, cid))
    (event,) = [m for m in published if m["event"] == "job"]
    assert (event["job_id"], event["result"], event["chars"], event["processing_s"]) == (
        spec_job,
        "retracted",
        10,
        0.3,
    )


def test_a_speculative_job_that_finishes_after_its_retraction_reports_retracted(
    w: World,
) -> None:
    c, published, rid, cid = converse(w)
    c.handle(ev.SpeechStarted(rid, cid, 10.0, 10.3))
    c.handle(ev.SpeculativeReady(rid, cid, spec_segment(), 12.0))
    w.queued.clear()  # already in the engine: it cannot be withdrawn
    c.handle(ev.SpeculationRetracted(rid, cid))
    job = w.jobs[0].id
    c.handle(finished(job, "Jaka ", 12.6, 2.0, 0.3))
    assert job_results(published) == [(job, "retracted")]


def test_a_withdrawn_speculative_job_reports_retracted(w: World) -> None:
    c, published, rid, cid = converse(w)
    c.handle(ev.SpeechStarted(rid, cid, 10.0, 10.3))
    c.handle(ev.SpeculativeReady(rid, cid, spec_segment(seconds=2.0), 12.0))
    job = w.jobs[0].id
    c.handle(ev.SpeculationRetracted(rid, cid))
    (event,) = [m for m in published if m["event"] == "job"]
    assert event == {
        "event": "job",
        "job_id": job,
        "source": "continuous",
        "audio_s": 2.0,
        "processing_s": None,
        "chars": 0,
        "result": "retracted",
    }


def test_a_held_speculative_job_reports_retracted_when_the_session_drops(w: World) -> None:
    c, published, rid, cid = converse(w)
    c.handle(ev.SpeechStarted(rid, cid, 10.0, 10.3))
    c.handle(ev.SpeculativeReady(rid, cid, spec_segment(), 12.0))
    job = w.jobs[0].id
    c.handle(finished(job, "Jaka ", 12.6, 2.0, 0.3))
    c.handle(ev.CancelRequested())
    assert job_results(published) == [(job, "retracted")]
    assert c._spec_held == {}


def test_drained_jobs_report_cancelled(w: World) -> None:
    c, published, rid, cid = listening(w)
    c.handle(ev.SpeechStarted(rid, cid, 10.0, 10.3))
    c.handle(ev.SegmentReady(rid, cid, segment()))
    job = w.jobs[0].id
    w.cancel_result = CancelResult((job,), False, False)
    c.handle(ev.CancelRequested())
    assert job_results(published) == [(job, "cancelled")]


def test_dictation_ignores_speculative_events(c: Controller, w: World) -> None:
    rid, cid = start(c, w)
    c.handle(ev.SpeechStarted(rid, cid, 10.0, 10.3))
    c.handle(ev.SpeculativeReady(rid, cid, spec_segment(), 12.0))
    assert w.jobs == []


def test_default_schedule_posts_the_event_after_the_delay(w: World) -> None:
    c = Controller(
        WHISPER,
        capture=w,
        consumer=Consumer(w),
        pipeline=Pipeline(w),
        feedback=w,
        lifecycle=w,
        reload_target=Reload(w),
        load_config=w.load_config,
    )
    c._schedule(0.01, ev.CaptureOpenDue(5))
    assert c.events.get(timeout=2) == ev.CaptureOpenDue(5)


# --- IPC subscribe stream (task 2.5; 10 §10.2) ----------------------------------------------


def test_state_changes_and_jobs_are_published(w: World) -> None:
    published: list[dict[str, Any]] = []
    c = make(w)
    c._on_publish = published.append
    rid, cid = start(c, w)
    assert [m["status"]["state"] for m in published] == ["LISTENING"]
    c.handle(ev.SpeechStarted(rid, cid, 10.0, 10.3))
    c.handle(ev.SegmentReady(rid, cid, segment(seconds=2)))
    job_id = w.jobs[0].id
    c.handle(ev.JobDiscarded(job_id, "continuous", "filtered"))
    state_and_jobs = [m for m in published if m["event"] in ("state", "job")]
    assert [m.get("status", {}).get("state") or m["result"] for m in state_and_jobs] == [
        "LISTENING",
        "LISTENING (speech)",
        "LISTENING (speech), transcribing 1",
        "filtered",
        "LISTENING (speech)",
    ]
    assert state_and_jobs[3] == {
        "event": "job",
        "job_id": job_id,
        "source": "continuous",
        "audio_s": 2.0,
        "processing_s": None,
        "chars": 0,
        "result": "filtered",
    }


@pytest.mark.parametrize(
    ("result", "outcome"),
    [
        (InjectResult(True, "clipboard", 9, "gedit", False, None), "injected"),
        (InjectResult(True, "clipboard", 9, None, True, None, no_target=True), "clipboard"),
        (InjectResult(False, "type", 0, "xterm", False, "xdotool failed"), "failed"),
    ],
)
def test_finished_and_failed_jobs_are_published(
    w: World, result: InjectResult, outcome: str
) -> None:
    published: list[dict[str, Any]] = []
    c = make(w)
    c._on_publish = published.append
    timings = {"audio": 1.5, "stt": 0.7}
    c.handle(ev.JobFinished(4, "ptt", result, timings))
    c.handle(ev.JobFailed(5, "ptt", 3.0, "HTTP 500"))
    jobs = [m for m in published if m["event"] == "job"]
    assert [
        (j["job_id"], j["result"], j["audio_s"], j["processing_s"], j["chars"]) for j in jobs
    ] == [
        (4, outcome, 1.5, 0.7, result.chars),
        (5, "failed", 3.0, None, 0),
    ]


# --- audio errors (task 2.7; 05 §5.6) --------------------------------------------------------


def test_muted_microphone_is_notified_once(c: Controller, w: World) -> None:
    rid, cid = start(c, w)
    c.handle(ev.MicrophoneSilent(rid, cid))
    c.handle(ev.MicrophoneSilent(rid, cid))
    c.handle(ev.MicrophoneSilent(rid + 1, cid))  # another session: stale
    assert w.notifications() == ["Microphone appears to be muted"]
    assert c.mode is Mode.CONTINUOUS  # dictation continues
    c.handle(ev.ContinuousToggle())
    w.calls.clear()
    c2 = make(w)
    rid2, cid2 = start(c2, w)
    c2.handle(ev.ContinuousToggle())  # stopping: no notification any more
    c2.handle(ev.MicrophoneSilent(rid2, cid2))
    assert w.notifications() == []


def test_status_reports_the_consumer_overflows(c: Controller, w: World) -> None:
    w.overflows = 3
    assert status_of(c)["audio"]["overflows"] == 3


# --- informational notifications (task 2.8; 10 §10.6, level "all") --------------------------


def informational(w: World) -> list[tuple[str, str]]:
    return [(c[1], c[2]) for c in w.calls if c[0] == "notify" and c[3]]


def test_dictation_enabled_and_disabled_are_informational(c: Controller, w: World) -> None:
    c.handle(ev.ContinuousToggle())
    assert informational(w) == []  # not before the microphone opens
    c.handle(ev.CaptureOpenDue(1))
    assert informational(w) == [("dictation", "Dictation enabled")]
    c.handle(ev.ContinuousToggle())
    assert len(informational(w)) == 1  # not until the flush is confirmed
    c.handle(ev.FlushDone(1, 1, stop_op(c), "stop"))
    assert informational(w)[-1] == ("dictation", "Dictation disabled")


def test_error_stops_keep_their_own_notification(w: World) -> None:
    c = make(w)
    rid, cid = start(c, w)
    c.handle(ev.EngineStateChanged(EngineHealth.DOWN))
    c.handle(ev.FlushDone(rid, cid, stop_op(c), "stop"))
    keys = [(call[1], call[2]) for call in w.calls if call[0] == "notify"]
    assert keys == [
        ("continuous", "STT engine stopped working — dictation stopped"),
        ("dictation", "Dictation disabled"),  # another key: the error stays visible
    ]


def test_cancel_notifies_only_an_open_session(c: Controller, w: World) -> None:
    c.handle(ev.ContinuousToggle())
    c.handle(ev.CancelRequested())  # before the open: nothing was enabled
    assert informational(w) == []
    start(c, w)
    c.handle(ev.CancelRequested())
    assert informational(w) == [("dictation", "Dictation disabled")]


def test_shutdown_does_not_notify(c: Controller, w: World) -> None:
    start(c, w)
    c.handle(ev.ShutdownRequested())
    assert informational(w) == []


def test_segments_take_the_language_active_when_they_arrive(c: Controller, w: World) -> None:
    # task 3.7: the switch applies to the next segment; the pipeline drops the context.
    rid, cid = start(c, w)
    c.handle(ev.SegmentReady(rid, cid, segment(1)))
    c.handle(ev.LanguageSwitch())
    c.handle(ev.SegmentReady(rid, cid, segment(2)))
    assert [j.language for j in w.jobs] == ["pl", "en"]
    assert "language_alt" not in w.sounds()  # the microphone is open
