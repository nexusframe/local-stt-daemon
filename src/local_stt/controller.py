"""Controller: the only owner of the daemon state (docs/04-state-machine.md).

All components post events to one queue; the controller thread handles them one at a time and
acts on components only through the interfaces in `interfaces.py`, so `handle()` is plain logic
that tests drive with fakes.

Modes IDLE and PTT_RECORDING (§4.3, task 1.3), the "Any state" rows and reload (§4.6), and
CONTINUOUS (task 2.3b): delayed microphone open, *Stop(flush)*, cancel, backlog, engine DOWN and
the microphone reconnect. Timers post events back into the queue through `schedule`.
"""

import dataclasses
import logging
import queue
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

from local_stt import __version__
from local_stt import events as ev
from local_stt.config import Config, ConfigError
from local_stt.interfaces import (
    AudioCaptureControl,
    AudioConsumerControl,
    AudioOpenError,
    CancelResult,
    Cut,
    DaemonLifecycle,
    EngineHealth,
    Feedback,
    HotkeyProblem,
    Job,
    PipelineControl,
    ReloadTarget,
)

log = logging.getLogger("local_stt.controller")

START_SOUND_MARGIN_S = 0.080  # masking continues this long after the start sound (05 §5.2)
CAPTURE_OPEN_DELAY_S = 0.150  # continuous: open the microphone after the start sound (10 §10.6)
RECONNECT_INTERVAL_S = 1.0  # between microphone reopen attempts (05 §5.6)
RECONNECT_ATTEMPTS = 3
SAMPLE_RATE = 16000
FAILURE_AGGREGATION_S = 10.0  # JobFailed notifications within this window are combined (04 §4.4)
STATS_WINDOW = 10  # rtf_avg_10 / latency_avg_10_s (10 §10.4)

# Reload groups (04 §4.6). Keys not listed here are applied live.
SERVER_KEYS = frozenset(
    f"stt.{k}"
    for k in (
        "engine", "model", "models_dir", "language", "threads", "beam_size", "port",
        "extra_server_args", "audio_ctx", "audio_ctx_margin",
    )
)  # fmt: skip
IDLE_SECTIONS = ("audio", "vad", "hotkeys")
ReloadGroup = Literal["live", "idle", "server"]


class Mode(Enum):
    IDLE = "IDLE"
    PTT_RECORDING = "PTT_RECORDING"
    CONTINUOUS = "CONTINUOUS"


def config_diff(old: Config, new: Config) -> list[str]:
    """Changed keys as `section.key`, in config order."""
    changed = []
    for section in dataclasses.fields(Config):
        before, after = getattr(old, section.name), getattr(new, section.name)
        for key in dataclasses.fields(before):
            if getattr(before, key.name) != getattr(after, key.name):
                changed.append(f"{section.name}.{key.name}")
    return changed


def reload_group(key: str) -> ReloadGroup:
    if key in SERVER_KEYS:
        return "server"
    if key.split(".", 1)[0] in IDLE_SECTIONS:
        return "idle"
    return "live"


@dataclass
class _PttRecording:
    recording_id: int
    capture_id: int
    pressed_at: float
    stopping: bool = False
    operation_id: int | None = None
    ended_at: float | None = None


@dataclass
class _Continuous:
    recording_id: int  # also the session_id
    capture_id: int
    start_reply: ev.Reply  # answered after the delayed open attempt
    opened: bool = False  # the stream is open (not before CaptureOpenDue, not while reconnecting)
    speech: bool = False
    stopping: bool = False
    reconnecting: bool = False
    reconnect_op: int | None = None  # the reconnect being run (flush, then ReconnectTicks)
    reconnect_flushing: bool = False  # its FlushDone has not arrived yet
    muted_notified: bool = False  # "Microphone appears to be muted" shown (05 §5.6)
    stop_op: int | None = None  # the stop flush, once requested


@dataclass(frozen=True)
class StatusSnapshot:
    """`DaemonState` as seen by `status` (04 §4.1, §4.7)."""

    mode: Mode
    engine: EngineHealth
    queued_jobs: int
    queued_audio_s: float
    busy: bool
    paused: bool
    display: str


def _ok(**fields: Any) -> dict[str, Any]:
    return {"ok": True, **fields}


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": code, "message": message}


class Controller:
    def __init__(
        self,
        config: Config,
        *,
        capture: AudioCaptureControl,
        consumer: AudioConsumerControl,
        pipeline: PipelineControl,
        feedback: Feedback,
        lifecycle: DaemonLifecycle,
        reload_target: ReloadTarget,
        load_config: Callable[[], tuple[Config, list[str]]],
        clock: Callable[[], float] = time.monotonic,
        schedule: Callable[[float, ev.Event], None] | None = None,
        on_status: Callable[[str], None] | None = None,
        on_publish: Callable[[dict[str, Any]], None] | None = None,
        hotkey_problems: Sequence[HotkeyProblem] = (),
    ):
        self.events: queue.Queue[ev.Event] = queue.Queue()
        self.config = config  # what components currently run with
        self._latest = config  # last accepted reload, including deferred parts
        self._capture = capture
        self._consumer = consumer
        self._pipeline = pipeline
        self._feedback = feedback
        self._lifecycle = lifecycle
        self._reload_target = reload_target
        self._load_config = load_config
        self._clock = clock
        self._schedule = schedule or self._timer
        self._on_status = on_status
        self._on_publish = on_publish  # IPC `subscribe` stream (10 §10.2)

        self.mode = Mode.IDLE
        self.engine = EngineHealth.STARTING
        self._rec: _PttRecording | None = None
        self._cont: _Continuous | None = None
        self._next_recording_id = 0
        self._next_capture_id = 0
        self._next_operation_id = 0
        self._next_job_id = 0

        self._outstanding: dict[int, float] = {}  # job_id → audio seconds, until reported done
        self._busy_job: int | None = None
        self._paused = False
        self._failures_since: float | None = None
        self._failure_count = 0

        self._pending_idle: Config | None = None
        self._pending_server: Config | None = None
        self._restarting: Config | None = None  # server restart requested, not yet done
        self._watch_restart = False  # E16 if the restarted server goes DOWN before READY

        # status (10 §10.4)
        self.hotkey_problems = list(hotkey_problems)  # from the startup grab, then each regrab
        self._started_at = clock()
        self._jobs_ok = 0
        self._jobs_failed = 0
        self._jobs_filtered = 0
        self._recent: deque[tuple[float, float]] = deque(maxlen=STATS_WINDOW)  # (rtf, total)
        self._last_job: tuple[float, float, float] | None = None  # audio_s, stt_s, finished at

        self._running = True
        self.exit_code: int | None = None
        self._last_status: str | None = None

        self._handlers: dict[type[Any], Callable[[Any], None]] = {
            ev.PttPressed: self._on_ptt_pressed,
            ev.PttReleased: self._on_ptt_released,
            ev.PttCancelKey: self._on_ptt_cancel_key,
            ev.ContinuousToggle: self._on_continuous_toggle,
            ev.CancelRequested: self._on_cancel,
            ev.RecordingStarted: self._on_recording_started,
            ev.RecordingLimitReached: self._on_recording_limit,
            ev.RecordingFinished: self._on_recording_finished,
            ev.CaptureOpenDue: self._on_capture_open_due,
            ev.SpeechStarted: self._on_speech,
            ev.SpeechEnded: self._on_speech,
            ev.SegmentReady: self._on_segment_ready,
            ev.MicrophoneSilent: self._on_microphone_silent,
            ev.FlushDone: self._on_flush_done,
            ev.ReconnectTick: self._on_reconnect_tick,
            ev.AudioError: self._on_audio_error,
            ev.EngineStateChanged: self._on_engine_state,
            ev.JobStarted: self._on_job_started,
            ev.JobFinished: self._on_job_finished,
            ev.JobDiscarded: self._on_job_discarded,
            ev.JobFailed: self._on_job_failed,
            ev.ReloadRequested: self._on_reload,
            ev.StatusRequested: self._on_status_requested,
            ev.ServerRestartDone: self._on_server_restart_done,
            ev.ShutdownRequested: self._on_shutdown,
            ev.X11ConnectionLost: self._on_x11_lost,
        }

    # --- loop ----------------------------------------------------------------------------

    def run(self) -> int:
        """Handles events until shutdown; returns the process exit code."""
        self._publish_status()
        while self._running:
            self.handle(self.events.get())
        assert self.exit_code is not None
        return self.exit_code

    def handle(self, event: ev.Event) -> None:
        self._handlers[type(event)](event)
        if self._running:
            self._apply_pending_reload()
        self._publish_status()

    # --- status (04 §4.7) ----------------------------------------------------------------

    def snapshot(self) -> StatusSnapshot:
        busy = self._busy_job is not None
        return StatusSnapshot(
            mode=self.mode,
            engine=self.engine,
            queued_jobs=len(self._outstanding) - (1 if busy else 0),
            queued_audio_s=sum(self._outstanding.values()),
            busy=busy,
            paused=self._paused,
            display=self.display_status(),
        )

    def display_status(self) -> str:
        queued = len(self._outstanding) - (1 if self._busy_job is not None else 0)
        if self.engine is EngineHealth.DOWN:
            return "ERROR: engine down"
        if self.engine is EngineHealth.STARTING:
            return "STARTING"
        if self.mode is Mode.PTT_RECORDING:
            return "RECORDING"
        if self._cont is not None:
            state = "LISTENING"
            if self._cont.reconnecting:
                state += " (reconnecting)"
            elif self._cont.speech:
                state += " (speech)"
            if self._outstanding:
                state += f", transcribing {len(self._outstanding)}"
            return state
        if self._busy_job is not None or queued > 0:
            return f"TRANSCRIBING ({queued} queued)"
        return "IDLE"

    def status(self) -> dict[str, Any]:
        """The `status --json` document (10 §10.4); never contains transcript text."""
        snap = self.snapshot()
        now = self._clock()
        cfg = self.config
        if not cfg.hotkeys.enabled:
            hotkeys_state = "disabled"
        else:
            hotkeys_state = "degraded" if self.hotkey_problems else "OK"
        recent = list(self._recent)
        last = None
        if self._last_job is not None:
            audio_s, stt_s, finished_at = self._last_job
            last = {"audio_s": audio_s, "stt_s": stt_s, "ago_s": now - finished_at}
        return {
            "version": __version__,
            "state": snap.display,
            "mode": snap.mode.value,
            "speech": self._cont is not None and self._cont.speech,
            "reconnecting": self._cont is not None and self._cont.reconnecting,
            "engine": {
                "state": snap.engine.value,
                "name": "whisper.cpp",
                "model": cfg.stt.model,
                "port": cfg.stt.port,
                "threads": cfg.stt.threads,
            },
            "hotkeys": {
                "state": hotkeys_state,
                "push_to_talk": cfg.hotkeys.push_to_talk,
                "continuous_toggle": cfg.hotkeys.continuous_toggle,
                "problems": [dataclasses.asdict(p) for p in self.hotkey_problems],
            },
            "audio": {
                "device": cfg.audio.device,
                "open": snap.mode is Mode.PTT_RECORDING
                or (self._cont is not None and self._cont.opened),
                "overflows": self._consumer.overflows,
            },
            "pipeline": {
                "queued": snap.queued_jobs,
                "queued_audio_s": snap.queued_audio_s,
                "busy": snap.busy,
                "paused": snap.paused,
                "generation": self._pipeline.generation,
                "last": last,
            },
            "stats": {
                "jobs_ok": self._jobs_ok,
                "jobs_failed": self._jobs_failed,
                "jobs_filtered": self._jobs_filtered,
                "rtf_avg_10": sum(r for r, _ in recent) / len(recent) if recent else None,
                "latency_avg_10_s": sum(t for _, t in recent) / len(recent) if recent else None,
            },
            "uptime_s": now - self._started_at,
        }

    def _on_status_requested(self, event: ev.StatusRequested) -> None:
        self._respond(event.reply, _ok(status=self.status()))

    def _publish_status(self) -> None:
        status = self.display_status()
        if status != self._last_status:
            self._last_status = status
            if self._on_status is not None:
                self._on_status(status)
            if self._on_publish is not None:
                self._on_publish({"event": "state", "status": self.status()})

    def _publish_job(
        self,
        job_id: int,
        source: str,
        result: str,
        *,
        audio_s: float | None,
        processing_s: float | None = None,
        chars: int = 0,
    ) -> None:
        """A `job` event for subscribers (10 §10.2); never contains text."""
        if self._on_publish is not None:
            self._on_publish(
                {
                    "event": "job",
                    "job_id": job_id,
                    "source": source,
                    "audio_s": audio_s,
                    "processing_s": processing_s,
                    "chars": chars,
                    "result": result,
                }
            )

    # --- helpers -------------------------------------------------------------------------

    @staticmethod
    def _respond(reply: ev.Reply, payload: dict[str, Any]) -> None:
        if reply is not None and not reply.done():
            reply.set_result(payload)

    def _unhandled(self, event: ev.Event) -> None:
        """Default for (mode, event) pairs not in the transition table (04 §4.3)."""
        reply: ev.Reply = getattr(event, "reply", None)
        if reply is not None:
            name = type(event).__name__
            self._respond(reply, _error("invalid_in_mode", f"{name} in {self.mode.value}"))
        else:
            log.debug("ignored %s in %s", type(event).__name__, self.mode.value)

    def _stale(self, event: ev.Event) -> None:
        log.debug("ignored stale %s", event)

    def _matches(self, recording_id: int, capture_id: int) -> bool:
        rec = self._rec
        return (
            rec is not None
            and self.mode is Mode.PTT_RECORDING
            and (rec.recording_id, rec.capture_id) == (recording_id, capture_id)
        )

    def _timer(self, delay_s: float, event: ev.Event) -> None:
        timer = threading.Timer(delay_s, self.events.put, args=(event,))
        timer.daemon = True
        timer.start()

    def _new_id(self, kind: Literal["recording", "capture", "operation", "job"]) -> int:
        attr = f"_next_{kind}_id"
        value: int = getattr(self, attr) + 1
        setattr(self, attr, value)
        return value

    def _drop_recording(self) -> None:
        """Invalidates the PTT recording and any finalization in progress (04 §4.3)."""
        rec = self._rec
        assert rec is not None
        self._rec = None
        self.mode = Mode.IDLE
        self._capture.close()
        self._consumer.discard(rec.recording_id, rec.capture_id)

    def _cancel_pipeline(self) -> CancelResult:
        result = self._pipeline.cancel_all()
        for job_id in result.drained_job_ids:
            # A drained job may already have started: requeued after a connection error.
            self._job_done(job_id)
        return result

    def _audio_failure(self, description: str) -> None:
        log.error("microphone error: %s", description)
        self._drop_recording()
        self._feedback.play("error")
        self._feedback.notify("audio", "Microphone error", description)

    # --- PTT (04 §4.3: IDLE, PTT_RECORDING) ---------------------------------------------

    def _on_ptt_pressed(self, event: ev.PttPressed) -> None:
        if self.mode is not Mode.IDLE:
            return self._unhandled(event)
        if self.engine is not EngineHealth.READY:
            starting = self.engine is EngineHealth.STARTING
            self._feedback.play("error")
            self._feedback.notify(
                "engine",
                "STT engine unavailable",
                "The engine is still starting." if starting else "Run: local-stt doctor",
            )
            code = "engine_starting" if starting else "engine_down"
            return self._respond(event.reply, _error(code, "STT engine unavailable"))

        rec = _PttRecording(self._new_id("recording"), self._new_id("capture"), event.at)
        self._rec = rec
        self.mode = Mode.PTT_RECORDING
        self._consumer.begin_ptt(rec.recording_id, rec.capture_id)
        try:
            self._capture.open(rec.recording_id, rec.capture_id)
        except AudioOpenError as e:
            self._audio_failure(str(e))
            return self._respond(event.reply, _error("audio_error", str(e)))
        self._respond(event.reply, _ok())

    def _on_recording_started(self, event: ev.RecordingStarted) -> None:
        if not self._matches(event.recording_id, event.capture_id):
            return self._stale(event)
        assert self._rec is not None
        if self._rec.stopping:
            return self._unhandled(event)
        duration = self._feedback.play("start")
        if duration is not None:  # no sound, no masking window (05 §5.2)
            until = self._clock() + duration + START_SOUND_MARGIN_S
            self._consumer.mask_start_sound(event.recording_id, event.capture_id, until)

    def _on_ptt_released(self, event: ev.PttReleased) -> None:
        rec = self._rec
        if self.mode is not Mode.PTT_RECORDING or rec is None or rec.stopping:
            return self._unhandled(event)
        held_ms = round((event.at - rec.pressed_at) * 1000, 3)  # 100.3 - 100.0 = 0.2999…
        if held_ms < self.config.ptt.min_duration_ms:
            log.debug("ptt too short (%.0f ms)", held_ms)
            self._drop_recording()
        else:
            self._finish_ptt(event.at, "release")
        self._respond(event.reply, _ok())

    def _on_recording_limit(self, event: ev.RecordingLimitReached) -> None:
        if not self._matches(event.recording_id, event.capture_id):
            return self._stale(event)
        assert self._rec is not None
        if self._rec.stopping:
            return self._unhandled(event)
        self._finish_ptt(event.ended_at, "max_duration")
        self._feedback.notify(
            "limit",
            "Recording limit reached",
            f"PTT recordings are limited to {self.config.ptt.max_duration_s:g} s.",
        )

    def _finish_ptt(self, ended_at: float, cut: Cut) -> None:
        """*Finish PTT* (04 §4.3): close capture first, then ask the consumer to finalize."""
        rec = self._rec
        assert rec is not None
        rec.stopping = True
        rec.ended_at = ended_at
        rec.operation_id = self._new_id("operation")
        self._capture.close()  # returns after the last callback
        self._consumer.finish_ptt(rec.recording_id, rec.capture_id, rec.operation_id, ended_at, cut)

    def _on_recording_finished(self, event: ev.RecordingFinished) -> None:
        rec = self._rec
        if (
            not self._matches(event.recording_id, event.capture_id)
            or rec is None
            or not rec.stopping
            or rec.operation_id != event.operation_id
        ):
            return self._stale(event)
        assert rec.ended_at is not None
        job = Job(
            id=self._new_id("job"),
            source="ptt",
            audio=event.clip.samples,
            ended_at=rec.ended_at,
            generation=self._pipeline.generation,
            session_id=None,
            seq=None,
            cut=event.cut,
        )
        self._rec = None
        self.mode = Mode.IDLE
        self._pipeline.submit(job)
        self._outstanding[job.id] = event.clip.duration_s
        self._feedback.play("stop")

    def _on_ptt_cancel_key(self, event: ev.PttCancelKey) -> None:
        if self.mode is not Mode.PTT_RECORDING:
            return self._unhandled(event)
        self._drop_recording()  # earlier pipeline jobs remain
        self._feedback.play("cancel")

    def _on_cancel(self, event: ev.CancelRequested) -> None:
        if self.mode is Mode.PTT_RECORDING:
            self._drop_recording()
            result = self._cancel_pipeline()
            self._feedback.play("cancel")
        elif self.mode is Mode.CONTINUOUS:
            self._drop_continuous()
            result = self._cancel_pipeline()
            self._feedback.play("cancel")
        else:
            result = self._cancel_pipeline()
            if result.discarded_any:
                self._feedback.play("cancel")
        self._respond(event.reply, _ok(injection_in_flight=result.injection_in_flight))

    def _on_audio_error(self, event: ev.AudioError) -> None:
        if self._cont_matches(event.recording_id, event.capture_id):
            if event.kind == "device_lost":
                return self._on_device_lost(event)
            return self._unhandled(event)  # open failures are handled where open() is called
        if not self._matches(event.recording_id, event.capture_id):
            return self._stale(event)
        self._audio_failure(event.description)

    def _on_continuous_toggle(self, event: ev.ContinuousToggle) -> None:
        if self.mode is Mode.CONTINUOUS:
            cont = self._cont
            assert cont is not None
            if cont.stopping:
                return self._unhandled(event)
            self._stop_continuous()
            self._feedback.play("stop")
            return self._respond(event.reply, _ok())
        if self.mode is not Mode.IDLE:
            return self._unhandled(event)
        if (rejection := self._continuous_rejection()) is not None:
            self._feedback.play("error")
            code, title, body = rejection
            self._feedback.notify("continuous", title, body)
            return self._respond(event.reply, _error(code, title))

        # The start sound plays before the microphone opens (10 §10.6); the IPC reply waits
        # for the open attempt.
        self._feedback.play("start")
        cont = _Continuous(self._new_id("recording"), self._new_id("capture"), event.reply)
        self._cont = cont
        self.mode = Mode.CONTINUOUS
        self._consumer.reset_continuous(cont.recording_id, cont.capture_id)
        self._schedule(CAPTURE_OPEN_DELAY_S, ev.CaptureOpenDue(cont.recording_id))

    def _continuous_rejection(self) -> tuple[str, str, str] | None:
        if self.engine is EngineHealth.STARTING:
            return "engine_starting", "STT engine unavailable", "The engine is still starting."
        if self.engine is not EngineHealth.READY:
            return "engine_down", "STT engine unavailable", "Run: local-stt doctor"
        if not self.config.vad.enabled:
            return "vad_disabled", "Continuous dictation needs VAD", "Set vad.enabled = true."
        if not self._consumer.continuous_available:  # vad.enabled, but no model (04 §4.3)
            return "vad_disabled", "VAD model unavailable", "Run: local-stt doctor"
        return None

    def _cont_matches(self, recording_id: int, capture_id: int | None = None) -> bool:
        cont = self._cont
        return (
            cont is not None
            and self.mode is Mode.CONTINUOUS
            and cont.recording_id == recording_id
            and (capture_id is None or cont.capture_id == capture_id)
        )

    def _on_capture_open_due(self, event: ev.CaptureOpenDue) -> None:
        cont = self._cont
        if not self._cont_matches(event.recording_id) or cont is None or cont.opened:
            return self._stale(event)
        if cont.stopping:
            return self._unhandled(event)
        try:
            self._capture.open(cont.recording_id, cont.capture_id)
        except AudioOpenError as e:
            log.error("microphone error: %s", e)
            self._respond(cont.start_reply, _error("audio_error", str(e)))
            self._drop_continuous()
            self._feedback.play("error")
            self._feedback.notify("audio", "Microphone error", str(e))
            return None
        cont.opened = True
        log.info("continuous dictation started (session %d)", cont.recording_id)
        self._feedback.notify("dictation", "Dictation enabled", informational=True)
        self._respond(cont.start_reply, _ok())

    def _on_speech(self, event: ev.SpeechStarted | ev.SpeechEnded) -> None:
        if not self._cont_matches(event.recording_id, event.capture_id):
            return self._stale(event)
        assert self._cont is not None
        self._cont.speech = isinstance(event, ev.SpeechStarted)

    def _on_segment_ready(self, event: ev.SegmentReady) -> None:
        cont = self._cont
        if (
            not self._cont_matches(event.recording_id, event.capture_id)
            or cont is None
            or event.operation_id not in (None, cont.reconnect_op, cont.stop_op)
        ):
            return self._stale(event)
        segment = event.segment
        job = Job(
            id=self._new_id("job"),
            source="continuous",
            audio=segment.samples,
            ended_at=segment.ended_at,
            generation=self._pipeline.generation,
            session_id=segment.session_id,
            seq=segment.seq,
            cut=segment.cut,
        )
        self._pipeline.submit(job)
        self._outstanding[job.id] = len(segment.samples) / SAMPLE_RATE
        backlog = sum(self._outstanding.values())
        if backlog > self.config.continuous.max_backlog_s and not cont.stopping:
            log.warning("backlog %.0f s of audio: stopping continuous dictation", backlog)
            self._stop_continuous()
            self._feedback.play("stop")
            self._feedback.play("error")
            self._feedback.notify("continuous", "Transcription cannot keep up — dictation stopped")

    def _on_microphone_silent(self, event: ev.MicrophoneSilent) -> None:
        cont = self._cont
        if not self._cont_matches(event.recording_id, event.capture_id) or cont is None:
            return self._stale(event)
        if cont.muted_notified or cont.stopping:
            return None
        cont.muted_notified = True
        self._feedback.notify(
            "audio", "Microphone appears to be muted", "Check the system sound settings."
        )

    def _stop_continuous(self) -> None:
        """*Stop(flush)* (04 §4.3): the session ends at the matching `FlushDone(stop)`."""
        cont = self._cont
        assert cont is not None and not cont.stopping
        self._capture.close()  # returns after the last callback
        cont.opened = False
        cont.stopping = True
        self._respond(cont.start_reply, _error("cancelled", "continuous start was cancelled"))
        if not cont.reconnect_flushing:  # otherwise requested after that flush's FlushDone
            self._request_stop_flush()

    def _request_stop_flush(self) -> None:
        cont = self._cont
        assert cont is not None
        cont.stop_op = self._new_id("operation")
        self._consumer.flush(
            cont.recording_id, cont.capture_id, cont.stop_op, "stop", self._clock()
        )

    def _on_flush_done(self, event: ev.FlushDone) -> None:
        cont = self._cont
        if not self._cont_matches(event.recording_id, event.capture_id) or cont is None:
            return self._stale(event)
        if event.purpose == "stop" and cont.stopping and event.operation_id == cont.stop_op:
            log.info("continuous dictation stopped (session %d)", cont.recording_id)
            self._cont = None
            self.mode = Mode.IDLE
            self._feedback.notify("dictation", "Dictation disabled", informational=True)
            return None
        if (
            event.purpose == "reconnect"
            and cont.reconnect_flushing
            and event.operation_id == cont.reconnect_op
        ):
            cont.reconnect_flushing = False
            if cont.stopping:
                return self._request_stop_flush()
            return self._schedule(
                RECONNECT_INTERVAL_S, ev.ReconnectTick(cont.recording_id, event.operation_id, 1)
            )
        return self._stale(event)

    def _on_device_lost(self, event: ev.AudioError) -> None:
        cont = self._cont
        assert cont is not None
        if cont.reconnecting or cont.stopping:
            return self._unhandled(event)
        log.warning("microphone lost: %s; reconnecting", event.description)
        self._capture.close()
        cont.opened = False
        cont.speech = False
        cont.reconnecting = cont.reconnect_flushing = True
        cont.reconnect_op = self._new_id("operation")
        self._consumer.flush(
            cont.recording_id, cont.capture_id, cont.reconnect_op, "reconnect", self._clock()
        )

    def _on_reconnect_tick(self, event: ev.ReconnectTick) -> None:
        cont = self._cont
        if (
            not self._cont_matches(event.recording_id)
            or cont is None
            or not cont.reconnecting
            or cont.reconnect_flushing
            or event.operation_id != cont.reconnect_op
        ):
            return self._stale(event)
        if cont.stopping:
            return self._unhandled(event)
        cont.capture_id = self._new_id("capture")
        # The same session_id keeps the next seq (05 §5.5, rule 8).
        self._consumer.reset_continuous(cont.recording_id, cont.capture_id)
        try:
            self._capture.open(cont.recording_id, cont.capture_id)
        except AudioOpenError as e:
            log.warning("microphone reopen attempt %d failed: %s", event.attempt, e)
            if event.attempt < RECONNECT_ATTEMPTS:
                return self._schedule(
                    RECONNECT_INTERVAL_S,
                    ev.ReconnectTick(cont.recording_id, event.operation_id, event.attempt + 1),
                )
            log.error("microphone lost: continuous dictation stopped")
            self._stop_continuous()
            self._feedback.play("stop")
            self._feedback.play("error")
            self._feedback.notify("audio", "Microphone lost — dictation stopped", str(e))
            return None
        cont.opened = True
        cont.reconnecting = False
        cont.reconnect_op = None
        log.info("microphone reconnected (attempt %d)", event.attempt)

    def _drop_continuous(self, *, notify: bool = True) -> None:
        """Invalidates the session and its operations; nothing more is submitted."""
        cont = self._cont
        assert cont is not None
        self._cont = None
        self.mode = Mode.IDLE
        self._capture.close()
        self._consumer.discard(cont.recording_id, cont.capture_id)
        self._respond(cont.start_reply, _error("cancelled", "continuous start was cancelled"))
        if notify and cont.opened:
            self._feedback.notify("dictation", "Dictation disabled", informational=True)

    # --- engine and jobs ("Any state") ---------------------------------------------------

    def _on_engine_state(self, event: ev.EngineStateChanged) -> None:
        previous, self.engine = self.engine, event.state
        if event.state is EngineHealth.READY:
            self._paused = False
            self._pipeline.resume()
            self._watch_restart = False
            if previous is not EngineHealth.READY:
                log.info("engine ready: %s", self.config.stt.model)
                self._feedback.notify(
                    "engine", "Engine ready", self.config.stt.model, informational=True
                )
        elif event.state is EngineHealth.DOWN:
            self._paused = True
            self._pipeline.pause()
            if previous is not EngineHealth.DOWN:
                log.error("engine down")
            if self._watch_restart:
                self._watch_restart = False
                self._restart_failed("the server did not become ready")
            if self._cont is not None and not self._cont.stopping:
                self._stop_continuous()  # segments wait in the paused queue (04 §4.5)
                self._feedback.play("stop")
                self._feedback.play("error")
                self._feedback.notify(
                    "continuous", "STT engine stopped working — dictation stopped"
                )

    def _on_job_started(self, event: ev.JobStarted) -> None:
        self._busy_job = event.job_id

    def _job_done(self, job_id: int) -> None:
        self._outstanding.pop(job_id, None)
        if self._busy_job == job_id:
            self._busy_job = None

    def _on_job_finished(self, event: ev.JobFinished) -> None:
        audio_s = self._outstanding.get(event.job_id)
        self._job_done(event.job_id)
        result = event.result
        outcome = "clipboard" if result.left_in_clipboard else "injected" if result.ok else "failed"
        self._publish_job(
            event.job_id,
            event.source,
            outcome,
            audio_s=event.timings.get("audio", audio_s),
            processing_s=event.timings.get("stt"),
            chars=result.chars,
        )
        if result.ok:
            self._jobs_ok += 1
        else:
            self._jobs_failed += 1
        t = event.timings
        if result.ok and {"audio", "stt", "total"} <= t.keys() and t["audio"] > 0:
            self._recent.append((t["stt"] / t["audio"], t["total"]))
            self._last_job = (t["audio"], t["stt"], self._clock())
        # The injector never notifies by itself (08 §8.5); messages never contain the text.
        if result.left_in_clipboard:
            title = (
                "No active field — text is in the clipboard"
                if result.no_target
                else "Could not paste — text is in the clipboard (Ctrl+V)"
            )
            self._feedback.notify("clipboard", title)
        elif not result.ok:
            self._feedback.notify("clipboard", "Could not enter text", result.error or "")

    def _on_job_discarded(self, event: ev.JobDiscarded) -> None:
        audio_s = self._outstanding.get(event.job_id)
        self._job_done(event.job_id)
        self._publish_job(event.job_id, event.source, event.reason, audio_s=audio_s)
        if event.reason in ("no_speech", "filtered"):
            self._jobs_filtered += 1
        if event.reason == "no_speech" and event.source == "ptt" and self.mode is Mode.IDLE:
            self._feedback.play("cancel")

    def _on_job_failed(self, event: ev.JobFailed) -> None:
        self._job_done(event.job_id)
        self._publish_job(event.job_id, event.source, "failed", audio_s=event.audio_s)
        self._jobs_failed += 1
        log.error("job %d failed (%.1f s of audio): %s", event.job_id, event.audio_s, event.error)
        now = self._clock()
        if self._failures_since is not None and now - self._failures_since <= FAILURE_AGGREGATION_S:
            self._failure_count += 1
            title = f"{self._failure_count} segments could not be transcribed"
        else:
            self._failures_since, self._failure_count = now, 1
            title = f"Could not transcribe segment ({event.audio_s:.0f} s)"
        self._feedback.notify("job_failed", title)

    # --- reload (04 §4.6) ----------------------------------------------------------------

    def _on_reload(self, event: ev.ReloadRequested) -> None:
        try:
            new, warnings = self._load_config()
        except ConfigError as e:
            log.error("reload rejected, keeping the current config: %s", "; ".join(e.errors))
            return self._respond(event.reply, {"ok": False, "errors": e.errors})
        for warning in warnings:
            log.warning("config: %s", warning)

        keys = config_diff(self._latest, new)
        self._latest = new
        groups = {key: reload_group(key) for key in keys}
        live = [k for k, g in groups.items() if g == "live"]
        idle = [k for k, g in groups.items() if g == "idle"]
        server = [k for k, g in groups.items() if g == "server"]
        applied: list[str] = list(live)
        deferred: list[str] = []

        if live:
            self._reload_target.apply_live(new)
            self.config = self._with_live(self.config, new)
        # models_dir also moves the VAD model, which is applied like the idle group.
        if idle or "stt.models_dir" in server:
            self._pending_idle = new
            (applied if self.mode is Mode.IDLE else deferred).extend(idle)
        if server:
            self._pending_server = new
            (applied if self._can_restart_server() else deferred).extend(server)

        log.info(
            "reload: applied=%s deferred=%s server_restart=%s", applied, deferred, bool(server)
        )
        self._respond(
            event.reply,
            _ok(applied=applied, deferred=deferred, server_restart=bool(server)),
        )

    @staticmethod
    def _with_live(base: Config, new: Config) -> Config:
        live_stt = {
            f.name: getattr(new.stt, f.name)
            for f in dataclasses.fields(new.stt)
            if f"stt.{f.name}" not in SERVER_KEYS
        }
        return dataclasses.replace(
            new,
            stt=dataclasses.replace(base.stt, **live_stt),
            audio=base.audio,
            vad=base.vad,
            hotkeys=base.hotkeys,
        )

    def _can_restart_server(self) -> bool:
        if self._restarting is not None:
            return False
        idle_and_empty = self.mode is Mode.IDLE and not self._outstanding
        return idle_and_empty or self.engine is EngineHealth.DOWN

    def _apply_pending_reload(self) -> None:
        if self._pending_idle is not None and self.mode is Mode.IDLE:
            new, self._pending_idle = self._pending_idle, None
            self.hotkey_problems = list(self._reload_target.apply_at_idle(new))
            self.config = dataclasses.replace(
                self.config, audio=new.audio, vad=new.vad, hotkeys=new.hotkeys
            )
            log.info("reload: applied deferred audio/vad/hotkeys settings")
        if self._pending_server is not None and self._can_restart_server():
            new, self._pending_server = self._pending_server, None
            self._restarting = new
            self.engine = EngineHealth.STARTING
            self._paused = True
            self._pipeline.pause()
            log.info("reload: restarting whisper-server")
            self._reload_target.restart_server(new)

    def _on_server_restart_done(self, event: ev.ServerRestartDone) -> None:
        new, self._restarting = self._restarting, None
        if new is None:
            return self._stale(event)
        # Only the server keys come from `new`: live stt keys may have been reloaded since.
        server_stt = {
            f.name: getattr(new.stt, f.name)
            for f in dataclasses.fields(new.stt)
            if f"stt.{f.name}" in SERVER_KEYS
        }
        self.config = dataclasses.replace(
            self.config, stt=dataclasses.replace(self.config.stt, **server_stt)
        )
        self._reload_target.use_server(self.config)
        self.engine = EngineHealth.STARTING  # the pipeline resumes on READY
        if event.exit_code != 0:
            self._restart_failed(f"systemctl exited with code {event.exit_code}")
        else:
            self._watch_restart = True

    def _restart_failed(self, reason: str) -> None:
        """E16: the env file has already changed, so the old config is not restored."""
        log.error("engine restart with the new config failed: %s", reason)
        self._feedback.notify(
            "engine", "Failed to start engine with new config", "Run: local-stt doctor"
        )

    # --- shutdown ("Any state") ----------------------------------------------------------

    def _on_shutdown(self, event: ev.ShutdownRequested) -> None:
        log.info("shutting down")
        if self._rec is not None:
            self._drop_recording()
        if self._cont is not None:
            self._drop_continuous(notify=False)
        self._cancel_pipeline()
        self._lifecycle.shutdown(x11_alive=True)
        self._stop(0)

    def _on_x11_lost(self, event: ev.X11ConnectionLost) -> None:
        log.warning("X11 connection lost; the session is ending")
        if self._rec is not None:
            self._drop_recording()
        if self._cont is not None:
            self._drop_continuous(notify=False)  # no X11: notify-send would fail anyway
        self._lifecycle.shutdown(x11_alive=False)
        self._stop(0)

    def _stop(self, code: int) -> None:
        self._running = False
        self.exit_code = code
