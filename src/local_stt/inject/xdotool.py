"""`xdotool type` injection (docs/08-text-injection.md §8.6).

Used for windows in `injection.type_window_classes` and when the clipboard cannot be restored
faithfully. Runs in the pipeline thread.
"""

import logging
import os
import subprocess
from collections.abc import Callable
from typing import Any

from local_stt.cancellation import CancellationToken, Cancelled
from local_stt.config import Config, parse_hotkey
from local_stt.inject.clipboard import ClipboardOwner
from local_stt.inject.x11util import X11InjectError, X11Session
from local_stt.interfaces import InjectResult

log = logging.getLogger("local_stt.inject")

CHUNK_CHARS = 200
MIN_TIMEOUT_S = 5.0
TIMEOUT_PER_CHAR_S = 0.05

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def chunks(text: str, size: int = CHUNK_CHARS) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


def chunk_timeout(chunk: str, delay_ms: int) -> float:
    """08 §8.6: max(5 s, 0.05 s per character), scaled up for a larger type_delay_ms."""
    per_char = max(TIMEOUT_PER_CHAR_S, 2 * delay_ms / 1000)
    return max(MIN_TIMEOUT_S, len(chunk) * per_char)


class XdotoolTypeInjector:
    """Implements `interfaces.Injector` (backend "type")."""

    backend = "type"

    def __init__(
        self,
        session: X11Session,
        owner: ClipboardOwner,
        config: Config,
        *,
        display_name: str | None = None,
        xdotool: str = "xdotool",
        run: Runner = subprocess.run,
    ):
        self._x = session
        self._owner = owner
        self._xdotool = xdotool
        self._run = run
        self._env = dict(os.environ)
        if display_name is not None:
            self._env["DISPLAY"] = display_name
        self.update_config(config)

    def update_config(self, config: Config) -> None:
        self._config = config.injection
        _, ptt_keysym = parse_hotkey(config.hotkeys.push_to_talk)
        self._ptt_keycodes = self._x.keycodes(ptt_keysym)

    def inject(self, text: str, *, cancel: CancellationToken) -> InjectResult:
        cfg = self._config
        try:
            released = self._x.wait_for_keys_released(
                cancel,
                ptt_keycodes=self._ptt_keycodes,
                modifier_wait_s=cfg.modifier_wait_ms / 1000,
            )
            target = self._x.active_window()
        except X11InjectError as e:
            return self._fail(text, None, 0, str(e))
        window_class = target.wm_class if target is not None else None
        if not released:
            return self._result(window_class, 0, cancelled=True)
        log.debug("typing into WM_CLASS=%s", window_class)

        typed = 0
        for chunk in chunks(text):
            try:
                with cancel.operation():
                    error = self._type(chunk, cfg.type_delay_ms)
            except Cancelled:
                # Later chunks never start; no clipboard fallback after cancellation.
                return self._result(window_class, typed, cancelled=True)
            if error is not None:
                return self._fail(text[typed:], window_class, typed, error)
            typed += len(chunk)
        return self._result(window_class, typed, ok=True)

    def _type(self, chunk: str, delay_ms: int) -> str | None:
        """Runs one xdotool call; returns an error description or None."""
        # xdotool 3.20160805 sends Linefeed for "\n" but Return for "\r" (tested on Xvfb
        # 2026-10-03); applications expect Return.
        argv = [self._xdotool, "type", "--delay", str(delay_ms), "--", chunk.replace("\n", "\r")]
        try:
            proc = self._run(
                argv,
                env=self._env,
                capture_output=True,
                text=True,
                timeout=chunk_timeout(chunk, delay_ms),
            )
        except FileNotFoundError:
            return f"{self._xdotool} not found"
        except subprocess.TimeoutExpired:
            return "xdotool timed out"
        if proc.returncode != 0:
            return f"xdotool exited with {proc.returncode}: {proc.stderr.strip()[:200]}"
        return None

    def _fail(self, rest: str, window_class: str | None, typed: int, error: str) -> InjectResult:
        """E13: keep the text that was not typed in the clipboard (sacrificing its previous
        content: dictated text must not be lost)."""
        log.error("type injection failed: %s", error)
        left = False
        try:
            left = bool(self._owner.take_text(rest).result(timeout=2.0))
        except Exception as e:  # the clipboard is a best-effort fallback here
            log.error("could not leave the text in the clipboard: %s", e)
        return self._result(window_class, typed, error=error, left_in_clipboard=left)

    def _result(self, window_class: str | None, typed: int, **kwargs: Any) -> InjectResult:
        return InjectResult(
            ok=kwargs.pop("ok", False),
            backend=self.backend,
            chars=typed,
            window_class=window_class,
            left_in_clipboard=kwargs.pop("left_in_clipboard", False),
            error=kwargs.pop("error", None),
            **kwargs,
        )
