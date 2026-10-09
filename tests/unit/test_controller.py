"""Controller transition tables (docs/04-state-machine.md §4.3, §4.6), one test per row."""

import dataclasses
import threading
from concurrent.futures import Future
from typing import Any

import numpy as np
import pytest

from local_stt import events as ev
from local_stt.config import Config, ConfigError, SttConfig
from local_stt.controller import Controller, Mode, config_diff, reload_group
from local_stt.history import TranscriptHistory
from local_stt.interfaces import (
    AudioClip,
    AudioOpenError,
    CancelResult,
    EngineHealth,
    HotkeyProblem,
    InjectResult,
    Job,
    Sound,
)

# These tests were written for whisper-server, the default engine before task 4.5; the engine is
# pinned so that they keep testing its language switch and status.
WHISPER = Config(stt=SttConfig(engine="whisper-server"))
SOUND_S = 0.13
NOTHING = CancelResult((), in_flight_cancelled=False, injection_in_flight=False)


class World:
    """All fakes record into one ordered `calls` list."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.now = 100.0
        self.open_error: str | None = None
        self.sound_duration: float | None = SOUND_S
        self.generation = 7
        self.cancel_result = NOTHING
        self.jobs: list[Job] = []
        self.next_config: Config | Exception = WHISPER
        self.statuses: list[str] = []
        self.hotkey_problems: list[HotkeyProblem] = []
        self.vad_available = True
        self.overflows = 0

    # AudioCaptureControl
    def open(self, recording_id: int, capture_id: int) -> None:
        self.calls.append(("capture.open", recording_id, capture_id))
        if self.open_error:
            raise AudioOpenError(self.open_error)

    def close(self) -> None:
        self.calls.append(("capture.close",))

    # Feedback
    def play(self, sound: Sound) -> float | None:
        self.calls.append(("sound", sound))
        return self.sound_duration

    def notify(self, key: str, title: str, body: str = "", *, informational: bool = False) -> None:
        self.calls.append(("notify", key, title, informational))

    # DaemonLifecycle
    def shutdown(self, *, x11_alive: bool) -> None:
        self.calls.append(("shutdown", x11_alive))

    def load_config(self) -> tuple[Config, list[str]]:
        if isinstance(self.next_config, Exception):
            raise self.next_config
        return self.next_config, []

    def sounds(self) -> list[str]:
        return [c[1] for c in self.calls if c[0] == "sound"]

    def notifications(self) -> list[str]:
        return [c[2] for c in self.calls if c[0] == "notify"]

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


class Consumer:
    def __init__(self, world: World) -> None:
        self.w = world

    def begin_ptt(self, recording_id: int, capture_id: int) -> None:
        self.w.calls.append(("consumer.begin_ptt", recording_id, capture_id))

    def mask_start_sound(self, recording_id: int, capture_id: int, until: float) -> None:
        self.w.calls.append(("consumer.mask", recording_id, capture_id, until))

    def finish_ptt(
        self, recording_id: int, capture_id: int, operation_id: int, ended_at: float, cut: str
    ) -> None:
        self.w.calls.append(
            ("consumer.finish_ptt", recording_id, capture_id, operation_id, ended_at, cut)
        )

    def discard(self, recording_id: int, capture_id: int) -> None:
        self.w.calls.append(("consumer.discard", recording_id, capture_id))

    @property
    def overflows(self) -> int:
        return self.w.overflows

    @property
    def continuous_available(self) -> bool:
        return self.w.vad_available

    def reset_continuous(self, recording_id: int, capture_id: int) -> None:
        self.w.calls.append(("consumer.reset_continuous", recording_id, capture_id))

    def flush(
        self, recording_id: int, capture_id: int, operation_id: int, purpose: str, at: float
    ) -> None:
        self.w.calls.append(("consumer.flush", recording_id, capture_id, operation_id, purpose, at))


class Pipeline:
    def __init__(self, world: World) -> None:
        self.w = world

    @property
    def generation(self) -> int:
        return self.w.generation

    def submit(self, job: Job) -> None:
        self.w.calls.append(("pipeline.submit", job.id))
        self.w.jobs.append(job)

    def cancel_all(self) -> CancelResult:
        self.w.calls.append(("pipeline.cancel_all",))
        self.w.generation += 1
        return self.w.cancel_result

    def reinject(self, job_id: int, text: str) -> None:
        self.w.calls.append(("pipeline.reinject", job_id, text))

    def pause(self) -> None:
        self.w.calls.append(("pipeline.pause",))

    def resume(self) -> None:
        self.w.calls.append(("pipeline.resume",))


class Reload:
    def __init__(self, world: World) -> None:
        self.w = world

    def apply_live(self, config: Config) -> None:
        self.w.calls.append(("reload.live", config))

    def apply_at_idle(self, config: Config) -> list[HotkeyProblem]:
        self.w.calls.append(("reload.idle", config))
        return self.w.hotkey_problems

    def restart_server(self, config: Config) -> None:
        self.w.calls.append(("reload.restart", config))

    def use_server(self, config: Config) -> None:
        self.w.calls.append(("reload.use_server", config))


@pytest.fixture
def w() -> World:
    return World()


def make(
    w: World,
    *,
    engine: EngineHealth | None = EngineHealth.READY,
    config: Config | None = None,
    history: TranscriptHistory | None = None,
) -> Controller:
    c = Controller(
        config or WHISPER,
        capture=w,
        consumer=Consumer(w),
        pipeline=Pipeline(w),
        feedback=w,
        lifecycle=w,
        reload_target=Reload(w),
        load_config=w.load_config,
        clock=lambda: w.now,
        schedule=lambda delay, event: w.calls.append(("schedule", delay, event)),
        on_status=w.statuses.append,
        history=history,
    )
    if engine is not None:
        c.handle(ev.EngineStateChanged(engine))
    w.calls.clear()
    w.statuses.clear()
    return c


@pytest.fixture
def c(w: World) -> Controller:
    return make(w)


def reply() -> Future[dict[str, Any]]:
    return Future()


def clip(seconds: float = 2.0, ended_at: float = 102.0) -> AudioClip:
    samples = np.zeros(int(seconds * 16000), dtype=np.float32)
    return AudioClip(samples, 16000, seconds, started_at=ended_at - seconds, ended_at=ended_at)


def press(c: Controller, w: World, at: float = 100.0) -> None:
    """IDLE → PTT_RECORDING with recording/capture ids 1, 1 (or the next ones)."""
    c.handle(ev.PttPressed(at))
    assert c.mode is Mode.PTT_RECORDING
    w.calls.clear()


def ids(c: Controller) -> tuple[int, int]:
    rec = c._rec
    assert rec is not None
    return rec.recording_id, rec.capture_id


def record_and_release(c: Controller, w: World, at: float = 102.0) -> tuple[int, int, int]:
    """PTT_RECORDING(stopping); returns (recording_id, capture_id, operation_id)."""
    press(c, w, at=100.0)
    rid, cid = ids(c)
    c.handle(ev.PttReleased(at))
    assert c._rec is not None and c._rec.operation_id is not None
    op = c._rec.operation_id
    w.calls.clear()
    return rid, cid, op


def finish_job(c: Controller, w: World) -> Job:
    rid, cid, op = record_and_release(c, w)
    c.handle(ev.RecordingFinished(rid, cid, op, clip(), "release"))
    w.calls.clear()
    return w.jobs[-1]


# --- IDLE --------------------------------------------------------------------------------


def test_idle_ptt_pressed_opens_capture_after_begin(c: Controller, w: World) -> None:
    r = reply()
    c.handle(ev.PttPressed(100.0, r))
    assert w.calls == [("consumer.begin_ptt", 1, 1), ("capture.open", 1, 1)]
    assert c.mode is Mode.PTT_RECORDING
    assert r.result() == {"ok": True}


def test_idle_ptt_pressed_open_failure_is_an_audio_error(c: Controller, w: World) -> None:
    w.open_error = "device busy"
    r = reply()
    c.handle(ev.PttPressed(100.0, r))
    assert w.names() == [
        "consumer.begin_ptt",
        "capture.open",
        "capture.close",
        "consumer.discard",
        "sound",
        "notify",
    ]
    assert w.sounds() == ["error"]
    assert c.mode is Mode.IDLE
    assert r.result() == {"ok": False, "error": "audio_error", "message": "device busy"}


@pytest.mark.parametrize(
    ("engine", "code"),
    [(EngineHealth.STARTING, "engine_starting"), (EngineHealth.DOWN, "engine_down")],
)
def test_idle_ptt_pressed_without_engine_is_rejected(
    w: World, engine: EngineHealth, code: str
) -> None:
    c = make(w, engine=engine)
    r = reply()
    c.handle(ev.PttPressed(100.0, r))
    assert w.sounds() == ["error"]
    assert w.notifications() == ["STT engine unavailable"]
    assert not any(name.startswith("capture") for name in w.names())
    assert c.mode is Mode.IDLE
    assert r.result()["error"] == code


def test_idle_cancel_with_discarded_jobs_plays_cancel(c: Controller, w: World) -> None:
    w.cancel_result = CancelResult((5,), in_flight_cancelled=True, injection_in_flight=True)
    r = reply()
    c.handle(ev.CancelRequested(r))
    assert w.calls == [("pipeline.cancel_all",), ("sound", "cancel")]
    assert r.result() == {"ok": True, "injection_in_flight": True}


def test_idle_cancel_with_nothing_to_discard_is_silent(c: Controller, w: World) -> None:
    r = reply()
    c.handle(ev.CancelRequested(r))
    assert w.calls == [("pipeline.cancel_all",)]
    assert r.result() == {"ok": True, "injection_in_flight": False}


@pytest.mark.parametrize(
    ("source", "reason", "sound"),
    [
        ("ptt", "no_speech", ["cancel"]),
        ("ptt", "filtered", []),
        ("ptt", "cancelled", []),
        ("continuous", "no_speech", []),
    ],
)
def test_idle_job_discarded_sound(
    c: Controller, w: World, source: Any, reason: Any, sound: list[str]
) -> None:
    c.handle(ev.JobDiscarded(1, source, reason))
    assert w.sounds() == sound


def test_job_discarded_no_speech_during_recording_is_silent(c: Controller, w: World) -> None:
    press(c, w)
    c.handle(ev.JobDiscarded(1, "ptt", "no_speech"))
    assert w.sounds() == []


# --- PTT_RECORDING -----------------------------------------------------------------------


def test_recording_started_plays_start_and_masks(c: Controller, w: World) -> None:
    press(c, w)
    w.now = 100.05
    c.handle(ev.RecordingStarted(1, 1))
    assert w.calls == [("sound", "start"), ("consumer.mask", 1, 1, pytest.approx(100.26))]


def test_recording_started_without_sound_does_not_mask(c: Controller, w: World) -> None:
    w.sound_duration = None
    press(c, w)
    c.handle(ev.RecordingStarted(1, 1))
    assert w.calls == [("sound", "start")]


def test_recording_started_while_stopping_is_ignored(c: Controller, w: World) -> None:
    rid, cid, _ = record_and_release(c, w)
    c.handle(ev.RecordingStarted(rid, cid))
    assert w.calls == []


def test_release_after_min_duration_finishes_ptt(c: Controller, w: World) -> None:
    press(c, w, at=100.0)
    r = reply()
    c.handle(ev.PttReleased(100.3, r))  # exactly ptt.min_duration_ms
    assert w.calls == [("capture.close",), ("consumer.finish_ptt", 1, 1, 1, 100.3, "release")]
    assert c.mode is Mode.PTT_RECORDING and c._rec is not None and c._rec.stopping
    assert r.result() == {"ok": True}
    assert w.sounds() == []


def test_release_before_min_duration_discards(c: Controller, w: World) -> None:
    press(c, w, at=100.0)
    c.handle(ev.PttReleased(100.29))
    assert w.calls == [("capture.close",), ("consumer.discard", 1, 1)]
    assert c.mode is Mode.IDLE
    assert w.jobs == []


def test_recording_finished_submits_job_and_plays_stop(c: Controller, w: World) -> None:
    rid, cid, op = record_and_release(c, w, at=102.0)
    c.handle(ev.RecordingFinished(rid, cid, op, clip(1.5, ended_at=102.0), "release"))
    assert w.calls == [("pipeline.submit", 1), ("sound", "stop")]
    job = w.jobs[0]
    assert (job.source, job.ended_at, job.generation, job.cut) == ("ptt", 102.0, 7, "release")
    assert (job.session_id, job.seq, len(job.audio)) == (None, None, 24000)
    assert c.mode is Mode.IDLE


@pytest.mark.parametrize("delta", [(1, 0, 0), (0, 1, 0), (0, 0, 1)])
def test_recording_finished_with_foreign_ids_is_ignored(
    c: Controller, w: World, delta: tuple[int, int, int]
) -> None:
    rid, cid, op = record_and_release(c, w)
    c.handle(ev.RecordingFinished(rid + delta[0], cid + delta[1], op + delta[2], clip(), "release"))
    assert w.calls == []
    assert c.mode is Mode.PTT_RECORDING


def test_recording_finished_before_release_is_ignored(c: Controller, w: World) -> None:
    press(c, w)
    c.handle(ev.RecordingFinished(1, 1, 1, clip(), "release"))
    assert w.calls == []


def test_recording_limit_finishes_with_limit_time(c: Controller, w: World) -> None:
    press(c, w)
    c.handle(ev.RecordingLimitReached(1, 1, ended_at=220.0))
    assert w.calls == [
        ("capture.close",),
        ("consumer.finish_ptt", 1, 1, 1, 220.0, "max_duration"),
        ("notify", "limit", "Recording limit reached", False),
    ]


def test_release_after_limit_does_not_finalize_again(c: Controller, w: World) -> None:
    press(c, w)
    c.handle(ev.RecordingLimitReached(1, 1, ended_at=220.0))
    w.calls.clear()
    c.handle(ev.PttReleased(221.0))  # the key was still held
    r = reply()
    c.handle(ev.PttReleased(221.0, r))
    c.handle(ev.RecordingLimitReached(1, 1, ended_at=221.0))
    assert w.calls == []
    assert r.result()["error"] == "invalid_in_mode"


@pytest.mark.parametrize("stopping", [False, True])
def test_cancel_key_discards_recording_but_keeps_jobs(
    c: Controller, w: World, stopping: bool
) -> None:
    if stopping:
        record_and_release(c, w)
    else:
        press(c, w)
    c.handle(ev.PttCancelKey())
    assert w.calls == [("capture.close",), ("consumer.discard", 1, 1), ("sound", "cancel")]
    assert c.mode is Mode.IDLE


@pytest.mark.parametrize("stopping", [False, True])
def test_cancel_requested_during_ptt_also_cancels_pipeline(
    c: Controller, w: World, stopping: bool
) -> None:
    if stopping:
        record_and_release(c, w)
    else:
        press(c, w)
    r = reply()
    c.handle(ev.CancelRequested(r))
    assert w.calls == [
        ("capture.close",),
        ("consumer.discard", 1, 1),
        ("pipeline.cancel_all",),
        ("sound", "cancel"),
    ]
    assert c.mode is Mode.IDLE
    assert r.result() == {"ok": True, "injection_in_flight": False}


@pytest.mark.parametrize("stopping", [False, True])
def test_audio_error_during_ptt(c: Controller, w: World, stopping: bool) -> None:
    if stopping:
        record_and_release(c, w)
    else:
        press(c, w)
    c.handle(ev.AudioError(1, 1, "device_lost", "unplugged"))
    assert w.calls == [
        ("capture.close",),
        ("consumer.discard", 1, 1),
        ("sound", "error"),
        ("notify", "audio", "Microphone error", False),
    ]
    assert c.mode is Mode.IDLE


def test_audio_error_with_foreign_capture_is_ignored(c: Controller, w: World) -> None:
    press(c, w)
    c.handle(ev.AudioError(1, 2, "device_lost", "old stream"))
    assert w.calls == []
    assert c.mode is Mode.PTT_RECORDING


def test_second_press_during_ptt(c: Controller, w: World) -> None:
    press(c, w)
    c.handle(ev.PttPressed(101.0))  # hotkey: ignored
    r = reply()
    c.handle(ev.PttPressed(101.0, r))  # IPC: rejected
    assert w.calls == []
    assert r.result() == {
        "ok": False,
        "error": "invalid_in_mode",
        "message": "PttPressed in PTT_RECORDING",
    }


def test_events_without_rows_in_idle(c: Controller, w: World) -> None:
    r = reply()
    c.handle(ev.PttReleased(100.0, r))
    c.handle(ev.PttReleased(100.0))
    c.handle(ev.PttCancelKey())
    c.handle(ev.RecordingStarted(1, 1))
    c.handle(ev.AudioError(1, 1, "device_lost", "x"))
    assert w.calls == []
    assert r.result()["error"] == "invalid_in_mode"


def test_stale_events_after_cancel_and_new_recording(c: Controller, w: World) -> None:
    rid1, cid1, op1 = record_and_release(c, w)
    c.handle(ev.PttCancelKey())
    press(c, w, at=110.0)
    assert ids(c) == (2, 2)
    c.handle(ev.RecordingStarted(rid1, cid1))
    c.handle(ev.RecordingFinished(rid1, cid1, op1, clip(), "release"))
    c.handle(ev.AudioError(rid1, cid1, "device_lost", "old"))
    c.handle(ev.RecordingLimitReached(rid1, cid1, ended_at=111.0))
    assert w.calls == []
    assert c.mode is Mode.PTT_RECORDING and ids(c) == (2, 2)


# --- Any state ---------------------------------------------------------------------------


def test_shutdown_during_ptt(c: Controller, w: World) -> None:
    press(c, w)
    c.handle(ev.ShutdownRequested())
    assert w.calls == [
        ("capture.close",),
        ("consumer.discard", 1, 1),
        ("pipeline.cancel_all",),
        ("shutdown", True),
    ]
    assert c.exit_code == 0


def test_x11_lost_closes_without_x11_operations(c: Controller, w: World) -> None:
    press(c, w)
    c.handle(ev.X11ConnectionLost())
    assert w.calls == [("capture.close",), ("consumer.discard", 1, 1), ("shutdown", False)]
    assert c.exit_code == 0


def test_run_returns_exit_code(c: Controller, w: World) -> None:
    c.events.put(ev.PttPressed(100.0))
    c.events.put(ev.ShutdownRequested())
    thread_result: list[int] = []
    t = threading.Thread(target=lambda: thread_result.append(c.run()))
    t.start()
    t.join(timeout=5)
    assert thread_result == [0]


def test_engine_ready_resumes_pipeline_and_notifies(w: World) -> None:
    c = make(w, engine=None)
    c.handle(ev.EngineStateChanged(EngineHealth.READY))
    assert w.calls == [("pipeline.resume",), ("notify", "engine", "Engine ready", True)]
    w.calls.clear()
    c.handle(ev.EngineStateChanged(EngineHealth.READY))
    assert w.calls == [("pipeline.resume",)]


def test_engine_down_pauses_pipeline(c: Controller, w: World) -> None:
    c.handle(ev.EngineStateChanged(EngineHealth.DOWN))
    assert w.calls == [("pipeline.pause",)]
    assert c.snapshot().paused
    assert c.display_status() == "ERROR: engine down"


def test_engine_change_does_not_interrupt_ptt(c: Controller, w: World) -> None:
    press(c, w)
    c.handle(ev.EngineStateChanged(EngineHealth.DOWN))
    assert c.mode is Mode.PTT_RECORDING


def test_status_follows_jobs(c: Controller, w: World) -> None:
    job = finish_job(c, w)
    assert c.snapshot().queued_jobs == 1
    assert c.snapshot().queued_audio_s == 2.0
    c.handle(ev.JobStarted(job.id))
    assert (c.snapshot().busy, c.snapshot().queued_jobs) == (True, 0)
    result = InjectResult(True, "clipboard", 10, "firefox", False, None)
    c.handle(ev.JobFinished(job.id, "ptt", result))
    assert w.statuses == ["RECORDING", "TRANSCRIBING (1 queued)", "TRANSCRIBING (0 queued)", "IDLE"]
    assert w.notifications() == []


def test_cancel_removes_drained_jobs_from_status(c: Controller, w: World) -> None:
    job = finish_job(c, w)
    w.cancel_result = CancelResult((job.id,), in_flight_cancelled=False, injection_in_flight=False)
    c.handle(ev.CancelRequested())
    assert c.display_status() == "IDLE"


def test_cancel_clears_busy_job_requeued_after_connection_error(c: Controller, w: World) -> None:
    job = finish_job(c, w)
    c.handle(ev.JobStarted(job.id))  # started, then requeued by the worker (04 §4.4)
    w.cancel_result = CancelResult((job.id,), in_flight_cancelled=False, injection_in_flight=False)
    c.handle(ev.CancelRequested())
    assert c.snapshot().busy is False
    assert c.display_status() == "IDLE"


@pytest.mark.parametrize(
    ("result", "title"),
    [
        (
            InjectResult(True, "clipboard", 5, None, True, None, no_target=True),
            "No active field — text is in the clipboard",
        ),
        (
            InjectResult(False, "clipboard", 0, "gedit", True, "no confirmation"),
            "Could not paste — text is in the clipboard (Ctrl+V)",
        ),
        (
            InjectResult(False, "type", 0, "xterm", False, "xdotool failed"),
            "Could not enter text",
        ),
    ],
)
def test_job_finished_notifications(
    c: Controller, w: World, result: InjectResult, title: str
) -> None:
    c.handle(ev.JobFinished(1, "ptt", result))
    assert w.notifications() == [title]


def test_job_failures_are_aggregated_within_10_s(c: Controller, w: World) -> None:
    c.handle(ev.JobFailed(1, "ptt", 4.2, "HTTP 500"))
    w.now += 5
    c.handle(ev.JobFailed(2, "ptt", 3.0, "HTTP 500"))
    w.now += 4
    c.handle(ev.JobFailed(3, "ptt", 3.0, "timeout"))
    w.now += 11
    c.handle(ev.JobFailed(4, "ptt", 6.0, "timeout"))
    assert w.notifications() == [
        "Could not transcribe segment (4 s)",
        "2 segments could not be transcribed",
        "3 segments could not be transcribed",
        "Could not transcribe segment (6 s)",
    ]
    assert {call[1] for call in w.calls} == {"job_failed"}  # one replaced notification


# --- reload (04 §4.6) --------------------------------------------------------------------


def with_changes(base: Config, **sections: dict[str, Any]) -> Config:
    return dataclasses.replace(
        base,
        **{
            name: dataclasses.replace(getattr(base, name), **values)
            for name, values in sections.items()
        },
    )


def test_config_diff_and_groups() -> None:
    new = with_changes(
        WHISPER,
        stt={"model": "small-q5_1", "vocabulary_prompt": "Gdańsk"},
        vad={"min_silence_ms": 500},
        logging={"level": "DEBUG"},
    )
    keys = config_diff(WHISPER, new)
    assert keys == ["stt.model", "stt.vocabulary_prompt", "vad.min_silence_ms", "logging.level"]
    assert [reload_group(k) for k in keys] == ["server", "live", "idle", "live"]


def test_reload_invalid_config_keeps_the_old_one(c: Controller, w: World) -> None:
    w.next_config = ConfigError(["stt.port: must be in 1-65535 (got 0)"])
    r = reply()
    c.handle(ev.ReloadRequested(r))
    assert r.result() == {"ok": False, "errors": ["stt.port: must be in 1-65535 (got 0)"]}
    assert w.calls == []
    assert c.config == WHISPER


def test_reload_without_changes(c: Controller, w: World) -> None:
    r = reply()
    c.handle(ev.ReloadRequested(r))
    assert r.result() == {"ok": True, "applied": [], "deferred": [], "server_restart": False}
    assert w.calls == []


def test_reload_live_keys_apply_immediately_even_during_ptt(c: Controller, w: World) -> None:
    press(c, w)
    w.next_config = with_changes(
        WHISPER, ptt={"min_duration_ms": 500}, stt={"vocabulary_prompt": "x"}
    )
    r = reply()
    c.handle(ev.ReloadRequested(r))
    assert w.names() == ["reload.live"]
    assert r.result()["applied"] == ["stt.vocabulary_prompt", "ptt.min_duration_ms"]
    assert c.config.ptt.min_duration_ms == 500
    assert c.config.stt.vocabulary_prompt == "x"


def test_reload_language_is_live_without_server_restart(c: Controller, w: World) -> None:
    # whisper-server v1.9.4 honours the per-request `language` (06 §6.5, tested 2026-10-05).
    press(c, w)
    w.next_config = with_changes(WHISPER, stt={"languages": ("en", "pl")})
    r = reply()
    c.handle(ev.ReloadRequested(r))
    assert w.names() == ["reload.live"]
    assert r.result() == {
        "ok": True,
        "applied": ["stt.languages"],
        "deferred": [],
        "server_restart": False,
    }
    assert c.config.stt.languages == ("en", "pl") and c.language == "en"


def test_reload_idle_keys_in_idle_apply_immediately(c: Controller, w: World) -> None:
    w.next_config = with_changes(WHISPER, hotkeys={"push_to_talk": "Pause"})
    r = reply()
    c.handle(ev.ReloadRequested(r))
    assert w.names() == ["reload.idle"]
    assert r.result()["applied"] == ["hotkeys.push_to_talk"]
    assert c.config.hotkeys.push_to_talk == "Pause"


def test_reload_idle_keys_during_ptt_wait_for_idle(c: Controller, w: World) -> None:
    rid, cid, op = record_and_release(c, w)
    w.next_config = with_changes(WHISPER, audio={"device": "usb-mic"})
    r = reply()
    c.handle(ev.ReloadRequested(r))
    assert r.result() == {
        "ok": True,
        "applied": [],
        "deferred": ["audio.device"],
        "server_restart": False,
    }
    assert w.calls == []
    assert c.config.audio.device == "default"
    c.handle(ev.RecordingFinished(rid, cid, op, clip(), "release"))
    assert w.names() == ["pipeline.submit", "sound", "reload.idle"]
    assert c.config.audio.device == "usb-mic"


def test_reload_server_keys_restart_when_idle_and_empty(c: Controller, w: World) -> None:
    new = with_changes(WHISPER, stt={"threads": 2})
    w.next_config = new
    r = reply()
    c.handle(ev.ReloadRequested(r))
    assert r.result() == {
        "ok": True,
        "applied": ["stt.threads"],
        "deferred": [],
        "server_restart": True,
    }
    assert w.calls == [("pipeline.pause",), ("reload.restart", new)]
    assert c.engine is EngineHealth.STARTING
    w.calls.clear()
    c.handle(ev.ServerRestartDone(0))
    assert w.calls == [("reload.use_server", new)]
    assert c.config.stt.threads == 2
    c.handle(ev.EngineStateChanged(EngineHealth.READY))
    assert ("pipeline.resume",) in w.calls
    assert "Failed to start engine with new config" not in w.notifications()


def test_restarted_server_gets_the_effective_config(c: Controller, w: World) -> None:
    # R3: use_server re-applies the live components, so it gets the server keys of the
    # restart and the live keys reloaded while it was running.
    w.next_config = with_changes(WHISPER, stt={"threads": 2})
    c.handle(ev.ReloadRequested())
    w.next_config = with_changes(WHISPER, stt={"threads": 2, "vocabulary_prompt": "x"})
    c.handle(ev.ReloadRequested())
    w.calls.clear()
    c.handle(ev.ServerRestartDone(0))
    expected = with_changes(WHISPER, stt={"threads": 2, "vocabulary_prompt": "x"})
    assert w.calls == [("reload.use_server", expected)]
    assert c.config == expected


def test_reload_server_restart_waits_for_queue_and_recording(c: Controller, w: World) -> None:
    job = finish_job(c, w)
    press(c, w, at=110.0)
    w.next_config = with_changes(WHISPER, stt={"model": "small-q5_1"})
    r = reply()
    c.handle(ev.ReloadRequested(r))
    assert r.result()["deferred"] == ["stt.model"]
    c.handle(ev.PttCancelKey())  # IDLE again, but one job is still queued
    assert "reload.restart" not in w.names()
    c.handle(ev.JobFinished(job.id, "ptt", InjectResult(True, "clipboard", 3, "x", False, None)))
    assert w.names()[-2:] == ["pipeline.pause", "reload.restart"]


def test_reload_server_restart_does_not_wait_when_engine_down(c: Controller, w: World) -> None:
    finish_job(c, w)
    c.handle(ev.EngineStateChanged(EngineHealth.DOWN))
    w.calls.clear()
    w.next_config = with_changes(WHISPER, stt={"port": 8179})
    c.handle(ev.ReloadRequested())
    assert w.names() == ["pipeline.pause", "reload.restart"]


def test_reload_models_dir_restarts_server_and_reloads_vad(c: Controller, w: World) -> None:
    w.next_config = with_changes(WHISPER, stt={"models_dir": WHISPER.stt.models_dir / "x"})
    c.handle(ev.ReloadRequested())
    assert w.names() == ["reload.idle", "pipeline.pause", "reload.restart"]


def test_restart_failure_is_reported(c: Controller, w: World) -> None:
    w.next_config = with_changes(WHISPER, stt={"threads": 2})
    c.handle(ev.ReloadRequested())
    c.handle(ev.ServerRestartDone(1))
    assert w.notifications() == ["Failed to start engine with new config"]


def test_restarted_server_going_down_before_ready_is_reported(c: Controller, w: World) -> None:
    w.next_config = with_changes(WHISPER, stt={"threads": 2})
    c.handle(ev.ReloadRequested())
    c.handle(ev.ServerRestartDone(0))
    c.handle(ev.EngineStateChanged(EngineHealth.DOWN))
    c.handle(ev.EngineStateChanged(EngineHealth.DOWN))
    assert w.notifications() == ["Failed to start engine with new config"]


def test_stray_server_restart_done_is_ignored(c: Controller, w: World) -> None:
    c.handle(ev.ServerRestartDone(0))
    assert w.calls == []


def test_reload_during_restart_waits_for_it(c: Controller, w: World) -> None:
    first = with_changes(WHISPER, stt={"threads": 2})
    second = with_changes(WHISPER, stt={"threads": 3})
    w.next_config = first
    c.handle(ev.ReloadRequested())
    w.next_config = second
    r = reply()
    c.handle(ev.ReloadRequested(r))
    assert r.result()["deferred"] == ["stt.threads"]
    assert [call for call in w.calls if call[0] == "reload.restart"] == [("reload.restart", first)]
    w.calls.clear()
    c.handle(ev.ServerRestartDone(0))
    assert w.calls == [
        ("reload.use_server", first),
        ("pipeline.pause",),
        ("reload.restart", second),
    ]


# --- status --json and statistics (10 §10.4) ----------------------------------------------


def status_of(c: Controller) -> dict[str, Any]:
    r = reply()
    c.handle(ev.StatusRequested(r))
    response = r.result()
    assert response["ok"] is True
    status: dict[str, Any] = response["status"]
    return status


def test_status_document(c: Controller, w: World) -> None:
    w.now = 160.0
    status = status_of(c)
    assert status == {
        "version": status["version"],
        "state": "IDLE",
        "mode": "IDLE",
        "speech": False,
        "reconnecting": False,
        "engine": {
            "state": "READY",
            "name": "whisper.cpp",
            "model": "small-q8_0",
            "port": 8178,
            "threads": 4,
        },
        "hotkeys": {
            "state": "OK",
            "push_to_talk": "Control_R",
            "continuous_toggle": "Shift+Control_R",
            "language_toggle": "Ctrl+Control_R",
            "problems": [],
        },
        "language": {"active": "pl", "languages": ["pl", "en"]},
        "audio": {"device": "default", "open": False, "overflows": 0},
        "pipeline": {
            "queued": 0,
            "queued_audio_s": 0,
            "busy": False,
            "paused": False,
            "generation": 7,
            "last": None,
        },
        "stats": {
            "jobs_ok": 0,
            "jobs_failed": 0,
            "jobs_filtered": 0,
            "jobs_non_latin": 0,
            "rtf_avg_10": None,
            "latency_avg_10_s": None,
        },
        "uptime_s": 60.0,
    }
    assert w.calls == []


def test_status_while_recording(c: Controller, w: World) -> None:
    press(c, w)
    status = status_of(c)
    assert (status["state"], status["mode"], status["audio"]["open"]) == (
        "RECORDING",
        "PTT_RECORDING",
        True,
    )


def test_status_counts_jobs_and_averages_last_ten(c: Controller, w: World) -> None:
    ok = InjectResult(True, "clipboard", 3, "x", False, None)
    for i in range(12):
        timings = {"audio": 2.0, "stt": 0.2 * (i + 1), "total": 1.0 + i}
        c.handle(ev.JobFinished(i + 1, "ptt", ok, timings, non_latin=i in (3, 7)))
    c.handle(ev.JobFinished(20, "ptt", InjectResult(False, "type", 0, None, False, "boom")))
    c.handle(ev.JobFailed(21, "ptt", 1.0, "timeout"))
    c.handle(ev.JobDiscarded(22, "ptt", "filtered"))
    c.handle(ev.JobDiscarded(23, "ptt", "no_speech"))
    c.handle(ev.JobDiscarded(24, "ptt", "cancelled"))
    w.now = 130.0
    status = status_of(c)
    assert status["stats"] == {
        "jobs_ok": 12,
        "jobs_failed": 2,
        "jobs_filtered": 2,
        "jobs_non_latin": 2,  # task 4.4: counted, still injected and counted as ok
        "rtf_avg_10": pytest.approx(sum(0.1 * (i + 1) for i in range(2, 12)) / 10),
        "latency_avg_10_s": pytest.approx(sum(1.0 + i for i in range(2, 12)) / 10),
    }
    assert status["pipeline"]["last"] == {
        "audio_s": 2.0,
        "stt_s": pytest.approx(2.4),
        "ago_s": 30.0,
    }


def test_status_hotkey_problems_from_startup_and_regrab(w: World) -> None:
    problem = HotkeyProblem("push_to_talk", "Control_R", "already grabbed by another client")
    c = Controller(
        WHISPER,
        capture=w,
        consumer=Consumer(w),
        pipeline=Pipeline(w),
        feedback=w,
        lifecycle=w,
        reload_target=Reload(w),
        load_config=w.load_config,
        hotkey_problems=[problem],
    )
    hotkeys = status_of(c)["hotkeys"]
    assert (hotkeys["state"], hotkeys["problems"]) == (
        "degraded",
        [{"hotkey": "push_to_talk", "value": "Control_R", "reason": problem.reason}],
    )
    w.next_config = with_changes(WHISPER, hotkeys={"push_to_talk": "F9"})
    c.handle(ev.ReloadRequested())  # applied at once in IDLE; the fake regrab succeeds
    hotkeys = status_of(c)["hotkeys"]
    assert (hotkeys["state"], hotkeys["push_to_talk"], hotkeys["problems"]) == ("OK", "F9", [])
    w.next_config = with_changes(WHISPER, hotkeys={"enabled": False})
    c.handle(ev.ReloadRequested())
    assert status_of(c)["hotkeys"]["state"] == "disabled"


# --- language switch (task 3.7) ----------------------------------------------------------

PL_EN = {"active": "pl", "languages": ["pl", "en"]}
EN = {"active": "en", "languages": ["pl", "en"]}


def test_language_cycles_through_the_list(c: Controller, w: World) -> None:
    r = reply()
    c.handle(ev.LanguageSwitch(reply=r))
    assert c.language == "en"
    assert w.calls == [("sound", "language_alt"), ("notify", "language", "Language: EN", False)]
    assert r.result() == {"ok": True, "language": EN}
    w.calls.clear()
    c.handle(ev.LanguageSwitch())
    assert c.language == "pl"
    assert w.sounds() == ["language"]  # one tone: back to the startup language
    assert c.status()["language"] == PL_EN


def test_language_cycle_of_three(w: World) -> None:
    c = make(w)
    w.next_config = with_changes(WHISPER, stt={"languages": ("pl", "en", "de")})
    c.handle(ev.ReloadRequested())
    w.calls.clear()
    seen = []
    for _ in range(4):
        c.handle(ev.LanguageSwitch())
        seen.append(c.language)
    assert seen == ["en", "de", "pl", "en"]
    assert w.sounds() == ["language_alt", "language_alt", "language", "language_alt"]


def test_language_set_to_a_code_and_rejects_others(c: Controller, w: World) -> None:
    c.handle(ev.LanguageSwitch("en"))
    c.handle(ev.LanguageSwitch("en"))  # already active: confirmed again, no change
    assert c.language == "en"
    assert w.sounds() == ["language_alt", "language_alt"]
    w.calls.clear()
    r = reply()
    c.handle(ev.LanguageSwitch("de", r))
    assert r.result() == {
        "ok": False,
        "error": "bad_language",
        "message": "language must be one of stt.languages: pl, en",
    }
    assert c.language == "en" and w.calls == []


def test_parakeet_rejects_language_switch(w: World) -> None:
    """Task 4.3: Parakeet has no language input, so the hotkey and the CLI do nothing."""
    c = make(w, config=with_changes(WHISPER, stt={"engine": "parakeet"}))
    for target in (None, "en"):
        r = reply()
        c.handle(ev.LanguageSwitch(target, r))
        assert r.result() == {
            "ok": False,
            "error": "language_unsupported",
            "message": "Parakeet detects the language itself; stt.languages is for whisper-server",
        }
    assert c.language == "pl"
    assert w.calls == [("notify", "language", "Language: automatic (Parakeet)", False)] * 2
    status = c.status()
    assert status["language"] == {"active": "auto", "languages": ["pl", "en"]}
    assert (status["engine"]["name"], status["engine"]["model"]) == (
        "parakeet",
        "parakeet-tdt-0.6b-v3-int8",
    )


def test_language_is_published_to_subscribers(w: World) -> None:
    published: list[dict[str, Any]] = []
    c = Controller(
        WHISPER,
        capture=w,
        consumer=Consumer(w),
        pipeline=Pipeline(w),
        feedback=w,
        lifecycle=w,
        reload_target=Reload(w),
        load_config=w.load_config,
        clock=lambda: w.now,
        on_publish=published.append,
    )
    c.handle(ev.LanguageSwitch())
    assert [m for m in published if m["event"] == "language"] == [
        {"event": "language", "language": EN}
    ]


def test_ptt_job_keeps_the_language_of_its_press(c: Controller, w: World) -> None:
    rid, cid, op = record_and_release(c, w)
    c.handle(ev.LanguageSwitch())  # switched before the recording was finalized
    c.handle(ev.RecordingFinished(rid, cid, op, clip(), "release"))
    assert w.jobs[-1].language == "pl"
    assert finish_job(c, w).language == "en"


def test_language_sound_is_skipped_while_the_microphone_records(c: Controller, w: World) -> None:
    press(c, w)
    c.handle(ev.LanguageSwitch())
    assert w.sounds() == []  # the tone would be recorded
    assert w.notifications() == ["Language: EN"]


def test_reload_of_the_languages_resets_the_active_language(c: Controller, w: World) -> None:
    c.handle(ev.LanguageSwitch())
    w.next_config = with_changes(WHISPER, stt={"vocabulary_prompt": "x"})
    c.handle(ev.ReloadRequested())
    assert c.language == "en"  # an unrelated reload keeps the switch
    w.next_config = with_changes(
        WHISPER, stt={"vocabulary_prompt": "x", "languages": ("pl", "en", "de")}
    )
    c.handle(ev.ReloadRequested())
    assert c.language == "pl"
    assert c.status()["language"] == {"active": "pl", "languages": ["pl", "en", "de"]}


# --- history (task 5.2) --------------------------------------------------------------------


def history_of(*texts: str) -> TranscriptHistory:
    history = TranscriptHistory(10)
    for text in texts:
        history.add(text)
    return history


def test_last_inserts_the_nth_newest_text_again(w: World) -> None:
    c = make(w, history=history_of("stary ", "nowy "))
    r1, r2 = reply(), reply()
    c.handle(ev.HistoryInsert(1, r1))
    c.handle(ev.HistoryInsert(2, r2))
    assert r1.result() == {"ok": True, "chars": 5}
    assert r2.result() == {"ok": True, "chars": 6}
    reinjected = [call[2] for call in w.calls if call[0] == "pipeline.reinject"]
    assert reinjected == ["nowy ", "stary "]


@pytest.mark.parametrize("n", [1, 3])
def test_last_without_such_text_is_rejected(w: World, n: int) -> None:
    c = make(w, history=history_of() if n == 1 else history_of("a ", "b "))
    r = reply()
    c.handle(ev.HistoryInsert(n, r))
    assert r.result()["error"] == "no_history"
    assert not [call for call in w.calls if call[0] == "pipeline.reinject"]


def test_last_is_rejected_while_recording(c: Controller, w: World) -> None:
    c = make(w, history=history_of("tekst "))
    press(c, w)
    r = reply()
    c.handle(ev.HistoryInsert(1, r))
    assert r.result()["error"] == "busy"
    assert not [call for call in w.calls if call[0] == "pipeline.reinject"]


def test_history_lists_texts_newest_first(w: World) -> None:
    c = make(w, history=history_of("a ", "b "))
    r = reply()
    c.handle(ev.HistoryRequested(r))
    assert r.result() == {"ok": True, "texts": ["b ", "a "]}


def test_reinserted_job_is_not_counted_as_a_dictation(w: World) -> None:
    c = make(w, history=history_of("tekst "))
    c.handle(ev.HistoryInsert(1, reply()))
    job_id = next(call[1] for call in w.calls if call[0] == "pipeline.reinject")
    result = InjectResult(True, "clipboard", 6, "gedit", False, None)
    c.handle(ev.JobFinished(job_id, "history", result, {"inject": 0.2}))
    assert c.status()["stats"]["jobs_ok"] == 0
