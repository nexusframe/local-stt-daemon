"""`local-stt daemon`: composition root, start/stop, signals (docs/02-architecture.md §2.2).

Startup order: logging → config and model files → X11 session → secret → components → IPC
socket (fails fast on a second instance) → component threads → hotkey grab → `READY=1`
(11 §11.5: the engine is not waited for) → controller thread. The main thread then only
handles signals (10 §10.3) until the controller returns its exit code; teardown runs in
reverse start order.

Exit codes: 0 clean shutdown or end of the X session (07 §7.4); 1 X11 connection failure at
startup, another instance (E15), crash of a critical thread (E14); 78 invalid config, missing
model or secret, non-X11 session (E1, E3; user decision 2026-10-03 for model and secret).
"""

import contextlib
import json
import logging
import os
import queue
import signal
import threading
from collections.abc import Callable, Mapping, MutableMapping
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, Any, Protocol

import numpy as np
from numpy.typing import NDArray

from local_stt import __version__, doctor
from local_stt import events as ev
from local_stt.config import Config, ConfigError, check_model_files, config_dir, load_config
from local_stt.interfaces import AudioCaptureControl, EngineHealth, SttEngine, Transcript
from local_stt.logging_setup import resolve_level, set_level, setup_logging
from local_stt.reload import switch_engine_unit

if TYPE_CHECKING:
    from local_stt.audio.capture import DeviceLostCallback, FrameSink
    from local_stt.sdnotify import SdNotifier

log = logging.getLogger("local_stt.controller")

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_CONFIG = 78  # EX_CONFIG: RestartPreventExitStatus=78 (11 §11.5)

# E14: an exception in one of these ends the process so systemd restarts it (02 §2.2).
CRITICAL_THREADS = frozenset(
    {"controller", "hotkeys", "audio-consumer", "pipeline", "clipboard-owner"}
)


class StartupError(Exception):
    """Startup cannot continue; `code` is the process exit code."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


# --- preflight -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Preflight:
    config: Config
    config_path: Path | None
    cli_level: str | None
    request_path: str


def preflight(
    config_path: Path | None,
    cli_level: str | None,
    *,
    environ: Mapping[str, str] = os.environ,
    run: doctor.Runner = doctor.run_command,
) -> Preflight:
    """Everything checked before a component is built; raises StartupError."""
    from local_stt.stt.whisper_server import read_request_path

    try:
        config, warnings = load_config(config_path, environ)
    except ConfigError as e:
        for error in e.errors:
            log.error("config error: %s", error)
        raise StartupError(EXIT_CONFIG, "invalid config") from e
    try:
        set_level(resolve_level(cli_level, config.logging.level, environ))
    except ValueError as e:
        raise StartupError(EXIT_CONFIG, str(e)) from e
    for warning in warnings:
        log.warning("config warning: %s", warning)
    missing = check_model_files(config)
    for error in missing:
        log.error("%s", error)
    if missing:
        raise StartupError(EXIT_CONFIG, "model file missing")

    kind = doctor.session_type(run, environ)
    if kind != "x11":
        raise StartupError(EXIT_CONFIG, f"unsupported session (only X11): {kind or 'unknown'}")
    if not environ.get("DISPLAY"):
        raise StartupError(EXIT_FAILURE, "DISPLAY is not set in an X11 session")

    secret = config_dir(environ) / "secret"
    try:
        request_path = read_request_path(secret)
    except (OSError, ValueError) as e:
        raise StartupError(
            EXIT_CONFIG, f"cannot read {secret}: {e} (run scripts/install.sh)"
        ) from e
    return Preflight(config, config_path, cli_level, request_path)


def startup_checks(
    config: Config, environ: Mapping[str, str], run: doctor.Runner, data_dir: Path
) -> list[str]:
    """The cheap `doctor` checks (user decision 2026-10-03), logged at INFO (12 §12.1)."""
    results = [
        doctor.check_manager_display(run),
        doctor.check_whisper_binary(data_dir / "bin", run),
        doctor.check_private_file(config_dir(environ) / "secret"),
        doctor.check_private_file(config_dir(environ) / "whisper-server.env"),
        doctor.check_port(config.stt.port, run),
    ]
    lines = []
    for r in results:
        if r.status is not doctor.Status.OK:
            hint = f" (→ {r.hint})" if r.hint else ""
            lines.append(f"doctor: {r.name} {r.status.value}: {r.detail}{hint}")
    return lines


# --- small adapters --------------------------------------------------------------------------


class SwitchableEngine:
    """`SttEngine` whose target is replaced after a server restart (04 §4.6, `use_server`)."""

    def __init__(self, engine: SttEngine):
        self._engine = engine
        self.name = engine.name

    def switch(self, engine: SttEngine) -> None:
        self._engine = engine  # one reference assignment: the pipeline reads it per request

    def health(self) -> EngineHealth:
        return self._engine.health()

    def transcribe(
        self,
        audio: NDArray[np.float32],
        *,
        sample_rate: int,
        language: str,
        prompt: str | None,
        timeout_s: float,
    ) -> Transcript:
        return self._engine.transcribe(
            audio, sample_rate=sample_rate, language=language, prompt=prompt, timeout_s=timeout_s
        )


def make_engine(config: Config, request_path: str) -> SttEngine:
    from local_stt.stt.parakeet import ParakeetEngine
    from local_stt.stt.whisper_server import WhisperServerEngine

    stt = config.stt
    if stt.engine == "parakeet":
        return ParakeetEngine(port=stt.port, request_path=request_path)
    return WhisperServerEngine(
        port=stt.port,
        request_path=request_path,
        model=stt.model,
        audio_ctx=stt.audio_ctx,
        audio_ctx_margin=stt.audio_ctx_margin,
    )


class Lifecycle:
    """`DaemonLifecycle`: ungrab hotkeys and close the IPC socket (04 §4.3 shutdown rows)."""

    def __init__(self, stop_hotkeys: Callable[[], None], stop_ipc: Callable[[], None]):
        self._stop_hotkeys = stop_hotkeys
        self._stop_ipc = stop_ipc

    def shutdown(self, *, x11_alive: bool) -> None:
        if x11_alive:  # after a lost connection the listener has already ended
            self._stop_hotkeys()
        self._stop_ipc()


class ThreadCrashHandler:
    """`threading.excepthook` (E14): critical threads end the process with code 1,
    restartable ones are restarted, any other thread is only logged."""

    def __init__(
        self,
        restarters: Mapping[str, Callable[[], None]],
        exit: Callable[[int], None] = os._exit,  # sys.exit in a thread ends only that thread
    ):
        self._restarters = dict(restarters)
        self._exit = exit

    def __call__(self, args: "threading.ExceptHookArgs") -> None:
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread is not None else "?"
        exc_info = (args.exc_type, args.exc_value, args.exc_traceback)
        if name in CRITICAL_THREADS:
            log.critical("unhandled exception in thread %s; exiting", name, exc_info=exc_info)  # type: ignore[arg-type]
            logging.shutdown()
            self._exit(EXIT_FAILURE)
            return
        restart = self._restarters.get(name)
        action = "restarting it" if restart is not None else "thread ended"
        log.critical("unhandled exception in thread %s; %s", name, action, exc_info=exc_info)  # type: ignore[arg-type]
        if restart is not None:
            restart()


SIGNAL_EVENTS: dict[int, Callable[[], ev.Event]] = {
    signal.SIGTERM: ev.ShutdownRequested,
    signal.SIGINT: ev.ShutdownRequested,
    signal.SIGHUP: ev.ReloadRequested,
}


class AudioSource(AudioCaptureControl, Protocol):
    """The microphone as the daemon holds it: `device` is replaced on reload (04 §4.6)."""

    device: str


# (frames, audio.device, on_device_lost) -> the source; FileAudioSource in E2E tests (14 §14.3)
CaptureFactory = Callable[["FrameSink", str, "DeviceLostCallback"], AudioSource]


def microphone(frames: "FrameSink", device: str, on_lost: "DeviceLostCallback") -> AudioSource:
    from local_stt.audio.capture import AudioCapture

    return AudioCapture(frames, device, on_device_lost=on_lost)


# (stt.engine, "start" | "restart") -> systemctl exit code (task 4.3)
EngineUnits = Callable[[str, str], int]


# --- the daemon ------------------------------------------------------------------------------


class Daemon:
    """Builds the components from a passed preflight and runs them until shutdown."""

    def __init__(
        self,
        pre: Preflight,
        notifier: "SdNotifier",
        *,
        capture: CaptureFactory = microphone,
        engine_units: EngineUnits | None = switch_engine_unit,
    ):
        """`engine_units=None` leaves the systemd engine units alone (E2E tests bring their
        own temporary server)."""
        self._pre = pre
        self.notifier = notifier
        self._capture_factory = capture
        self._engine_units = engine_units
        self._stack = contextlib.ExitStack()
        self._controller_thread: threading.Thread | None = None
        self.exit_code = EXIT_FAILURE  # replaced by the controller's own exit code

    def _post(self, event: ev.Event) -> None:
        self.controller.events.put(event)

    def build(self) -> None:
        """Constructs components; X11 connections are opened here (raises StartupError)."""
        from local_stt.audio.consumer import AudioConsumer
        from local_stt.audio.vad import VadTrimmer
        from local_stt.controller import Controller
        from local_stt.engine_monitor import EngineMonitor
        from local_stt.feedback import DesktopFeedback
        from local_stt.history import TranscriptHistory
        from local_stt.hotkeys.x11 import HotkeyConnectError, X11GrabHotkeys
        from local_stt.inject.auto import build_injector
        from local_stt.inject.clipboard import ClipboardOwner
        from local_stt.ipc import IpcServer, socket_path
        from local_stt.pipeline import PipelineWorker
        from local_stt.reload import ComponentReloader
        from local_stt.text.processor import DefaultTextProcessor

        pre, config, post = self._pre, self._pre.config, self._post
        self.frames: queue.SimpleQueue[Any] = queue.SimpleQueue()
        self.capture = self._capture_factory(
            self.frames,
            config.audio.device,
            lambda rid, cid, why: post(ev.AudioError(rid, cid, "device_lost", why)),
        )
        self.consumer = AudioConsumer(self.frames, post, max_duration_s=config.ptt.max_duration_s)
        self.consumer.update_vad(config)  # the Segmenter's own Silero session (05 §5.4)
        self.engine = SwitchableEngine(make_engine(config, pre.request_path))
        self.monitor = EngineMonitor(
            self.engine.health, post, startup_timeout_s=config.stt.startup_timeout_s
        )
        self.processor = DefaultTextProcessor(config)
        self.trimmer = VadTrimmer(config)  # the pipeline's own ONNX session (05 §5.3)
        try:
            self.hotkeys = X11GrabHotkeys()
            self.owner = ClipboardOwner(on_connection_lost=lambda: post(ev.X11ConnectionLost()))
            self.injector = build_injector(config, self.owner)
        except HotkeyConnectError as e:
            raise StartupError(EXIT_FAILURE, str(e)) from e
        except Exception as e:  # python-xlib raises several unrelated types on connect
            raise StartupError(EXIT_FAILURE, f"cannot connect to the X display: {e}") from e
        self.history = TranscriptHistory(config.history.size)  # RAM only (task 5.2)
        self.pipeline = PipelineWorker(
            engine=self.engine,
            processor=self.processor,
            injector=self.injector,
            post=post,
            report_connection_failure=self.monitor.report_connection_failure,
            config=config,
            trimmer=self.trimmer,
            history=self.history,
        )
        self.feedback = DesktopFeedback(config.feedback)
        self.ipc = IpcServer(socket_path(), post)

        def switch_server(new: Config) -> None:
            self.engine.switch(make_engine(new, pre.request_path))
            self.monitor.restarted(startup_timeout_s=new.stt.startup_timeout_s)

        def set_audio_device(new: Config) -> None:
            self.capture.device = new.audio.device  # read at the next open()

        reloader = ComponentReloader(
            post=post,
            live=[
                lambda c: set_level(resolve_level(pre.cli_level, c.logging.level)),
                self.processor.update_config,
                self.injector.update_config,
                self.pipeline.update_config,
                self.feedback.update_config,
                lambda c: self.consumer.set_max_duration(c.ptt.max_duration_s),
                lambda c: self.monitor.set_startup_timeout(c.stt.startup_timeout_s),
                lambda c: self.history.resize(c.history.size),
            ],
            # the injectors wait for the PTT key's release, so they follow hotkeys.* too
            at_idle=[
                set_audio_device,
                self.trimmer.update,
                self.consumer.update_vad,
                self.injector.update_config,
            ],
            hotkeys=self.hotkeys,
            switch_server=switch_server,
            restart=self._restart_engine_unit,
        )
        self.controller = Controller(
            config,
            capture=self.capture,
            consumer=self.consumer,
            pipeline=self.pipeline,
            feedback=self.feedback,
            lifecycle=Lifecycle(self.hotkeys.stop, self.ipc.stop),
            reload_target=reloader,
            load_config=lambda: load_config(pre.config_path),
            on_status=self._publish_status,
            on_publish=self.ipc.publish,
            history=self.history,
        )

    def start(self) -> None:
        """Starts threads in dependency order; each registers its stop for teardown."""
        from local_stt.ipc import AnotherInstanceRunning

        try:
            self.ipc.start()  # first: a second instance must not grab or open anything
        except AnotherInstanceRunning as e:
            raise StartupError(EXIT_FAILURE, str(e)) from e
        except OSError as e:
            raise StartupError(EXIT_FAILURE, f"cannot listen on {self.ipc.path}: {e}") from e
        stack = self._stack
        stack.callback(self.ipc.stop)
        for component in (self.feedback, self.owner, self.consumer, self.pipeline, self.monitor):
            component.start()
            stack.callback(component.stop)
        stack.callback(self.capture.close)  # before the consumer stops: no frames afterwards

        self.hotkeys.start(self._post)
        stack.callback(self.hotkeys.stop)
        problems = self.hotkeys.apply(self._pre.config.hotkeys)
        self.controller.hotkey_problems = list(problems)
        if problems:  # E4
            self.feedback.notify(
                "hotkeys",
                "Hotkey unavailable",
                ", ".join(f"{p.value}: {p.reason}" for p in problems),
            )

        # Not waited for, like the engine itself (11 §11.5): EngineMonitor reports READY.
        threading.Thread(
            target=self._run_engine_unit,
            args=(self._pre.config.stt.engine, "start"),
            name="engine-unit",
            daemon=True,
        ).start()

        threading.excepthook = ThreadCrashHandler(
            {"engine-monitor": self.monitor.start, "ipc-server": self.ipc.restart_thread}
        )
        self._controller_thread = threading.Thread(
            target=self._run_controller, name="controller", daemon=True
        )
        self._controller_thread.start()
        self.notifier.ready()

    def _run_engine_unit(self, engine: str, action: str) -> int:
        """Starts or restarts the selected engine's unit and stops the other (task 4.3)."""
        if self._engine_units is None:
            return 0
        code = self._engine_units(engine, action)
        if code != 0 and action == "start":
            log.error("cannot start the %s engine service (systemctl exit code %d)", engine, code)
        return code

    def _restart_engine_unit(self, engine: str) -> int:
        return self._run_engine_unit(engine, "restart")

    def _publish_status(self, text: str) -> None:
        self.notifier.status(text)  # STATUS= for systemctl --user status (11 §11.5)

    def _run_controller(self) -> None:
        self.exit_code = self.controller.run()

    def wait(self) -> int:
        """Waits for the controller in the main thread, so signal handlers can run."""
        assert self._controller_thread is not None
        self._controller_thread.join()
        return self.exit_code

    def stop(self) -> None:
        self.notifier.stopping()
        self._stack.close()

    # --- signals (10 §10.3) ------------------------------------------------------------------

    def install_signal_handlers(self) -> None:
        for signum in SIGNAL_EVENTS:
            signal.signal(signum, self._on_signal)
        signal.signal(signal.SIGUSR1, self._on_dump)

    def _on_signal(self, signum: int, frame: FrameType | None) -> None:
        log.info("received %s", signal.Signals(signum).name)
        self._post(SIGNAL_EVENTS[signum]())

    def _on_dump(self, signum: int, frame: FrameType | None) -> None:
        """SIGUSR1: internal state to the INFO log, for hang diagnostics."""
        threads = sorted(t.name for t in threading.enumerate())
        log.info(
            "state dump: threads=%s events=%d frames=%d",
            threads,
            self.controller.events.qsize(),
            self.frames.qsize(),
        )
        reply: Future[dict[str, Any]] = Future()
        reply.add_done_callback(lambda f: log.info("state dump: %s", json.dumps(f.result())))
        self._post(ev.StatusRequested(reply=reply))  # answered by the controller, if alive


def log_versions(config: Config) -> None:
    from local_stt.stt.whisper_server import DATA_DIR

    if config.stt.engine == "whisper-server":
        try:
            tag = (DATA_DIR / "bin/.whisper-tag").read_text(encoding="utf-8").strip()
        except OSError:
            tag = "unknown"
        engine = f"whisper.cpp {tag}"
    else:
        engine = config.stt.engine
    log.info(
        "local-stt %s starting (%s, model %s, audio device %s)",
        __version__,
        engine,
        config.stt.engine_model,
        config.audio.device,
    )
    if config.logging.log_text:
        log.warning("log_text is enabled — transcripts will be stored in the journal")


def take_notifier(environ: MutableMapping[str, str]) -> "SdNotifier":
    """Reads `$NOTIFY_SOCKET` once and removes it, like sd_notify's `unset_environment`.

    Child processes would inherit it otherwise, and systemd 255 tools (`loginctl`,
    `systemctl`) send `EXIT_STATUS=0` there on exit, which systemd logs as a warning for
    every call (tested 2026-10-03).
    """
    from local_stt.sdnotify import SdNotifier

    notifier = SdNotifier(environ)
    environ.pop("NOTIFY_SOCKET", None)
    return notifier


def run_daemon(config_path: Path | None, cli_level: str | None) -> int:
    setup_logging(logging.INFO)  # the configured level is applied once the config is read
    notifier = take_notifier(os.environ)
    try:
        pre = preflight(config_path, cli_level)
    except StartupError as e:
        log.error("%s", e)
        return e.code
    log_versions(pre.config)
    from local_stt.stt.whisper_server import DATA_DIR

    for line in startup_checks(pre.config, os.environ, doctor.run_command, DATA_DIR):
        log.info("%s", line)

    daemon = Daemon(pre, notifier)
    try:
        daemon.build()
        daemon.install_signal_handlers()
        daemon.start()
    except StartupError as e:
        log.error("%s", e)
        daemon.stop()
        return e.code
    try:
        code = daemon.wait()
    finally:
        daemon.stop()
    log.info("stopped (exit code %d)", code)
    return code
