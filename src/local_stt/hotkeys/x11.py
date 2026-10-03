"""Global hotkeys through passive key grabs on the root window (docs/07-hotkeys-x11.md §7.3-7.4).

python-xlib is untyped; everything public here has typed signatures (14 §14.1). The listener
thread owns its X connection (02 §2.2: connection #1): grabbing and ungrabbing happen only
there, other threads submit commands through a queue and wake the loop through a pipe.
"""

import itertools
import logging
import os
import queue
import select
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any

from Xlib import XK, X, display
from Xlib.error import BadAccess, CatchError, ConnectionClosedError

from local_stt.config import HotkeysConfig
from local_stt.events import (
    ContinuousToggle,
    Event,
    PttCancelKey,
    PttPressed,
    PttReleased,
    X11ConnectionLost,
)
from local_stt.hotkeys.spec import parse_hotkey
from local_stt.interfaces import HotkeyProblem

log = logging.getLogger("local_stt.hotkeys")

LOST_RELEASE_CHECK_S = 0.25  # 07 §7.3: keymap check while PTT is held
COMMAND_TIMEOUT_S = 5.0
# Keysyms whose modifier row gives the mask of each configured modifier name.
_MODIFIER_KEYSYMS = {
    "Shift": ("Shift_L", "Shift_R"),
    "Ctrl": ("Control_L", "Control_R"),
    "Alt": ("Alt_L", "Alt_R", "Meta_L"),
    "Super": ("Super_L", "Super_R"),
}


class HotkeyConnectError(Exception):
    """The X display cannot be opened at startup (07 §7.5: exit 1, systemd retries)."""


@dataclass(frozen=True)
class Binding:
    keycode: int
    mods: int  # X modifier mask without lock modifiers


class KeyRouter:
    """Key events → controller events (07 §7.3, press/release table); no X calls.

    Auto-repeat pairs are filtered out by the listener before they reach the router.
    """

    def __init__(self) -> None:
        self.ptt: Binding | None = None
        self.toggle: Binding | None = None
        self.cancel_keycode = 0
        self.relevant = 0  # masks of Shift, Ctrl, Alt, Super
        self.ptt_down = False

    def configure(
        self, ptt: Binding | None, toggle: Binding | None, cancel_keycode: int, relevant: int
    ) -> None:
        if self.ptt_down and (ptt is None or self.ptt is None or ptt.keycode != self.ptt.keycode):
            self.ptt_down = False  # the old key's release would no longer match
        self.ptt, self.toggle = ptt, toggle
        self.cancel_keycode, self.relevant = cancel_keycode, relevant

    def press(self, keycode: int, state: int, at: float) -> Event | None:
        mods = state & self.relevant
        if self.ptt_down:
            # Active grab: every key reaches us, only the cancel key means something.
            return PttCancelKey() if keycode == self.cancel_keycode else None
        if self.ptt is not None and (keycode, mods) == (self.ptt.keycode, self.ptt.mods):
            self.ptt_down = True
            return PttPressed(at)
        if self.toggle is not None and (keycode, mods) == (self.toggle.keycode, self.toggle.mods):
            return ContinuousToggle()
        return None

    def release(self, keycode: int, at: float) -> Event | None:
        # By keycode only: the state includes the released key's own modifier (07 §7.3).
        if self.ptt_down and self.ptt is not None and keycode == self.ptt.keycode:
            self.ptt_down = False
            return PttReleased(at)
        return None

    def lost_release(self, at: float) -> Event | None:
        if not self.ptt_down:
            return None
        self.ptt_down = False
        return PttReleased(at)


@dataclass
class _Command:
    kind: str  # "apply" | "stop"
    config: HotkeysConfig | None
    done: "Future[list[HotkeyProblem]]"


class X11GrabHotkeys:
    """`HotkeyBackend` for X11 (07 §7.6)."""

    def __init__(self, display_name: str | None = None):
        try:
            self._d: Any = display.Display(display_name)
        except Exception as e:  # Xlib raises several unrelated types on connect
            raise HotkeyConnectError(f"cannot connect to X display {display_name!r}: {e}") from e
        self._root = self._d.screen().root
        self._router = KeyRouter()
        self._grabs: list[tuple[int, int]] = []  # (keycode, full mask) actually grabbed
        self._sink: Callable[[Event], None] = lambda _: None
        self._commands: queue.SimpleQueue[_Command] = queue.SimpleQueue()
        self._wake_r, self._wake_w = os.pipe()
        os.set_blocking(self._wake_r, False)
        self._lock = threading.Lock()
        self._closed = False  # set by the listener thread when it ends; guarded by _lock
        self._thread = threading.Thread(target=self._run, name="hotkeys", daemon=True)

    # --- called from other threads -------------------------------------------------------

    def start(self, sink: Callable[[Event], None]) -> None:
        self._sink = sink
        self._thread.start()

    def apply(self, config: HotkeysConfig) -> list[HotkeyProblem]:
        result = self._submit("apply", config)
        return result if result is not None else []

    def stop(self) -> None:
        if not self._thread.is_alive() and not self._closed:  # never started
            self._closed = True
            self._close_display()
            os.close(self._wake_r)
            os.close(self._wake_w)
            return
        self._submit("stop", None)
        self._thread.join(COMMAND_TIMEOUT_S)

    def _submit(self, kind: str, config: HotkeysConfig | None) -> list[HotkeyProblem] | None:
        done: Future[list[HotkeyProblem]] = Future()
        with self._lock:
            if self._closed:
                log.debug("hotkeys: %s ignored, listener has ended", kind)
                return None
            self._commands.put(_Command(kind, config, done))
            os.write(self._wake_w, b"x")  # under the lock: the thread closes the pipe at exit
        return done.result(COMMAND_TIMEOUT_S)

    # --- listener thread -----------------------------------------------------------------

    def _run(self) -> None:
        x_alive = True
        try:
            self._loop()
        except ConnectionClosedError as e:
            x_alive = False
            log.warning("X connection lost: %s", e)
            self._sink(X11ConnectionLost())
        finally:
            with self._lock:
                self._closed = True
            while not self._commands.empty():
                self._commands.get().done.set_result([])
            if x_alive:
                self._close_display()
            os.close(self._wake_r)
            os.close(self._wake_w)

    def _loop(self) -> None:
        next_check = 0.0
        while True:
            self._drain_events()
            if self._router.ptt_down:
                now = time.monotonic()
                if now >= next_check:
                    self._check_lost_release()
                    next_check = now + LOST_RELEASE_CHECK_S
                    if self._d.pending_events():
                        continue  # read along with the keymap reply
                timeout: float | None = max(0.0, next_check - time.monotonic())
            else:
                timeout = None
            ready, _, _ = select.select([self._d.fileno(), self._wake_r], [], [], timeout)
            if self._wake_r in ready:
                while True:
                    try:
                        if not os.read(self._wake_r, 64):
                            break
                    except BlockingIOError:
                        break
            while not self._commands.empty():
                cmd = self._commands.get()
                try:
                    if cmd.kind == "stop":
                        self._ungrab_all()
                        self._d.sync()
                        cmd.done.set_result([])
                        return
                    assert cmd.config is not None
                    cmd.done.set_result(self._apply(cmd.config))
                except ConnectionClosedError:
                    cmd.done.set_result([])  # the caller is not at fault; _run reports the loss
                    raise
                except BaseException as e:
                    cmd.done.set_exception(e)
                    raise

    def _drain_events(self) -> None:
        # Any round trip (refresh_keyboard_mapping, query_keymap, grabs) can read events into
        # Xlib's queue, after which select() on the socket would not wake for them.
        while self._d.pending_events():
            batch: list[Any] = []
            while self._d.pending_events():
                batch.append(self._d.next_event())
            self._process(batch)

    def _process(self, batch: list[Any]) -> None:
        i = 0
        while i < len(batch):
            ev = batch[i]
            if ev.type == X.KeyRelease:
                if i + 1 == len(batch):
                    while self._d.pending_events():  # look ahead past the last read
                        batch.append(self._d.next_event())
                nxt = batch[i + 1] if i + 1 < len(batch) else None
                if (
                    nxt is not None
                    and nxt.type == X.KeyPress
                    and nxt.detail == ev.detail
                    and nxt.time == ev.time
                ):
                    i += 2  # auto-repeat: same keycode and timestamp (07 §7.3)
                    continue
                self._emit(self._router.release(ev.detail, time.monotonic()))
            elif ev.type == X.KeyPress:
                self._emit(self._router.press(ev.detail, ev.state, time.monotonic()))
            elif ev.type == X.MappingNotify:
                self._d.refresh_keyboard_mapping(ev)
                log.debug("keyboard mapping changed (request %d)", ev.request)
            i += 1

    def _check_lost_release(self) -> None:
        ptt = self._router.ptt
        assert ptt is not None
        keymap = self._d.query_keymap()  # 32 bytes, one bit per keycode
        if self._d.pending_events():
            return  # events read with the reply come first; check again on the next pass
        if not keymap[ptt.keycode // 8] & (1 << (ptt.keycode % 8)):
            log.warning("push-to-talk release lost; releasing")
            self._emit(self._router.lost_release(time.monotonic()))

    def _emit(self, event: Event | None) -> None:
        if event is not None:
            log.debug("hotkey: %s", type(event).__name__)
            self._sink(event)

    # --- grabs ---------------------------------------------------------------------------

    def _apply(self, config: HotkeysConfig) -> list[HotkeyProblem]:
        self._ungrab_all()
        if not config.enabled:
            self._router.configure(None, None, 0, 0)
            self._d.sync()
            log.info("hotkeys disabled")
            return []
        masks = self._modifier_masks()
        locks = [
            m for m in (X.LockMask, masks.get("Num_Lock", 0), masks.get("Scroll_Lock", 0)) if m
        ]
        lock_combos = sorted(
            {sum(c) for n in range(len(locks) + 1) for c in itertools.combinations(locks, n)}
        )
        relevant = 0
        for name in _MODIFIER_KEYSYMS:
            relevant |= masks.get(name, 0)

        problems: list[HotkeyProblem] = []
        bindings: dict[str, Binding] = {}
        for name in ("push_to_talk", "continuous_toggle", "ptt_cancel_key"):
            value = getattr(config, name)
            mods, keysym = parse_hotkey(value)  # syntax was validated with the config
            keycode = self._d.keysym_to_keycode(XK.string_to_keysym(keysym))
            missing = sorted(m for m in mods if m not in masks)
            if not keycode:
                reason = f"{keysym} has no keycode in the current keyboard map"
            elif missing:
                reason = f"modifier {missing[0]} is not mapped"
            elif name != "ptt_cancel_key" and not self._grab(keycode, mods, masks, lock_combos):
                reason = "already grabbed by another client"
            else:
                bindings[name] = Binding(keycode, sum(masks[m] for m in mods))
                continue
            log.error("hotkey %s (%s): %s", value, name, reason)
            problems.append(HotkeyProblem(name, value, reason))

        cancel = bindings.get("ptt_cancel_key")
        self._router.configure(
            bindings.get("push_to_talk"),
            bindings.get("continuous_toggle"),
            cancel.keycode if cancel else 0,
            relevant,
        )
        log.info(
            "hotkeys grabbed: %d of 2", len({"push_to_talk", "continuous_toggle"} & bindings.keys())
        )
        return problems

    def _grab(
        self, keycode: int, mods: frozenset[str], masks: dict[str, int], lock_combos: list[int]
    ) -> bool:
        base = sum(masks[m] for m in mods)
        catch = CatchError(BadAccess)
        for extra in lock_combos:
            self._root.grab_key(
                keycode, base | extra, False, X.GrabModeAsync, X.GrabModeAsync, onerror=catch
            )
        self._d.sync()
        grabs = [(keycode, base | extra) for extra in lock_combos]
        if catch.get_error() is not None:
            for code, mask in grabs:  # a partial grab would work only with some lock states
                self._root.ungrab_key(code, mask)
            self._d.sync()
            return False
        self._grabs += grabs
        return True

    def _ungrab_all(self) -> None:
        for keycode, mask in self._grabs:
            self._root.ungrab_key(keycode, mask)
        self._grabs = []

    def _modifier_masks(self) -> dict[str, int]:
        """Modifier and lock names → mask, from the current modifier mapping (07 §7.2-7.3)."""
        rows = self._d.get_modifier_mapping()  # Shift, Lock, Control, Mod1..Mod5
        names = {**_MODIFIER_KEYSYMS, "Num_Lock": ("Num_Lock",), "Scroll_Lock": ("Scroll_Lock",)}
        masks: dict[str, int] = {}
        for name, keysyms in names.items():
            codes = {self._d.keysym_to_keycode(XK.string_to_keysym(k)) for k in keysyms} - {0}
            for index, row in enumerate(rows):
                if codes & set(row):
                    masks[name] = 1 << index
                    break
        return masks

    def _close_display(self) -> None:
        try:
            self._d.close()
        except Exception as e:  # already broken connection
            log.debug("closing X connection: %s", e)
