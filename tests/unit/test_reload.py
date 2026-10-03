"""ComponentReloader: the daemon's ReloadTarget (docs/04-state-machine.md §4.6)."""

import queue
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

from local_stt import reload
from local_stt.config import Config, HotkeysConfig
from local_stt.events import Event, ServerRestartDone
from local_stt.interfaces import HotkeyProblem
from local_stt.reload import ComponentReloader

SECRET = "ab" * 16


class Hotkeys:
    def __init__(self) -> None:
        self.applied: list[HotkeysConfig] = []

    def start(self, sink: Any) -> None: ...

    def stop(self) -> None: ...

    def apply(self, config: HotkeysConfig) -> list[HotkeyProblem]:
        self.applied.append(config)
        return [HotkeyProblem("push_to_talk", config.push_to_talk, "taken")]


class Env:
    def __init__(self, tmp_path: Path, exit_code: int = 0) -> None:
        self.events: queue.Queue[Event] = queue.Queue()
        self.calls: list[tuple[str, Config]] = []
        self.hotkeys = Hotkeys()
        self.exit_code = exit_code
        self.env_path = tmp_path / "cfg" / "whisper-server.env"
        self.secret = tmp_path / "secret"
        self.secret.write_text(SECRET + "\n")
        self.reloader = ComponentReloader(
            post=self.events.put,
            live=[
                lambda c: self.calls.append(("live1", c)),
                lambda c: self.calls.append(("live2", c)),
            ],
            at_idle=[lambda c: self.calls.append(("idle", c))],
            hotkeys=self.hotkeys,
            switch_server=lambda c: self.calls.append(("switch", c)),
            env_path=self.env_path,
            secret_path=self.secret,
            restart=lambda: self.exit_code,
        )


def test_live_and_idle_callbacks(tmp_path: Path) -> None:
    env = Env(tmp_path)
    config = Config()
    env.reloader.apply_live(config)
    problems = env.reloader.apply_at_idle(config)
    env.reloader.use_server(config)
    assert env.calls == [("live1", config), ("live2", config), ("idle", config), ("switch", config)]
    assert env.hotkeys.applied == [config.hotkeys]
    assert problems == [HotkeyProblem("push_to_talk", "Control_R", "taken")]


def test_without_hotkeys_backend(tmp_path: Path) -> None:
    reloader = ComponentReloader(post=lambda e: None, switch_server=lambda c: None)
    assert reloader.apply_at_idle(Config()) == []


@pytest.mark.parametrize("code", [0, 1])
def test_restart_writes_env_then_reports_exit_code(tmp_path: Path, code: int) -> None:
    env = Env(tmp_path, exit_code=code)
    env.reloader.restart_server(Config())
    assert env.events.get(timeout=2) == ServerRestartDone(code)
    text = env.env_path.read_text()
    assert f"--request-path /{SECRET}" in text and "-t 4" in text
    assert stat.S_IMODE(env.env_path.stat().st_mode) == 0o600


def test_unreadable_secret_fails_without_restart(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.secret.write_text("short")
    env.exit_code = 99  # must not be reached
    env.reloader.restart_server(Config())
    assert env.events.get(timeout=2) == ServerRestartDone(reload.EXIT_ENV_FAILED)
    assert not env.env_path.exists()


@pytest.mark.parametrize(
    ("outcome", "code"),
    [
        (subprocess.CompletedProcess([], 0, "", ""), 0),
        (subprocess.CompletedProcess([], 5, "", "Unit not found."), 5),
        (FileNotFoundError(), reload.EXIT_NOT_FOUND),
        (subprocess.TimeoutExpired("systemctl", 90), reload.EXIT_TIMEOUT),
    ],
)
def test_systemctl_restart(monkeypatch: pytest.MonkeyPatch, outcome: Any, code: int) -> None:
    calls: list[list[str]] = []

    def run(args: list[str], **kwargs: Any) -> Any:
        calls.append(args)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(subprocess, "run", run)
    assert reload.systemctl_restart() == code
    assert calls == [["systemctl", "--user", "restart", "local-stt-whisper.service"]]
