"""Backend selection (docs/08-text-injection.md §8.4, §8.7) and injector wiring.

`injection.backend`:
- "auto": `type` for `type_window_classes`, otherwise clipboard + paste; an unrestorable
  clipboard switches to `type`. Without xdotool only the clipboard is used and an
  unrestorable clipboard is pasted without restoration (WARNING).
- "clipboard": always paste; an unrestorable clipboard is not restored (WARNING).
- "type": always `xdotool type`; falls back to the clipboard if xdotool is missing.
- "clipboard-only": put the text in the clipboard and send no keys (task 5.3).
"""

import logging
import shutil

from local_stt.cancellation import CancellationToken
from local_stt.config import Config
from local_stt.inject.clipboard import (
    ClipboardOnlyInjector,
    ClipboardOwner,
    ClipboardPasteInjector,
    ClipboardUnrestorable,
)
from local_stt.inject.x11util import X11Session
from local_stt.inject.xdotool import XdotoolTypeInjector
from local_stt.interfaces import Injector, InjectResult

log = logging.getLogger("local_stt.inject")


class AutoInjector:
    """Implements `interfaces.Injector`; used by the pipeline thread."""

    def __init__(
        self,
        session: X11Session,
        clipboard: ClipboardPasteInjector,
        typer: XdotoolTypeInjector | None,
        config: Config,
        *,
        clipboard_only: Injector,
    ):
        self._x = session
        self._clipboard = clipboard
        self._typer = typer
        self._clipboard_only = clipboard_only
        self.update_config(config)

    def update_config(self, config: Config) -> None:
        self._config = config.injection
        self._clipboard.update_config(config)
        if self._typer is not None:
            self._typer.update_config(config)
        self._clipboard.type_fallback = self._typer is not None and self._config.backend == "auto"
        if self._typer is None and self._config.backend == "type":
            log.warning("injection.backend = type, but xdotool is missing: using the clipboard")

    def inject(self, text: str, *, cancel: CancellationToken) -> InjectResult:
        injector = self._select()
        try:
            return injector.inject(text, cancel=cancel)
        except ClipboardUnrestorable as e:
            # Raised before anything changed: the clipboard stays untouched (08 §8.5 step 3).
            assert self._typer is not None  # type_fallback is set only with xdotool
            log.info("clipboard cannot be restored (%s): typing instead", e)
            return self._typer.inject(text, cancel=cancel)

    def _select(self) -> Injector:
        backend = self._config.backend
        if backend == "clipboard-only":
            return self._clipboard_only
        if self._typer is None or backend == "clipboard":
            return self._clipboard
        if backend == "type":
            return self._typer
        target = self._x.active_window()
        if target is not None:
            names = {target.wm_class.lower(), target.wm_instance.lower()}
            if names & {c.lower() for c in self._config.type_window_classes}:
                return self._typer
        return self._clipboard


def build_injector(
    config: Config, owner: ClipboardOwner, display_name: str | None = None
) -> AutoInjector:
    """Wires the injectors for the pipeline thread (called from app.py, task 1.13).

    The X11Session is used only by the pipeline thread afterwards (02 §2.2).
    """
    session = X11Session(display_name)
    clipboard = ClipboardPasteInjector(owner, session, config, type_fallback=False)
    xdotool = shutil.which("xdotool")
    typer = (
        XdotoolTypeInjector(session, owner, config, display_name=display_name, xdotool=xdotool)
        if xdotool is not None
        else None
    )
    if typer is None:
        log.warning("xdotool not found: the type backend is unavailable")
    return AutoInjector(
        session, clipboard, typer, config, clipboard_only=ClipboardOnlyInjector(owner)
    )
