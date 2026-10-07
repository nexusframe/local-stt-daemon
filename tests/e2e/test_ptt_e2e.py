"""The whole daemon, PTT mode (docs/14-tests.md §14.3 item 3).

The daemon runs in this process, built by `app.Daemon` like `local-stt daemon` builds it,
with a `FileAudioSource` playing `pl_short.wav` in place of the microphone, a private Xvfb
(DISPLAY points to it only inside the test), a focused receiving window, and a real
`whisper-server` with base-q5_1. It is driven through its real IPC socket.

Run without network (F1/N5, §14.3 item 4):
`unshare -rn sh -c 'ip link set lo up && XDG_RUNTIME_DIR=$(mktemp -d) pytest -m e2e'`
"""

import dataclasses
import logging
import os
import shutil
import signal
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from local_stt import app
from local_stt import events as ev
from local_stt.audio.file_source import FileAudioSource
from local_stt.config import Config
from local_stt.inject.x11util import X11Session
from local_stt.ipc import call
from local_stt.sdnotify import SdNotifier
from local_stt.stt import whisper_server as ws

from ..integration.x11_clients import Receiver, held_key, xvfb

pytestmark = [pytest.mark.e2e, pytest.mark.needs_x11, pytest.mark.needs_whisper]

FIXTURE = Path(__file__).parent.parent / "fixtures" / "pl_short.wav"
MODEL = "base-q5_1"
MODEL_PATH = ws.DATA_DIR / "models" / f"ggml-{MODEL}.bin"
# “Warto poświęcić pół godziny na spacer…”: the first 3 s of the 4.2 s fixture
KEYWORDS = ("warto", "godzin")
PTT_S = 3.0
RESULT_TIMEOUT_S = 30.0
NO_RESULT_WAIT_S = 5.0


def whisper_server() -> ws.TemporaryWhisperServer:
    if not ws.DEFAULT_BINARY.is_file() or not MODEL_PATH.is_file():
        pytest.skip("whisper-server binary or base-q5_1 model not installed")
    srv = ws.TemporaryWhisperServer(MODEL_PATH, model=MODEL)
    srv.start()
    return srv


@pytest.fixture(scope="module")
def server() -> Iterator[ws.TemporaryWhisperServer]:
    srv = whisper_server()
    yield srv
    srv.stop()


@pytest.fixture
def own_server() -> Iterator[ws.TemporaryWhisperServer]:
    """A server this test may kill and restart."""
    srv = whisper_server()
    yield srv
    srv.stop()


class Session:
    """A running daemon on a private display, with a receiving window."""

    def __init__(self, daemon: app.Daemon, display: str, receiver: Receiver):
        self.daemon = daemon
        self.display = display
        self.receiver = receiver

    def ipc(self, cmd: str, **fields: Any) -> dict[str, Any]:
        return call({"cmd": cmd, **fields}, path=self.daemon.ipc.path)

    def status(self) -> dict[str, Any]:
        status: dict[str, Any] = self.ipc("status")["status"]
        return status

    def wait_for(self, condition: Callable[[dict[str, Any]], bool], poll_s: float = 0.1) -> None:
        deadline = time.monotonic() + RESULT_TIMEOUT_S
        while not condition(status := self.status()):
            assert time.monotonic() < deadline, status
            time.sleep(poll_s)

    def dictate(self) -> None:
        assert self.ipc("ptt", action="start")["ok"]
        time.sleep(PTT_S)
        assert self.ipc("ptt", action="stop")["ok"]


@contextmanager
def running_daemon(
    server: ws.TemporaryWhisperServer,
    monkeypatch: pytest.MonkeyPatch,
    *,
    audio: Path = FIXTURE,
    **stt: Any,
) -> Iterator[Session]:
    assert server.engine is not None
    base = Config()
    config = dataclasses.replace(
        base,
        stt=dataclasses.replace(base.stt, model=MODEL, port=server.engine.port, **stt),
        # no sounds or notifications on the developer's real desktop
        feedback=dataclasses.replace(base.feedback, sounds=False, notifications="none"),
    )
    pre = app.Preflight(config, None, None, server.engine.request_path)
    runtime = tempfile.mkdtemp(prefix="lstt-")  # short: AF_UNIX paths have 108 bytes
    excepthook = threading.excepthook
    with xvfb() as display:
        monkeypatch.setenv("DISPLAY", display)  # read by the daemon's X connections only
        monkeypatch.setenv("XDG_RUNTIME_DIR", runtime)
        daemon = app.Daemon(
            pre,
            SdNotifier({}),
            capture=lambda frames, _device, _lost: FileAudioSource(frames, audio),
            engine_units=None,  # the temporary server above, not the systemd units
        )
        receiver = Receiver(display)
        started = False
        try:
            daemon.build()
            daemon.start()
            started = True
            s = Session(daemon, display, receiver)
            s.wait_for(lambda status: status["engine"]["state"] == "READY")
            yield s
        finally:
            if started:
                daemon.controller.events.put(ev.ShutdownRequested())
                assert daemon.wait() == app.EXIT_OK
            daemon.stop()
            receiver.close()
            threading.excepthook = excepthook
            shutil.rmtree(runtime, ignore_errors=True)


@pytest.fixture
def session(
    server: ws.TemporaryWhisperServer, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Session]:
    with running_daemon(server, monkeypatch) as s:
        yield s


def test_ptt_inserts_transcript_into_focused_window(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="local_stt")
    session.dictate()

    assert session.receiver.pasted.wait(RESULT_TIMEOUT_S), "no text within 30 s"
    [text] = session.receiver.received
    assert all(word in text.lower() for word in KEYWORDS), text
    session.wait_for(lambda status: status["stats"]["jobs_ok"] == 1)  # JobFinished follows
    # 12 §12.3: with log_text = false no dictated word reaches the log
    logged = caplog.text.lower()
    assert "job 1" in logged or "job=1" in logged  # the timing line was captured
    assert not any(word in logged for word in KEYWORDS)


def test_cancel_before_transcript_inserts_nothing(session: Session) -> None:
    session.dictate()
    reply = session.ipc("cancel")

    assert reply["ok"] and not reply["injection_in_flight"]
    assert not session.receiver.pasted.wait(NO_RESULT_WAIT_S)
    assert session.receiver.received == []


def test_cancel_while_injector_waits_inserts_nothing(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    waiting = threading.Event()
    original = X11Session.wait_for_keys_released

    def wait_and_signal(self: X11Session, *args: Any, **kwargs: Any) -> bool:
        waiting.set()
        return original(self, *args, **kwargs)

    monkeypatch.setattr(X11Session, "wait_for_keys_released", wait_and_signal)
    # 1000 ms modifier_wait_ms by default: a held Shift keeps the injector at step 1
    with held_key(session.display, "Shift_L"):
        session.dictate()
        assert waiting.wait(RESULT_TIMEOUT_S), "the injector never started"
        reply = session.ipc("cancel")
    assert reply["ok"] and not reply["injection_in_flight"]
    assert not session.receiver.pasted.wait(NO_RESULT_WAIT_S)
    assert session.receiver.received == []
    assert [k.keysym for k in session.receiver.keys] == ["Shift_L"]  # ours, no Ctrl+V


# --- the server dies mid-job (E7, §14.3 item 1) ------------------------------------------

# Shortened stt.startup_timeout_s: the monitor's startup grace period (a dead server reads
# as STARTING, 04 §4.5) and the paused queue's JobFailed timer.
STARTUP_S = 5.0


def kill_mid_job(s: Session, server: ws.TemporaryWhisperServer) -> None:
    time.sleep(STARTUP_S - PTT_S + 0.5)  # the kill falls after the startup grace period
    s.dictate()
    s.wait_for(lambda status: status["pipeline"]["busy"], poll_s=0.01)
    assert server.pid is not None
    os.kill(server.pid, signal.SIGKILL)
    server.stop()  # reaps it


def test_server_killed_mid_job_pauses_until_restart_then_job_succeeds(
    own_server: ws.TemporaryWhisperServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert own_server.engine is not None
    port, path = own_server.engine.port, own_server.engine.request_path
    with running_daemon(own_server, monkeypatch, startup_timeout_s=STARTUP_S) as s:
        kill_mid_job(s, own_server)
        s.wait_for(lambda st: st["engine"]["state"] == "DOWN" and st["pipeline"]["paused"])
        assert s.receiver.received == []

        own_server.start(port=port, request_path=path)
        assert s.receiver.pasted.wait(RESULT_TIMEOUT_S), "the job did not resume"
        [text] = s.receiver.received
        assert all(word in text.lower() for word in KEYWORDS), text
        s.wait_for(lambda st: st["engine"]["state"] == "READY" and st["stats"]["jobs_ok"] == 1)
        assert s.status()["stats"]["jobs_failed"] == 0


def test_server_killed_mid_job_without_restart_fails_the_job(
    own_server: ws.TemporaryWhisperServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running_daemon(own_server, monkeypatch, startup_timeout_s=STARTUP_S) as s:
        kill_mid_job(s, own_server)
        killed = time.monotonic()
        s.wait_for(lambda st: st["stats"]["jobs_failed"] == 1)
        assert time.monotonic() - killed >= STARTUP_S - 0.5
        status = s.status()
        assert status["engine"]["state"] == "DOWN"
        assert status["pipeline"]["queued"] == 0 and not status["pipeline"]["busy"]
        assert s.receiver.received == []
