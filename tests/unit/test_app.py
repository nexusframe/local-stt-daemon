"""Daemon composition helpers (app.py): preflight exit codes, crash handling, adapters."""

import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from local_stt import app, cli
from local_stt.app import EXIT_CONFIG, EXIT_FAILURE, StartupError
from local_stt.interfaces import EngineHealth, Transcript

from .test_doctor import Run, loginctl

SECRET = "0123456789abcdef" * 2


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    """A complete installation: config dir with secret, models dir with both models."""
    config_dir = tmp_path / "xdg/local-stt"
    config_dir.mkdir(parents=True)
    (config_dir / "secret").write_text(SECRET + "\n")
    (config_dir / "secret").chmod(0o600)
    models = tmp_path / "models"
    models.mkdir()
    (models / "ggml-small-q8_0.bin").write_bytes(b"x")
    (models / "silero_vad.onnx").write_bytes(b"x")
    (config_dir / "config.toml").write_text(f'[stt]\nmodels_dir = "{models}"\n')
    return {"XDG_CONFIG_HOME": str(tmp_path / "xdg"), "DISPLAY": ":1"}


X11 = Run(loginctl("3", "x11"))


def startup_code(env: dict[str, str], run: Run = X11) -> int:
    with pytest.raises(StartupError) as e:
        app.preflight(None, None, environ=env, run=run)
    return e.value.code


def test_preflight_ok(env: dict[str, str]) -> None:
    pre = app.preflight(None, "DEBUG", environ=env, run=X11)
    assert pre.request_path == "/" + SECRET
    assert pre.cli_level == "DEBUG"
    assert pre.config.stt.model == "small-q8_0"


def test_invalid_config_exits_78(env: dict[str, str], caplog: pytest.LogCaptureFixture) -> None:
    path = Path(env["XDG_CONFIG_HOME"]) / "local-stt/config.toml"
    path.write_text("[stt]\nthreads = 0\n")
    assert startup_code(env) == EXIT_CONFIG
    assert "config error: stt.threads" in caplog.text


def test_missing_model_exits_78(env: dict[str, str], caplog: pytest.LogCaptureFixture) -> None:
    path = Path(env["XDG_CONFIG_HOME"]) / "local-stt/config.toml"
    path.write_text('[stt]\nmodels_dir = "/nonexistent"\n')
    assert startup_code(env) == EXIT_CONFIG
    assert "local-stt models pull small-q8_0" in caplog.text


def test_invalid_log_level_from_environment_exits_78(env: dict[str, str]) -> None:
    assert startup_code({**env, "LOCAL_STT_LOG_LEVEL": "LOUD"}) == EXIT_CONFIG


def test_non_x11_session_exits_78(env: dict[str, str]) -> None:
    assert startup_code(env, Run(loginctl("2", "wayland"))) == EXIT_CONFIG


def test_x11_without_display_exits_1(env: dict[str, str]) -> None:
    del env["DISPLAY"]
    assert startup_code(env) == EXIT_FAILURE


def test_bad_secret_exits_78(env: dict[str, str]) -> None:
    (Path(env["XDG_CONFIG_HOME"]) / "local-stt/secret").write_text("short\n")
    assert startup_code(env) == EXIT_CONFIG
    (Path(env["XDG_CONFIG_HOME"]) / "local-stt/secret").unlink()
    assert startup_code(env) == EXIT_CONFIG


def test_startup_checks_report_only_problems(env: dict[str, str], tmp_path: Path) -> None:
    pre = app.preflight(None, None, environ=env, run=X11)
    run = Run(
        {
            ("systemctl", "--user", "show-environment"): (0, "DISPLAY=:1\n"),
            ("ss", "-ltnH"): (0, "LISTEN 0 511 0.0.0.0:8178 0.0.0.0:*\n"),
        }
    )
    lines = app.startup_checks(pre.config, env, run, tmp_path / "data")
    names = [line.split()[1] for line in lines]
    assert names == ["whisper-server", "whisper-server.env", "port"]  # OK checks are not logged
    assert lines[0].endswith("(→ scripts/install.sh --rebuild-whisper)")
    assert any(line.startswith("doctor: port 8178 FAIL") for line in lines)


class FakeEngine:
    def __init__(self, name: str, health: EngineHealth) -> None:
        self.name = "whisper.cpp"
        self.model = name
        self._health = health
        self.calls: list[dict[str, Any]] = []

    def health(self) -> EngineHealth:
        return self._health

    def transcribe(self, audio: Any, **kwargs: Any) -> Transcript:
        self.calls.append(kwargs)
        return Transcript("ok", [], 1.0, 0.1, "whisper.cpp", self.model)


def test_switchable_engine() -> None:
    old, new = FakeEngine("small", EngineHealth.READY), FakeEngine("medium", EngineHealth.DOWN)
    engine = app.SwitchableEngine(old)
    audio = np.zeros(16000, dtype=np.float32)
    kwargs: dict[str, Any] = {
        "sample_rate": 16000,
        "language": "pl",
        "prompt": None,
        "timeout_s": 5.0,
    }
    assert engine.transcribe(audio, **kwargs).model == "small"
    assert old.calls == [kwargs]
    engine.switch(new)
    assert engine.health() is EngineHealth.DOWN
    assert engine.transcribe(audio, **kwargs).model == "medium"


def test_lifecycle_skips_ungrab_after_x11_loss() -> None:
    calls: list[str] = []
    lifecycle = app.Lifecycle(lambda: calls.append("hotkeys"), lambda: calls.append("ipc"))
    lifecycle.shutdown(x11_alive=False)
    assert calls == ["ipc"]
    lifecycle.shutdown(x11_alive=True)
    assert calls == ["ipc", "hotkeys", "ipc"]


def crash_in_thread(
    name: str, handler: app.ThreadCrashHandler, monkeypatch: pytest.MonkeyPatch, exc: BaseException
) -> None:
    monkeypatch.setattr(threading, "excepthook", handler)

    def boom() -> None:
        raise exc

    thread = threading.Thread(target=boom, name=name)
    thread.start()
    thread.join()


def handler_with(restarts: list[str]) -> tuple[app.ThreadCrashHandler, list[int]]:
    exits: list[int] = []
    restarters: dict[str, Callable[[], None]] = {
        "engine-monitor": lambda: restarts.append("engine-monitor"),
        "ipc-server": lambda: restarts.append("ipc-server"),
    }
    return app.ThreadCrashHandler(restarters, exit=exits.append), exits


@pytest.mark.parametrize("name", sorted(app.CRITICAL_THREADS))
def test_critical_thread_crash_exits_1(
    name: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    restarts: list[str] = []
    handler, exits = handler_with(restarts)
    crash_in_thread(name, handler, monkeypatch, RuntimeError("bug"))
    assert exits == [EXIT_FAILURE]
    assert restarts == []
    record = caplog.records[-1]
    assert record.levelname == "CRITICAL"
    assert record.exc_info is not None


@pytest.mark.parametrize("name", ["engine-monitor", "ipc-server"])
def test_restartable_thread_crash_restarts(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    restarts: list[str] = []
    handler, exits = handler_with(restarts)
    crash_in_thread(name, handler, monkeypatch, RuntimeError("bug"))
    assert (exits, restarts) == ([], [name])


def test_other_thread_crash_is_logged_only(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    restarts: list[str] = []
    handler, exits = handler_with(restarts)
    crash_in_thread("feedback", handler, monkeypatch, RuntimeError("bug"))
    assert (exits, restarts) == ([], [])
    assert "unhandled exception in thread feedback; thread ended" in caplog.text
    crash_in_thread("pipeline", handler, monkeypatch, SystemExit(0))
    assert exits == []


def test_cli_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[Path | None, str | None]] = []

    def fake(config_path: Path | None, level: str | None) -> int:
        seen.append((config_path, level))
        return 78

    monkeypatch.setattr(app, "run_daemon", fake)
    assert cli.main(["daemon", "--config", "/tmp/c.toml", "--log-level", "DEBUG"]) == 78
    assert seen == [(Path("/tmp/c.toml"), "DEBUG")]
    with pytest.raises(SystemExit) as e:
        cli.main(["daemon", "--log-level", "LOUD"])
    assert e.value.code == 2


def test_daemon_imports_no_internet_code() -> None:
    import subprocess
    import sys

    # every module Daemon.build() imports lazily
    modules = [
        "app", "audio.capture", "audio.consumer", "controller", "engine_monitor", "feedback",
        "hotkeys.x11", "inject.auto", "inject.clipboard", "ipc", "pipeline", "reload",
        "sdnotify", "text.processor",
    ]  # fmt: skip
    code = (
        f"import importlib, sys; [importlib.import_module('local_stt.' + m) for m in {modules}]; "
        "bad = {'local_stt.models', 'urllib.request'} & set(sys.modules); "
        "assert not bad, bad"
    )
    subprocess.run([sys.executable, "-c", code], check=True, env={**os.environ})
