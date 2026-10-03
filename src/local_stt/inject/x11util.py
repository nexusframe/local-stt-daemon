"""X11 helpers for the pipeline thread's own connection (docs/08 §8.5 steps 1, 2, 6).

python-xlib is untyped; everything public here has typed signatures (14 §14.1). One
`X11Session` belongs to one thread (02 §2.2: connection #2, the pipeline).
"""

import logging
import time
from dataclasses import dataclass
from typing import Any

from Xlib import XK, X, display
from Xlib.error import XError
from Xlib.ext import xtest

from local_stt.cancellation import CancellationToken

log = logging.getLogger("local_stt.inject")

POLL_S = 0.02  # 08 §8.5 step 1: modifiers and the token are checked every 20 ms
KEY_STEP_S = 0.008  # pause after each XTest step
# Paste shortcut modifiers always use the left keys (08 §8.5 step 6); the validator keeps
# Control_L and Shift_L out of daemon hotkeys, so they cannot trigger our own grab.
SHORTCUT_KEYSYMS = {"Ctrl": "Control_L", "Shift": "Shift_L", "Alt": "Alt_L", "Super": "Super_L"}


class X11InjectError(Exception):
    """An X11 failure in the injector (08 §8.7: InjectResult(ok=False))."""


@dataclass(frozen=True)
class TargetWindow:
    window: int
    wm_instance: str
    wm_class: str


@dataclass(frozen=True)
class Shortcut:
    modifiers: tuple[str, ...]  # keysym names, pressed in order
    key: str  # keysym name


def parse_shortcut(text: str) -> Shortcut:
    """`Ctrl+Shift+V` → left-hand modifier keysyms and the lowercase key keysym."""
    *mods, key = text.split("+")
    keysym = key.lower() if len(key) == 1 else key
    return Shortcut(tuple(SHORTCUT_KEYSYMS[m] for m in mods), keysym)


class X11Session:
    def __init__(self, display_name: str | None = None):
        try:
            self._d: Any = display.Display(display_name)
        except Exception as e:  # Xlib raises several unrelated types on connect
            raise X11InjectError(f"cannot connect to X display {display_name!r}: {e}") from e
        if not self._d.has_extension("XTEST"):
            raise X11InjectError("the X server has no XTEST extension")
        self._root = self._d.screen().root
        self._atoms: dict[str, int] = {}

    def close(self) -> None:
        self._d.close()

    @property
    def resource_id_mask(self) -> int:
        mask: int = self._d.display.info.resource_id_mask
        return mask

    def atom(self, name: str) -> int:
        if name not in self._atoms:
            self._atoms[name] = self._d.intern_atom(name)
        return self._atoms[name]

    # --- step 2: target window ----------------------------------------------------------

    def active_window(self) -> TargetWindow | None:
        """`_NET_ACTIVE_WINDOW` with its WM_CLASS; None for no window or the desktop."""
        try:
            prop = self._root.get_full_property(self.atom("_NET_ACTIVE_WINDOW"), X.AnyPropertyType)
            window_id = int(prop.value[0]) if prop is not None and len(prop.value) else 0
            if window_id == 0:
                return None
            window = self._d.create_resource_object("window", window_id)
            if self._is_desktop(window):
                return None
            wm_class = window.get_wm_class() or ("", "")
        except XError as e:  # the window vanished meanwhile
            log.debug("active window lookup failed: %s", e)
            return None
        return TargetWindow(window_id, wm_class[0], wm_class[1])

    def _is_desktop(self, window: Any) -> bool:
        prop = window.get_full_property(self.atom("_NET_WM_WINDOW_TYPE"), X.AnyPropertyType)
        desktop = self.atom("_NET_WM_WINDOW_TYPE_DESKTOP")
        return prop is not None and desktop in list(prop.value)

    # --- step 1: waiting for keys -------------------------------------------------------

    def keycodes(self, keysym_name: str) -> set[int]:
        keysym = XK.string_to_keysym(keysym_name)
        return {code for code, _ in self._d.keysym_to_keycodes(keysym)} if keysym else set()

    def modifier_keycodes(self) -> set[int]:
        return {code for row in self._d.get_modifier_mapping() for code in row if code}

    def held(self, keycodes: set[int]) -> bool:
        keymap = self._d.query_keymap()  # 32 bytes, one bit per keycode
        return any(keymap[code // 8] & (1 << (code % 8)) for code in keycodes)

    def wait_for_keys_released(
        self, cancel: CancellationToken, *, ptt_keycodes: set[int], modifier_wait_s: float
    ) -> bool:
        """08 §8.5 step 1. Waits without a limit while the PTT key is held (a new recording;
        the active grab would swallow injected keys), then up to `modifier_wait_s` for the
        modifiers. Returns False if cancelled; never sends synthetic releases."""
        modifiers = self.modifier_keycodes() - ptt_keycodes
        deadline = time.monotonic() + modifier_wait_s
        while True:
            if cancel.cancelled:
                return False
            if self.held(ptt_keycodes):
                deadline = time.monotonic() + modifier_wait_s
            elif not self.held(modifiers):
                return True
            elif time.monotonic() >= deadline:
                log.warning("modifiers still held")
                return True
            if cancel.wait(POLL_S):
                return False

    # --- step 6: XTest ----------------------------------------------------------------

    def send_shortcut(self, shortcut: Shortcut) -> None:
        """Press modifiers, press/release the key, release modifiers (reverse order)."""
        codes = []
        for name in (*shortcut.modifiers, shortcut.key):
            keysym = XK.string_to_keysym(name)
            code = self._d.keysym_to_keycode(keysym) if keysym else 0
            if not code:
                raise X11InjectError(f"no keycode for {name}")
            codes.append(code)
        *mods, key = codes
        steps = [(X.KeyPress, c) for c in mods]
        steps += [(X.KeyPress, key), (X.KeyRelease, key)]
        steps += [(X.KeyRelease, c) for c in reversed(mods)]
        try:
            for kind, code in steps:
                xtest.fake_input(self._d, kind, code)
                self._d.sync()
                time.sleep(KEY_STEP_S)
        except XError as e:
            raise X11InjectError(f"XTest failed: {e}") from e
