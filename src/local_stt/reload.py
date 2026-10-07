"""Applies a reloaded config to running components (docs/04-state-machine.md §4.6).

`ComponentReloader` is the daemon's `ReloadTarget`. Components register their own update
functions (wired in app.py, task 1.13); the server restart itself lives here: rewrite
`whisper-server.env` (09 §9.4) for whisper-server, stop the other engine's unit and run
`systemctl --user restart` in a helper thread, which posts `ServerRestartDone`.
"""

import logging
import subprocess
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

from local_stt.config import Config, config_dir, render_whisper_env, write_whisper_env
from local_stt.events import Event, ServerRestartDone
from local_stt.interfaces import HotkeyBackend, HotkeyProblem
from local_stt.stt import ENGINE_UNITS
from local_stt.stt.whisper_server import read_request_path

log = logging.getLogger("local_stt.controller")

SYSTEMCTL_TIMEOUT_S = 90.0
EXIT_ENV_FAILED = 1  # reported like a failed systemctl: E16
EXIT_TIMEOUT = 124
EXIT_NOT_FOUND = 127
EXIT_UNIT_NOT_LOADED = 5  # systemctl stop: the unit is not installed

ConfigCallback = Callable[[Config], None]


def systemctl(action: str, unit: str) -> int:
    """`systemctl --user <action> <unit>`; returns its exit code."""
    try:
        done = subprocess.run(
            ["systemctl", "--user", action, unit],
            capture_output=True,
            text=True,
            timeout=SYSTEMCTL_TIMEOUT_S,
        )
    except FileNotFoundError:
        log.error("systemctl not found")
        return EXIT_NOT_FOUND
    except subprocess.TimeoutExpired:
        log.error("systemctl %s %s did not finish in %.0f s", action, unit, SYSTEMCTL_TIMEOUT_S)
        return EXIT_TIMEOUT
    if done.returncode != 0 and not (action == "stop" and done.returncode == EXIT_UNIT_NOT_LOADED):
        log.error("systemctl %s %s: %s", action, unit, done.stderr.strip())
    return done.returncode


def switch_engine_unit(engine: str, action: str = "restart") -> int:
    """Stops the other engines' units, then `start`s or `restart`s the selected engine's unit
    (task 4.3: only one runs, they share stt.port); returns the exit code of the last call.

    A failed stop is only logged: the start then fails on the taken port and reports it.
    """
    for name, unit in ENGINE_UNITS.items():
        if name != engine:
            systemctl("stop", unit)
    return systemctl(action, ENGINE_UNITS[engine])


class ComponentReloader:
    def __init__(
        self,
        *,
        post: Callable[[Event], None],
        live: Sequence[ConfigCallback] = (),
        at_idle: Sequence[ConfigCallback] = (),
        hotkeys: HotkeyBackend | None = None,
        switch_server: ConfigCallback,
        env_path: Path | None = None,
        secret_path: Path | None = None,
        restart: Callable[[str], int] = switch_engine_unit,
    ):
        self._post = post
        self._live = list(live)
        self._at_idle = list(at_idle)
        self._hotkeys = hotkeys
        self._switch_server = switch_server
        self._env_path = env_path or config_dir() / "whisper-server.env"
        self._secret_path = secret_path or config_dir() / "secret"
        self._restart = restart

    def apply_live(self, config: Config) -> None:
        for update in self._live:
            update(config)

    def apply_at_idle(self, config: Config) -> list[HotkeyProblem]:
        for update in self._at_idle:
            update(config)
        return self._hotkeys.apply(config.hotkeys) if self._hotkeys is not None else []

    def restart_server(self, config: Config) -> None:
        """Writes the env file now (the controller decided the server may restart) and
        restarts the selected engine's unit in a helper thread; never blocks the controller.

        Only whisper-server reads the env file; the Parakeet server reads the config itself.
        """
        engine = config.stt.engine
        if engine != "whisper-server":
            self._start_restart(engine)
            return
        try:
            content = render_whisper_env(config.stt, read_request_path(self._secret_path))
            write_whisper_env(self._env_path, content)
        except (OSError, ValueError) as e:
            log.error("cannot write %s: %s", self._env_path, e)
            self._post(ServerRestartDone(EXIT_ENV_FAILED))
            return
        self._start_restart(engine)

    def _start_restart(self, engine: str) -> None:
        threading.Thread(
            target=self._run_restart, args=(engine,), name="server-restart", daemon=True
        ).start()

    def _run_restart(self, engine: str) -> None:
        code = self._restart(engine)
        log.info("%s restart finished with code %d", ENGINE_UNITS[engine], code)
        self._post(ServerRestartDone(code))

    def use_server(self, config: Config) -> None:
        self._switch_server(config)
        # Live components get the effective config too, never one older than the controller's.
        self.apply_live(config)
