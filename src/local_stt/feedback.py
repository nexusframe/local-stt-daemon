"""Sounds and desktop notifications (docs/10-cli-ipc-status.md §10.6).

`play()` and `notify()` are called from the controller thread and never wait: sounds are
separate `pw-play` processes, notifications go through a queue to the `feedback` thread, which
runs `notify-send -p` and remembers the returned id per key so the next notification with the
same key replaces the previous one. Notifications never contain transcript text.
"""

import logging
import os
import queue
import shutil
import subprocess
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from local_stt.audio.wav import float32_to_wav_bytes
from local_stt.config import Config, FeedbackConfig
from local_stt.interfaces import Sound

log = logging.getLogger("local_stt.feedback")

SOUND_RATE = 48000
FADE_S = 0.005
# (frequency in Hz or 0 for a pause, seconds) per sound (10 §10.6).
PATTERNS: dict[Sound, list[tuple[float, float]]] = {
    "start": [(660.0, 0.065), (880.0, 0.065)],
    "stop": [(880.0, 0.065), (660.0, 0.065)],
    "cancel": [(440.0, 0.120)],
    "error": [(330.0, 0.050), (0.0, 0.050), (330.0, 0.050), (0.0, 0.050), (330.0, 0.050)],
}
PLAYERS = ("pw-play", "paplay")
NOTIFY_TIMEOUT_S = 5.0
APP_NAME = "local-stt"
ICON = "audio-input-microphone"


def sounds_dir(environ: Mapping[str, str] = os.environ) -> Path:
    runtime = environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime) / "local-stt" / "sounds"


def render(sound: Sound, volume: float) -> NDArray[np.float32]:
    """Sine tones with 5 ms fade-in/out at `volume` amplitude; pauses are silence."""
    parts = []
    fade = int(FADE_S * SOUND_RATE)
    ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)
    for freq, seconds in PATTERNS[sound]:
        n = round(seconds * SOUND_RATE)
        if freq == 0:
            parts.append(np.zeros(n, dtype=np.float32))
            continue
        t = np.arange(n, dtype=np.float32) / SOUND_RATE
        tone = (volume * np.sin(2 * np.pi * freq * t)).astype(np.float32)
        tone[:fade] *= ramp
        tone[-fade:] *= ramp[::-1]
        parts.append(tone)
    return np.concatenate(parts)


def duration(sound: Sound) -> float:
    return sum(seconds for _, seconds in PATTERNS[sound])


def write_sounds(directory: Path, volume: float) -> dict[Sound, Path]:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    paths: dict[Sound, Path] = {}
    for sound in PATTERNS:
        path = directory / f"{sound}.wav"
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(float32_to_wav_bytes(render(sound, volume), SOUND_RATE))
        os.replace(tmp, path)  # a pw-play started earlier keeps reading the old file
        paths[sound] = path
    return paths


@dataclass(frozen=True)
class _Notification:
    key: str
    title: str
    body: str
    transient: bool


class DesktopFeedback:
    """`Feedback` (interfaces.py) for GNOME: pw-play/paplay and notify-send."""

    def __init__(
        self,
        config: FeedbackConfig,
        directory: Path | None = None,
        *,
        which: Callable[[str], str | None] = shutil.which,
        popen: Callable[..., Any] = subprocess.Popen,
        run: Callable[..., Any] = subprocess.run,
    ):
        self._config = config
        self._directory = directory or sounds_dir()
        self._popen = popen
        self._run = run
        self._player = next((path for p in PLAYERS if (path := which(p))), None)
        self._notify_send = which("notify-send")
        self._paths: dict[Sound, Path] = {}
        self._playing: list[Any] = []  # Popen objects, reaped on the next play()
        self._ids: dict[str, str] = {}  # key → notification id; feedback thread only
        self._queue: queue.Queue[_Notification | None] = queue.Queue()
        self._thread = threading.Thread(target=self._notify_loop, name="feedback", daemon=True)

    def start(self) -> None:
        if self._player is None:
            log.warning("neither pw-play nor paplay found: no sounds")
        if self._notify_send is None:
            log.warning("notify-send not found: no notifications")
        self._write_sounds()
        self._thread.start()

    def stop(self) -> None:
        if self._thread.is_alive():
            self._queue.put(None)
            self._thread.join(NOTIFY_TIMEOUT_S)

    def update_config(self, config: Config) -> None:
        """Live reload (04 §4.6); called from the controller thread."""
        old, self._config = self._config, config.feedback
        if config.feedback.sound_volume != old.sound_volume:
            self._write_sounds()

    # --- Feedback -------------------------------------------------------------------------

    def play(self, sound: Sound) -> float | None:
        self._playing = [p for p in self._playing if p.poll() is None]
        path = self._paths.get(sound)
        if not self._config.sounds or self._player is None or path is None:
            return None
        try:
            self._playing.append(
                self._popen(
                    [self._player, str(path)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
        except OSError as e:
            log.warning("cannot play %s sound: %s", sound, e)
            return None
        return duration(sound)

    def notify(self, key: str, title: str, body: str = "", *, informational: bool = False) -> None:
        level = self._config.notifications
        if level == "none" or (informational and level != "all"):
            return
        if self._notify_send is None:
            return
        self._queue.put(_Notification(key, title, body, transient=informational))

    # --- internals ------------------------------------------------------------------------

    def _write_sounds(self) -> None:
        try:
            self._paths = write_sounds(self._directory, self._config.sound_volume)
        except OSError as e:
            log.warning("cannot write sounds to %s: %s", self._directory, e)
            self._paths = {}

    def _notify_loop(self) -> None:
        while (item := self._queue.get()) is not None:
            self._send(item)

    def _send(self, n: _Notification) -> None:
        assert self._notify_send is not None
        args = [self._notify_send, "-a", APP_NAME, "-i", ICON, "-p"]
        if n.key in self._ids:
            args += ["-r", self._ids[n.key]]
        if n.transient:
            args.append("-e")
        args += ["--", n.title, n.body]
        try:
            done = self._run(args, capture_output=True, text=True, timeout=NOTIFY_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("notify-send failed: %s", e)
            return
        notification_id = done.stdout.strip()
        if done.returncode != 0 or not notification_id.isdigit():
            log.warning("notify-send exited with %d: %s", done.returncode, done.stderr.strip())
            return
        self._ids[n.key] = notification_id
