"""Clipboard + paste injection (docs/08-text-injection.md §8.5).

`ClipboardOwner` is the `clipboard-owner` thread with X11 connection #3 and a 1x1 window: it
serves CLIPBOARD, saves another client's content and records paste requests. The pipeline
thread talks to it only through methods returning `Future`s. `ClipboardPasteInjector` runs in
the pipeline thread and uses `X11Session` (connection #2) for the window and XTest.
"""

import contextlib
import logging
import os
import queue
import select
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any, Literal, TypeVar

from Xlib import X, Xatom, display
from Xlib.error import ConnectionClosedError, XError
from Xlib.protocol import event as xevent
from Xlib.protocol import request

from local_stt.cancellation import CancellationToken, Cancelled
from local_stt.config import Config
from local_stt.hotkeys.spec import parse_hotkey
from local_stt.inject.x11util import (
    POLL_S,
    TargetWindow,
    X11InjectError,
    X11Session,
    parse_shortcut,
)
from local_stt.interfaces import InjectResult

log = logging.getLogger("local_stt.inject")

T = TypeVar("T")

TEXT_TARGETS = ("UTF8_STRING", "text/plain;charset=utf-8", "TEXT", "STRING")
META_TARGETS = frozenset({"TARGETS", "TIMESTAMP", "MULTIPLE", "SAVE_TARGETS", "DELETE"})
TARGETS_TIMEOUT_S = 0.3
SAVE_TIMEOUT_S = 0.5
MAX_TARGETS = 32
# 08 §8.5 says 256 KiB; python-xlib does not use BIG-REQUESTS, so one ChangeProperty request
# carries at most 65535 * 4 bytes including its 24-byte header (tested on Xvfb 2026-10-03).
# A restored target must fit in one request because we do not implement INCR.
MAX_TARGET_BYTES = 65535 * 4 - 24
MAX_TOTAL_BYTES = 1024 * 1024
MAX_TEXT_BYTES = 64 * 1024
CONFIRM_GRACE_S = 0.15  # applications sometimes fetch several targets
_PROPERTY = "LOCAL_STT_SELECTION"


@dataclass(frozen=True)
class TargetData:
    type: int  # property type atom
    format: int  # 8, 16 or 32
    value: Any  # bytes for format 8, a sequence of ints otherwise

    @property
    def size(self) -> int:
        return len(self.value) * self.format // 8


Saved = dict[int, TargetData]  # target atom → content; atoms are server-global


@dataclass(frozen=True)
class SaveResult:
    kind: Literal["saved", "none", "unrestorable"]
    saved: Saved | None  # what restoration serves (None: no owner afterwards)
    rollback: Saved | None  # content right before this operation (cancel after takeover)
    reason: str = ""


class ClipboardUnrestorable(Exception):
    """The clipboard cannot be saved faithfully (limits, INCR); use the `type` backend."""


@dataclass
class _Waiter:
    t_sent: float
    client_base: int
    mask: int
    future: "Future[bool]" = field(default_factory=Future)


class ClipboardOwner:
    def __init__(
        self,
        display_name: str | None = None,
        *,
        on_connection_lost: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._d: Any = display.Display(display_name)
        self._on_connection_lost = on_connection_lost
        self._clock = clock
        root = self._d.screen().root
        self._window = root.create_window(
            -10, -10, 1, 1, 0, X.CopyFromParent, event_mask=X.PropertyChangeMask
        )
        self._clipboard = self._atom("CLIPBOARD")
        self._targets = self._atom("TARGETS")
        self._incr = self._atom("INCR")
        self._property = self._atom(_PROPERTY)
        self._text_targets = {self._atom(name) for name in TEXT_TARGETS}
        self._meta = {self._atom(name) for name in META_TARGETS}
        self._d.sync()
        # State below is touched only by the owner thread.
        self._owned_since: int | None = None  # server time of our ownership
        self._served: Saved | None = None
        self._serving_text = False
        self._user_saved: Saved | None = None
        self._waiters: list[_Waiter] = []
        # Text requests since our last takeover: a paste may be handled before the waiter
        # command is, so a new waiter also checks this history.
        self._requests: list[tuple[float, int]] = []
        self._commands: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        self._wake_r, self._wake_w = os.pipe()
        self._stop = False
        self._thread: threading.Thread | None = None

    def _atom(self, name: str) -> int:
        atom: int = self._d.intern_atom(name)
        return atom

    # --- lifecycle ----------------------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="clipboard-owner", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        def stop() -> None:
            self._stop = True

        self._call(stop)
        if self._thread is not None:
            self._thread.join()
        os.close(self._wake_r)
        os.close(self._wake_w)
        self._d.close()

    # --- API for the pipeline thread (each returns a Future) ------------------------------

    def save(self) -> "Future[SaveResult]":
        return self._call(self._save)

    def take_text(self, text: str) -> "Future[bool]":
        return self._call(lambda: self._take_text(text))

    def restore(self, saved: Saved | None) -> "Future[bool]":
        """Serve `saved` (None: give up ownership), only while we still own CLIPBOARD."""
        return self._call(lambda: self._restore(saved))

    def wait_confirmation(self, t_sent: float, window: int, mask: int) -> "Future[bool]":
        """Resolves True on a text-target request after `t_sent` from the X client that owns
        `window` (08 §8.5 step 7); the caller applies the timeout and calls `cancel_wait`."""
        waiter = _Waiter(t_sent, window & ~mask, mask)
        self._call(lambda: self._add_waiter(waiter))
        return waiter.future

    def cancel_wait(self, future: "Future[bool]") -> None:
        def drop() -> None:
            self._waiters = [w for w in self._waiters if w.future is not future]

        self._call(drop)

    def _call(self, fn: Callable[[], T]) -> "Future[T]":
        future: Future[T] = Future()

        def run() -> None:
            if not future.set_running_or_notify_cancel():
                return
            try:
                future.set_result(fn())
            except BaseException as e:
                future.set_exception(e)

        self._commands.put(run)
        os.write(self._wake_w, b"x")
        return future

    # --- owner thread ---------------------------------------------------------------------

    def _run(self) -> None:
        fd = self._d.fileno()
        try:
            while not self._stop:
                self._drain_events()
                while not self._stop and not self._commands.empty():
                    self._commands.get()()
                    self._drain_events()
                if self._stop:
                    break
                ready, _, _ = select.select([fd, self._wake_r], [], [], 1.0)
                if self._wake_r in ready:
                    os.read(self._wake_r, 4096)
        except ConnectionClosedError as e:
            log.error("clipboard X11 connection lost: %s", e)
            self._fail_pending(e)
            if self._on_connection_lost is not None:
                self._on_connection_lost()

    def _fail_pending(self, error: BaseException) -> None:
        for waiter in self._waiters:
            waiter.future.set_result(False)
        while not self._commands.empty():
            self._commands.get()()  # each fails on the closed connection and sets its future

    def _drain_events(self) -> None:
        while self._d.pending_events():
            self._handle(self._d.next_event())

    def _next_event(self, deadline: float) -> Any | None:
        """Next X event before `deadline`, or None; used inside commands."""
        while not self._d.pending_events():
            remaining = deadline - self._clock()
            if remaining <= 0:
                return None
            select.select([self._d.fileno()], [], [], remaining)  # pending_events() reads
        return self._d.next_event()

    def _handle(self, ev: Any) -> None:
        if ev.type == X.SelectionRequest:
            self._on_request(ev)
        elif ev.type == X.SelectionClear and ev.atom == self._clipboard:
            # Another application took the clipboard: never reclaim it (08 §8.5 step 8).
            self._owned_since = None
            self._served = None
            self._serving_text = False
            self._user_saved = None

    def _on_request(self, ev: Any) -> None:
        prop = ev.property or ev.target  # obsolete clients send None
        served = self._served
        ok = False
        if (
            served is not None
            and self._owned_since is not None
            and ev.selection == self._clipboard
            and (ev.time == X.CurrentTime or ev.time >= self._owned_since)
        ):
            try:
                if ev.target == self._targets:
                    atoms = [self._targets, *served]
                    ev.requestor.change_property(prop, Xatom.ATOM, 32, atoms)
                    ok = True
                elif ev.target in served:
                    data = served[ev.target]
                    ev.requestor.change_property(prop, data.type, data.format, data.value)
                    ok = True
            except XError as e:
                log.debug("clipboard request from 0x%x failed: %s", ev.requestor.id, e)
        notify = xevent.SelectionNotify(
            time=ev.time,
            requestor=ev.requestor,
            selection=ev.selection,
            target=ev.target,
            property=prop if ok else X.NONE,
        )
        try:
            ev.requestor.send_event(notify)
            self._d.flush()
        except XError as e:
            log.debug("SelectionNotify to 0x%x failed: %s", ev.requestor.id, e)
        if ok and self._serving_text and ev.target in self._text_targets:
            self._record_request(ev.requestor.id)

    def _record_request(self, requestor: int) -> None:
        now = self._clock()
        self._requests.append((now, requestor))
        for waiter in list(self._waiters):
            if self._confirms(waiter, now, requestor):
                self._waiters.remove(waiter)
                waiter.future.set_result(True)

    def _add_waiter(self, waiter: _Waiter) -> None:
        if any(self._confirms(waiter, at, requestor) for at, requestor in self._requests):
            waiter.future.set_result(True)
        else:
            self._waiters.append(waiter)

    @staticmethod
    def _confirms(waiter: _Waiter, at: float, requestor: int) -> bool:
        # After t_sent (clipboard managers fetch right after the owner changes) and from the
        # X client of the active window: resource IDs carry the client base.
        return at > waiter.t_sent and requestor & ~waiter.mask == waiter.client_base

    def _server_time(self) -> int:
        """A server timestamp from a zero-length property append (ICCCM §2.1)."""
        self._window.change_property(self._property, Xatom.STRING, 8, b"", X.PropModeAppend)
        self._d.flush()
        deadline = self._clock() + 1.0
        while (ev := self._next_event(deadline)) is not None:
            if ev.type == X.PropertyNotify and ev.window.id == self._window.id:
                stamp: int = ev.time
                return stamp
            self._handle(ev)
        return int(X.CurrentTime)

    def _owner_is_us(self) -> bool:
        owner = self._d.get_selection_owner(self._clipboard)
        return bool(owner != X.NONE and owner.id == self._window.id)

    # --- commands -------------------------------------------------------------------------

    def _save(self) -> SaveResult:
        if self._owned_since is not None and self._owner_is_us():
            current = dict(self._served) if self._served is not None else None
            return SaveResult("saved" if self._user_saved else "none", self._user_saved, current)
        self._owned_since = None
        if self._d.get_selection_owner(self._clipboard) == X.NONE:
            self._user_saved = None
            return SaveResult("none", None, None)

        deadline = self._clock() + SAVE_TIMEOUT_S
        targets = self._convert(self._targets, min(deadline, self._clock() + TARGETS_TIMEOUT_S))
        if targets is None:
            self._user_saved = None
            return SaveResult("none", None, None, "no TARGETS response")
        if targets.type == self._incr:
            return SaveResult("unrestorable", None, None, "INCR")
        wanted = [int(a) for a in targets.value if int(a) not in self._meta]
        if len(wanted) > MAX_TARGETS:
            return SaveResult("unrestorable", None, None, f"{len(wanted)} targets")

        saved: Saved = {}
        total = 0
        for target in wanted:
            data = self._convert(target, deadline)
            if self._clock() >= deadline:
                return SaveResult("unrestorable", None, None, "save timed out")
            if data is None:
                continue  # the owner refuses this target; it cannot be served anyway
            if data.type == self._incr:
                return SaveResult("unrestorable", None, None, "INCR")
            total += data.size
            if data.size > MAX_TARGET_BYTES or total > MAX_TOTAL_BYTES:
                return SaveResult("unrestorable", None, None, "content too large")
            saved[target] = data
        self._user_saved = saved or None
        return SaveResult("saved" if saved else "none", self._user_saved, self._user_saved)

    def _convert(self, target: int, deadline: float) -> TargetData | None:
        self._window.delete_property(self._property)
        self._window.convert_selection(self._clipboard, target, self._property, X.CurrentTime)
        self._d.flush()
        while (ev := self._next_event(deadline)) is not None:
            if ev.type == X.SelectionNotify and ev.requestor.id == self._window.id:
                if ev.property == X.NONE or ev.target != target:
                    return None
                prop = self._window.get_full_property(self._property, X.AnyPropertyType)
                self._window.delete_property(self._property)
                if prop is None:
                    return None
                return TargetData(prop.property_type, prop.format, prop.value)
            self._handle(ev)
        return None

    def _take_text(self, text: str) -> bool:
        utf8 = text.encode("utf-8")
        content: Saved = {
            self._atom("UTF8_STRING"): TargetData(self._atom("UTF8_STRING"), 8, utf8),
            self._atom("text/plain;charset=utf-8"): TargetData(
                self._atom("text/plain;charset=utf-8"), 8, utf8
            ),
            self._atom("TEXT"): TargetData(self._atom("UTF8_STRING"), 8, utf8),
        }
        with contextlib.suppress(UnicodeEncodeError):  # STRING only if the text fits Latin-1
            content[Xatom.STRING] = TargetData(Xatom.STRING, 8, text.encode("latin-1"))
        stamp = self._server_time()
        self._window.set_selection_owner(self._clipboard, stamp)
        self._d.flush()
        if not self._owner_is_us():
            return False
        self._owned_since = stamp
        self._served = content
        self._serving_text = True
        self._requests.clear()
        return True

    def _restore(self, saved: Saved | None) -> bool:
        if self._owned_since is None or not self._owner_is_us():
            return False  # the user (or an app) took the clipboard meanwhile: keep theirs
        self._serving_text = False
        if saved is None:
            # python-xlib only offers Window.set_selection_owner (owner = that window).
            request.SetSelectionOwner(
                display=self._d.display,
                window=X.NONE,
                selection=self._clipboard,
                time=self._server_time(),
            )
            self._d.flush()
            self._owned_since = None
            self._served = None
            self._user_saved = None
        else:
            self._served = dict(saved)
        return True


def _await(future: "Future[T]", cancel: CancellationToken | None, timeout_s: float) -> T | None:
    """Waits for `future`, checking `cancel` every 20 ms; None when cancelled."""
    deadline = time.monotonic() + timeout_s
    while True:
        if cancel is not None and cancel.cancelled:
            return None
        try:
            return future.result(timeout=min(POLL_S, max(0.0, deadline - time.monotonic())))
        except FutureTimeout:
            if time.monotonic() >= deadline:
                raise


class ClipboardPasteInjector:
    """Implements `interfaces.Injector` (backend "clipboard"); used by the pipeline thread.

    With `type_fallback`, an unrestorable clipboard raises `ClipboardUnrestorable` before
    anything changes (AutoInjector switches to `type`); without it the text is pasted and
    the clipboard is not restored (08 §8.7: xdotool missing).
    """

    backend = "clipboard"

    def __init__(
        self,
        owner: ClipboardOwner,
        session: X11Session,
        config: Config,
        *,
        type_fallback: bool,
    ):
        self._owner = owner
        self._x = session
        self.type_fallback = type_fallback
        self.update_config(config)

    def update_config(self, config: Config) -> None:
        self._config = config.injection
        _, ptt_keysym = parse_hotkey(config.hotkeys.push_to_talk)
        self._ptt_keycodes = self._x.keycodes(ptt_keysym)

    def inject(self, text: str, *, cancel: CancellationToken) -> InjectResult:
        try:
            return self._inject(text, cancel)
        except ClipboardUnrestorable:
            raise
        except (X11InjectError, XError, ConnectionClosedError, FutureTimeout) as e:
            log.error("clipboard injection failed: %s", e)
            return self._result(text, None, ok=False, error=str(e))

    def _result(
        self, text: str, target: TargetWindow | None, *, ok: bool, **kwargs: Any
    ) -> InjectResult:
        return InjectResult(
            ok=ok,
            backend=self.backend,
            chars=kwargs.pop("chars", len(text) if ok else 0),
            window_class=target.wm_class if target is not None else None,
            left_in_clipboard=kwargs.pop("left_in_clipboard", False),
            error=kwargs.pop("error", None),
            **kwargs,
        )

    def _cancelled(self, text: str, target: TargetWindow | None) -> InjectResult:
        return self._result(text, target, ok=False, cancelled=True)

    def _inject(self, text: str, cancel: CancellationToken) -> InjectResult:
        cfg = self._config
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise ClipboardUnrestorable("text longer than 64 KiB (no INCR support)")
        # 1. modifiers and a held PTT key
        if not self._x.wait_for_keys_released(
            cancel, ptt_keycodes=self._ptt_keycodes, modifier_wait_s=cfg.modifier_wait_ms / 1000
        ):
            return self._cancelled(text, None)

        # 2. target window
        target = self._x.active_window()
        if target is None:
            try:
                with cancel.operation():
                    taken = _await(self._owner.take_text(text), None, 2.0)
            except Cancelled:
                return self._cancelled(text, None)
            log.info("no active window: text left in the clipboard")
            return self._result(text, None, ok=False, left_in_clipboard=bool(taken), no_target=True)
        log.debug("paste target WM_CLASS=%s", target.wm_class)

        # 3. save the clipboard
        save = _await(self._owner.save(), cancel, SAVE_TIMEOUT_S + 1.0)
        if save is None:
            return self._cancelled(text, target)
        restorable = save.kind != "unrestorable"
        if not restorable:
            if self.type_fallback:
                raise ClipboardUnrestorable(save.reason)
            log.warning("clipboard cannot be restored (%s): pasting without restoring", save.reason)

        # 4. take ownership
        if cancel.cancelled:
            return self._cancelled(text, target)
        if not _await(self._owner.take_text(text), None, 2.0):
            return self._result(text, target, ok=False, error="could not own CLIPBOARD")

        # 5.-8. shortcut, confirmation and restoration form one input operation (08 §8.3)
        shortcut = parse_shortcut(self._shortcut_for(target))
        try:
            with cancel.operation():
                t_sent = time.monotonic()
                confirmation = self._owner.wait_confirmation(
                    t_sent, target.window, self._x.resource_id_mask
                )
                self._x.send_shortcut(shortcut)
                try:
                    confirmed = confirmation.result(timeout=cfg.paste_timeout_ms / 1000)
                except FutureTimeout:
                    self._owner.cancel_wait(confirmation)
                    confirmed = False
                if not confirmed:
                    log.warning("paste not confirmed by %s: text left in the clipboard",
                                target.wm_class)  # fmt: skip
                    return self._result(text, target, ok=False, left_in_clipboard=True)
                time.sleep(CONFIRM_GRACE_S)
                if cfg.restore_clipboard and restorable:
                    _await(self._owner.restore(save.saved), None, 2.0)
                return self._result(text, target, ok=True)
        except Cancelled:
            # Cancelled after the takeover but before the shortcut: put back what was there,
            # regardless of restore_clipboard, unless someone else took the clipboard.
            _await(self._owner.restore(save.rollback), None, 2.0)
            return self._cancelled(text, target)

    def _shortcut_for(self, target: TargetWindow) -> str:
        cfg = self._config
        names = {target.wm_class.lower(), target.wm_instance.lower()}
        for window_class, shortcut in cfg.paste_shortcut_overrides.items():
            if window_class.lower() in names:
                return shortcut
        if names & {c.lower() for c in cfg.terminal_window_classes}:
            return "Ctrl+Shift+V"
        return "Ctrl+V"
