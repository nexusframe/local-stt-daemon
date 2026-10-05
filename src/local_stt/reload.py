"""Applies a reloaded config to running components (docs/04-state-machine.md §4.6).

`ComponentReloader` is the daemon's `ReloadTarget`. Components register their own update
functions (wired in app.py, task 1.13); the server restart itself lives here: rewrite
`whisper-server.env` (09 §9.4) and run `systemctl --user restart` in a helper thread, which
posts `ServerRestartDone`.
"""

import logging
import subprocess
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

from local_stt.config import Config, config_dir, render_whisper_env, write_whisper_env
from local_stt.events import Event, ServerRestartDone
from local_stt.interfaces import HotkeyBackend, HotkeyProblem
from local_stt.stt.whisper_server import read_request_path

log = logging.getLogger("local_stt.controller")

WHISPER_UNIT = "local-stt-whisper.service"
SYSTEMCTL_TIMEOUT_S = 90.0
EXIT_ENV_FAILED = 1  # reported like a failed systemctl: E16
EXIT_TIMEOUT = 124
EXIT_NOT_FOUND = 127

ConfigCallback = Callable[[Config], None]


def systemctl_restart(unit: str = WHISPER_UNIT) -> int:
    """`systemctl --user restart <unit>`; returns its exit code."""
    try:
        done = subprocess.run(
            ["systemctl", "--user", "restart", unit],
            capture_output=True,
            text=True,
            timeout=SYSTEMCTL_TIMEOUT_S,
        )
    except FileNotFoundError:
        log.error("systemctl not found")
        return EXIT_NOT_FOUND
    except subprocess.TimeoutExpired:
        log.error("systemctl restart %s did not finish in %.0f s", unit, SYSTEMCTL_TIMEOUT_S)
        return EXIT_TIMEOUT
    if done.returncode != 0:
        log.error("systemctl restart %s: %s", unit, done.stderr.strip())
    return done.returncode


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
        restart: Callable[[], int] = systemctl_restart,
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
        restarts the unit in a helper thread; never blocks the controller."""
        try:
            content = render_whisper_env(config.stt, read_request_path(self._secret_path))
            write_whisper_env(self._env_path, content)
        except (OSError, ValueError) as e:
            log.error("cannot write %s: %s", self._env_path, e)
            self._post(ServerRestartDone(EXIT_ENV_FAILED))
            return
        threading.Thread(target=self._run_restart, name="server-restart", daemon=True).start()

    def _run_restart(self) -> None:
        code = self._restart()
        log.info("whisper-server restart finished with code %d", code)
        self._post(ServerRestartDone(code))

    def use_server(self, config: Config) -> None:
        self._switch_server(config)
        # Live components get the effective config too, never one older than the controller's.
        self.apply_live(config)
