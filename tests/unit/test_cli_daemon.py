# ruff: noqa: E501  (status fixtures and expected output verbatim)
"""Daemon commands of the CLI (docs/10-cli-ipc-status.md §10.1, §10.4)."""

import json
import sys
import tempfile
import threading
from collections.abc import Iterator
from typing import Any

import pytest

from local_stt import cli, ipc
from local_stt.audio.capture import AudioCapture, DeviceInfo
from local_stt.config import Config
from local_stt.controller import Controller

from .test_controller import Consumer, Pipeline, Reload, World

STATUS: dict[str, Any] = {
    "version": "0.1.0",
    "state": "IDLE",
    "mode": "IDLE",
    "speech": False,
    "reconnecting": False,
    "engine": {"state": "READY", "name": "whisper.cpp", "model": "small-q8_0", "port": 8178, "threads": 4},
    "hotkeys": {"state": "OK", "push_to_talk": "Control_R", "continuous_toggle": "Shift+Control_R", "problems": []},
    "audio": {"device": "default", "open": False, "overflows": 0},
    "pipeline": {
        "queued": 0,
        "queued_audio_s": 0,
        "busy": False,
        "paused": False,
        "generation": 1,
        "last": {"audio_s": 3.8, "stt_s": 1.6, "ago_s": 130},
    },
    "stats": {"jobs_ok": 1, "jobs_failed": 0, "jobs_filtered": 0, "rtf_avg_10": 0.42, "latency_avg_10_s": 1.7},
    "uptime_s": 8040,
}  # fmt: skip


def test_format_status_matches_the_spec_example() -> None:
    assert cli.format_status(STATUS) == (
        "local-stt 0.1.0 — IDLE\n"
        "  engine     READY   whisper.cpp small-q8_0 @127.0.0.1:8178 (t=4)\n"
        "  hotkeys    OK      PTT=Control_R  continuous=Shift+Control_R\n"
        "  audio      default (closed)\n"
        "  pipeline   0 queued, last: 3.8 s audio → 1.6 s (RTF 0.42) 2 min ago\n"
        "  uptime     2 h 14 min"
    )


def test_format_status_degraded_disabled_and_busy() -> None:
    degraded = {
        **STATUS,
        "hotkeys": {
            **STATUS["hotkeys"],
            "state": "degraded",
            "problems": [{"hotkey": "push_to_talk", "value": "Control_R", "reason": "taken"}],
        },
        "pipeline": {**STATUS["pipeline"], "queued": 2, "busy": True, "paused": True, "last": None},
        "uptime_s": 42,
    }
    text = cli.format_status(degraded)
    assert "  hotkeys    degraded PTT=Control_R" in text
    assert "             push_to_talk (Control_R): taken" in text
    assert "  pipeline   2 queued, transcribing, paused\n" in text
    assert text.endswith("  uptime     42 s")
    disabled = {**STATUS, "hotkeys": {**STATUS["hotkeys"], "state": "disabled"}}
    assert "  hotkeys    disabled\n" in cli.format_status(disabled)


class FakeIpc:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.response: dict[str, Any] | Exception = {"ok": True}

    def call(self, request: dict[str, Any]) -> dict[str, Any]:
        self.sent.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeIpc:
    f = FakeIpc()
    monkeypatch.setattr(ipc, "call", f.call)
    return f


@pytest.mark.parametrize(
    ("argv", "request_"),
    [
        (["ptt", "start"], {"cmd": "ptt", "action": "start"}),
        (["ptt", "stop"], {"cmd": "ptt", "action": "stop"}),
        (["cancel"], {"cmd": "cancel"}),
        (["toggle"], {"cmd": "toggle"}),
        (["status"], {"cmd": "status"}),
    ],
)
def test_requests(
    fake: FakeIpc,
    argv: list[str],
    request_: dict[str, Any],
) -> None:
    fake.response = {"ok": True, "status": STATUS}
    assert cli.main(argv) == 0
    assert fake.sent == [request_]


def test_status_json(
    fake: FakeIpc,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake.response = {"ok": True, "status": STATUS}
    assert cli.main(["status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == STATUS


def test_not_running(
    fake: FakeIpc,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake.response = ipc.DaemonNotRunning("daemon not running")
    assert cli.main(["status"]) == 3
    assert capsys.readouterr().err == "daemon not running\n"


def test_ipc_failure(fake: FakeIpc) -> None:
    fake.response = ipc.IpcError("no response")
    assert cli.main(["cancel"]) == 1


def test_rejection_exit_code(
    fake: FakeIpc,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake.response = {"ok": False, "error": "engine_down", "message": "STT engine unavailable"}
    assert cli.main(["ptt", "start"]) == 4
    assert capsys.readouterr().err == "rejected: STT engine unavailable\n"


def test_cancel_with_injection_in_flight(
    fake: FakeIpc,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake.response = {"ok": True, "injection_in_flight": True}
    assert cli.main(["cancel"]) == 0
    assert "injection already in progress may finish" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("response", "code", "out"),
    [
        ({"ok": True, "applied": [], "deferred": [], "server_restart": False}, 0, "no changes\n"),
        (
            {"ok": True, "applied": ["text.replacements"], "deferred": ["stt.threads"], "server_restart": True},
            0,
            "applied:  text.replacements\ndeferred: stt.threads\nwhisper-server restarts with the new settings\n",
        ),
        ({"ok": False, "errors": ["stt.port: must be in 1-65535 (got 0)"]}, 78, ""),
    ],
)  # fmt: skip
def test_reload(
    fake: FakeIpc,
    capsys: pytest.CaptureFixture[str],
    response: dict[str, Any],
    code: int,
    out: str,
) -> None:
    fake.response = response
    assert cli.main(["reload"]) == code
    captured = capsys.readouterr()
    assert captured.out == out
    if code == 78:
        assert "config error: stt.port: must be in 1-65535 (got 0)" in captured.err


# --- status --watch (task 2.5) ----------------------------------------------------------------


def state(name: str) -> dict[str, Any]:
    return {"event": "state", "status": {**STATUS, "state": name}}


JOB = {
    "event": "job",
    "job_id": 1,
    "source": "continuous",
    "audio_s": 2.0,
    "processing_s": 1.0,
    "chars": 9,
    "result": "injected",
}


def fake_stream(monkeypatch: pytest.MonkeyPatch, messages: list[Any]) -> None:
    def subscribe() -> Iterator[dict[str, Any]]:
        for m in messages:
            if isinstance(m, BaseException):
                raise m
            yield m

    monkeypatch.setattr(ipc, "subscribe", subscribe)


def test_watch_prints_a_line_per_state_change(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_stream(monkeypatch, [state("IDLE"), JOB, state("LISTENING (speech)"), KeyboardInterrupt()])
    assert cli.main(["status", "--watch"]) == 0
    assert capsys.readouterr().out == "IDLE\nLISTENING (speech)\n"  # not a terminal: no rewriting


def test_watch_rewrites_one_line_on_a_terminal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_stream(monkeypatch, [state("IDLE"), state("LISTENING"), KeyboardInterrupt()])
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    assert cli.main(["status", "--watch"]) == 0
    assert capsys.readouterr().out == "\r\033[KIDLE\r\033[KLISTENING\n"


def test_watch_json_passes_every_event(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_stream(monkeypatch, [state("IDLE"), JOB])
    assert cli.main(["status", "--watch", "--json"]) == 3  # the stream ended: daemon stopped
    captured = capsys.readouterr()
    assert [json.loads(line) for line in captured.out.splitlines()] == [state("IDLE"), JOB]
    assert captured.err == "daemon stopped\n"


@pytest.mark.parametrize(
    ("messages", "code", "err"),
    [
        ([{"ok": False, "error": "timeout", "message": "no answer"}], 4, "rejected: no answer\n"),
        ([ipc.DaemonNotRunning("x")], 3, "daemon not running\n"),
        ([ipc.IpcError("broken")], 1, "error: broken\n"),
    ],
)
def test_watch_failures(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    messages: list[Any],
    code: int,
    err: str,
) -> None:
    fake_stream(monkeypatch, messages)
    assert cli.main(["status", "--watch"]) == code
    assert capsys.readouterr().err == err


def test_devices(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    devices = [
        DeviceInfo("alsa_input.usb", "USB Mic", False),
        DeviceInfo("alsa_input.pci", "Built-in", True),
    ]
    monkeypatch.setattr(AudioCapture, "list_devices", staticmethod(lambda: devices))
    assert cli.main(["devices"]) == 0
    assert capsys.readouterr().out == (
        "  alsa_input.usb  USB Mic\n"
        "* alsa_input.pci  Built-in\n"
        '* = default source (audio.device = "default")\n'
    )


def test_devices_without_pactl(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail() -> list[DeviceInfo]:
        raise FileNotFoundError("pactl")

    monkeypatch.setattr(AudioCapture, "list_devices", staticmethod(fail))
    assert cli.main(["devices"]) == 1


# --- CLI → socket → controller thread ------------------------------------------------------


@pytest.fixture
def running(monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    w = World()
    controller = Controller(
        Config(),
        capture=w,
        consumer=Consumer(w),
        pipeline=Pipeline(w),
        feedback=w,
        lifecycle=w,
        reload_target=Reload(w),
        load_config=w.load_config,
    )
    with tempfile.TemporaryDirectory(prefix="lstt-") as runtime:
        monkeypatch.setenv("XDG_RUNTIME_DIR", runtime)
        server = ipc.IpcServer(ipc.socket_path(), controller.events.put, poll_interval_s=0.01)
        server.start()
        thread = threading.Thread(target=controller.run, daemon=True)
        thread.start()
        yield w
        from local_stt.events import ShutdownRequested

        controller.events.put(ShutdownRequested())
        thread.join(2)
        server.stop()


def test_round_trip_through_the_socket(running: World, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["status", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert (status["state"], status["engine"]["state"]) == ("STARTING", "STARTING")
    assert cli.main(["ptt", "start"]) == 4  # engine not ready yet: rejected
    assert "engine" in capsys.readouterr().err
    assert cli.main(["cancel"]) == 0


def test_watch_through_the_socket(
    running: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    real = ipc.subscribe

    def first_state_then_interrupt() -> Iterator[dict[str, Any]]:
        yield next(real())
        raise KeyboardInterrupt

    monkeypatch.setattr(ipc, "subscribe", first_state_then_interrupt)
    assert cli.main(["status", "--watch"]) == 0
    assert capsys.readouterr().out == "STARTING\n"
