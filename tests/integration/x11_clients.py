"""Xvfb and helper X clients for needs_x11 tests (docs/14-tests.md §14.2 item 2).

Every helper connects to the display name it is given; nothing reads $DISPLAY, so a test can
never type into the developer's real session.
"""

import os
import select
import shutil
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from Xlib import XK, X, Xatom, display
from Xlib.ext import xfixes, xtest
from Xlib.protocol import event as xevent

WAIT_S = 5.0


@contextmanager
def xvfb() -> Iterator[str]:
    """Starts a private Xvfb and yields its display name (":N")."""
    if shutil.which("Xvfb") is None:
        raise RuntimeError("Xvfb is not installed (scripts/install.sh --dev)")
    read_fd, write_fd = os.pipe()
    proc = subprocess.Popen(
        ["Xvfb", "-displayfd", str(write_fd), "-nolisten", "tcp", "-screen", "0", "640x480x24"],
        pass_fds=(write_fd,),
        stderr=subprocess.DEVNULL,
    )
    os.close(write_fd)
    try:
        ready, _, _ = select.select([read_fd], [], [], WAIT_S)
        number = os.read(read_fd, 64).decode().strip() if ready else ""
        if not number:
            raise RuntimeError("Xvfb did not report a display number")
        name = f":{number}"
        real = os.environ.get("DISPLAY", "")
        assert name != real, f"refusing to use the real session display {real}"
        yield name
    finally:
        os.close(read_fd)
        proc.terminate()
        proc.wait(timeout=WAIT_S)


class _Client:
    """One X connection with a 1x1 window and an event thread."""

    def __init__(self, name: str, *, mapped: bool = False, event_mask: int = 0):
        self.d: Any = display.Display(name)
        self.root = self.d.screen().root
        self.window = self.root.create_window(
            0, 0, 10, 10, 0, X.CopyFromParent, event_mask=event_mask | X.PropertyChangeMask
        )
        if mapped:
            self.window.map()
        self.d.sync()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def atom(self, name: str) -> int:
        atom: int = self.d.intern_atom(name)
        return atom

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(WAIT_S)
        self.d.close()

    def _loop(self) -> None:
        fd = self.d.fileno()
        while not self._stop.is_set():
            while self.d.pending_events():
                self.handle(self.d.next_event())
            select.select([fd], [], [], 0.02)

    def handle(self, ev: Any) -> None:
        pass

    def server_time(self) -> int:
        self.window.change_property(self.atom("T"), Xatom.STRING, 8, b"", X.PropModeAppend)
        self.d.sync()
        while True:
            ev = self.d.next_event()
            if ev.type == X.PropertyNotify:
                stamp: int = ev.time
                return stamp


@dataclass
class Content:
    type: str
    format: int
    value: Any  # bytes (format 8) or a list of ints


class SelectionOwner(_Client):
    """Another application owning CLIPBOARD (Firefox-like content, INCR, slow replies)."""

    def __init__(
        self,
        name: str,
        targets: dict[str, Content],
        *,
        incr: bool = False,
        delay_s: float = 0.0,
    ):
        super().__init__(name)
        self.targets = targets
        self.incr = incr
        self.delay_s = delay_s
        self.lost = threading.Event()
        stamp = self.server_time()
        self.window.set_selection_owner(self.atom("CLIPBOARD"), stamp)
        self.d.sync()
        self.start()

    def handle(self, ev: Any) -> None:
        if ev.type == X.SelectionClear:
            self.lost.set()
            return
        if ev.type != X.SelectionRequest:
            return
        if self.delay_s:
            time.sleep(self.delay_s)
        prop = ev.property
        target = self.d.get_atom_name(ev.target)
        if self.incr and target != "TARGETS":
            ev.requestor.change_property(prop, self.atom("INCR"), 32, [1 << 20])
        elif target == "TARGETS":
            atoms = [self.atom(t) for t in ("TARGETS", "TIMESTAMP", *self.targets)]
            ev.requestor.change_property(prop, Xatom.ATOM, 32, atoms)
        elif target in self.targets:
            c = self.targets[target]
            # Large values in chunks: python-xlib has no BIG-REQUESTS, but a client using it
            # could set such a property in one request.
            chunk = 200_000
            mode = X.PropModeReplace
            for start in range(0, max(len(c.value), 1), chunk):
                part = c.value[start : start + chunk]
                ev.requestor.change_property(prop, self.atom(c.type), c.format, part, mode)
                mode = X.PropModeAppend
        else:
            prop = X.NONE
        ev.requestor.send_event(
            xevent.SelectionNotify(
                time=ev.time,
                requestor=ev.requestor,
                selection=ev.selection,
                target=ev.target,
                property=prop,
            )
        )
        self.d.flush()


def read_selection(name: str, target: str) -> tuple[str, int, Any] | None:
    """(type name, format, value) of CLIPBOARD converted to `target`, from a fresh client."""
    c = _Client(name)
    try:
        prop = c.atom("READ")
        c.window.convert_selection(c.atom("CLIPBOARD"), c.atom(target), prop, X.CurrentTime)
        c.d.flush()
        deadline = time.monotonic() + WAIT_S
        while time.monotonic() < deadline:
            if not c.d.pending_events():
                select.select([c.d.fileno()], [], [], 0.02)
                continue
            ev = c.d.next_event()
            if ev.type == X.SelectionNotify:
                if ev.property == X.NONE:
                    return None
                p = c.window.get_full_property(prop, X.AnyPropertyType)
                return c.d.get_atom_name(p.property_type), p.format, p.value
        raise TimeoutError(f"no SelectionNotify for {target}")
    finally:
        c.d.close()


def clipboard_owner(name: str) -> int:
    c = display.Display(name)
    try:
        owner = c.get_selection_owner(c.intern_atom("CLIPBOARD"))
        return 0 if owner == X.NONE else int(owner.id)
    finally:
        c.close()


_NAMES = ("Control_L", "Control_R", "Shift_L", "Shift_R", "Alt_L", "Super_L", "Return", "Linefeed")


def keysym_name(keysym: int) -> str:
    """Printable ASCII as the character, a few named keys, otherwise hex."""
    if 0x20 <= keysym <= 0x7E:
        return chr(keysym)
    for name in _NAMES:
        if XK.string_to_keysym(name) == keysym:
            return name
    return f"0x{keysym:x}"


@dataclass
class KeyEvent:
    keysym: str
    state: int


class Receiver(_Client):
    """A focused text field: on Ctrl+V (or Ctrl+Shift+V) it pastes CLIPBOARD UTF8_STRING."""

    def __init__(
        self,
        name: str,
        wm_class: tuple[str, str] = ("gedit", "Gedit"),
        *,
        pastes: bool = True,
        paste_via: "Paster | None" = None,
    ):
        super().__init__(name, mapped=True, event_mask=X.KeyPressMask)
        self.pastes = pastes
        self.paste_via = paste_via  # another connection reads the clipboard (Chromium/CEF)
        self.received: list[str] = []
        self.keys: list[KeyEvent] = []
        self.pasted = threading.Event()
        self.window.set_wm_class(*wm_class)
        self.root.change_property(
            self.atom("_NET_ACTIVE_WINDOW"), Xatom.WINDOW, 32, [self.window.id]
        )
        self.d.sync()
        self.window.set_input_focus(X.RevertToParent, X.CurrentTime)
        self.d.sync()
        self.start()

    def handle(self, ev: Any) -> None:
        if ev.type == X.KeyPress:
            keysym = keysym_name(self.d.keycode_to_keysym(ev.detail, 0))
            self.keys.append(KeyEvent(keysym, ev.state))
            if self.paste_via is not None and keysym == "v" and ev.state & X.ControlMask:
                self.paste_via.paste()
            elif self.pastes and keysym == "v" and ev.state & X.ControlMask:
                self.window.convert_selection(
                    self.atom("CLIPBOARD"), self.atom("UTF8_STRING"), self.atom("PASTE"), ev.time
                )
                self.d.flush()
        elif ev.type == X.SelectionNotify and ev.property != X.NONE:
            p = self.window.get_full_property(self.atom("PASTE"), X.AnyPropertyType)
            self.received.append(bytes(p.value).decode("utf-8"))
            self.pasted.set()


class Paster(_Client):
    """A second X connection that reads CLIPBOARD UTF8_STRING when asked."""

    def __init__(self, name: str):
        super().__init__(name)
        self.received: list[str] = []
        self.pasted = threading.Event()
        self.start()

    def paste(self) -> None:
        self.window.convert_selection(
            self.atom("CLIPBOARD"), self.atom("UTF8_STRING"), self.atom("PASTE"), X.CurrentTime
        )
        self.d.flush()

    def handle(self, ev: Any) -> None:
        if ev.type == X.SelectionNotify and ev.property != X.NONE:
            p = self.window.get_full_property(self.atom("PASTE"), X.AnyPropertyType)
            self.received.append(bytes(p.value).decode("utf-8"))
            self.pasted.set()


class ClipboardManager(_Client):
    """Fetches CLIPBOARD right after every owner change (XFixes), like GNOME managers."""

    def __init__(self, name: str):
        super().__init__(name)
        self.fetched = threading.Event()
        self.d.xfixes_query_version()
        self.d.xfixes_select_selection_input(
            self.window, self.atom("CLIPBOARD"), xfixes.XFixesSetSelectionOwnerNotifyMask
        )
        self.d.sync()
        self.start()

    def handle(self, ev: Any) -> None:
        # python-xlib builds extension event classes dynamically: isinstance() fails.
        if type(ev).__name__ == "SetSelectionOwnerNotify":
            self.window.convert_selection(
                self.atom("CLIPBOARD"), self.atom("UTF8_STRING"), self.atom("MGR"), X.CurrentTime
            )
            self.d.flush()
        elif ev.type == X.SelectionNotify and ev.property != X.NONE:
            self.fetched.set()


class TargetsWatcher(_Client):
    """Fetches TARGETS after every owner change (XFixes) and remembers them, like GTK4
    apps that enable “Paste” from cached formats (Nautilus)."""

    def __init__(self, name: str):
        super().__init__(name)
        self.snapshots: list[list[str]] = []
        self.d.xfixes_query_version()
        self.d.xfixes_select_selection_input(
            self.window, self.atom("CLIPBOARD"), xfixes.XFixesSetSelectionOwnerNotifyMask
        )
        self.d.sync()
        self.start()

    def handle(self, ev: Any) -> None:
        if type(ev).__name__ == "SetSelectionOwnerNotify":
            self.window.convert_selection(
                self.atom("CLIPBOARD"), self.atom("TARGETS"), self.atom("WATCH"), X.CurrentTime
            )
            self.d.flush()
        elif ev.type == X.SelectionNotify and ev.property != X.NONE:
            p = self.window.get_full_property(self.atom("WATCH"), X.AnyPropertyType)
            self.snapshots.append([self.d.get_atom_name(a) for a in p.value])

    def wait_for(self, target: str) -> bool:
        deadline = time.monotonic() + WAIT_S
        while time.monotonic() < deadline:
            if self.snapshots and target in self.snapshots[-1]:
                return True
            time.sleep(0.02)
        return False


def set_desktop_active(name: str) -> None:
    """Makes a desktop-type window the active one (GNOME desktop icons)."""
    c = display.Display(name)
    root = c.screen().root
    w = root.create_window(0, 0, 10, 10, 0, X.CopyFromParent)
    w.change_property(
        c.intern_atom("_NET_WM_WINDOW_TYPE"),
        Xatom.ATOM,
        32,
        [c.intern_atom("_NET_WM_WINDOW_TYPE_DESKTOP")],
    )
    root.change_property(c.intern_atom("_NET_ACTIVE_WINDOW"), Xatom.WINDOW, 32, [w.id])
    c.sync()
    # the connection stays open so the window survives; closed with the server


@contextmanager
def held_key(name: str, keysym: str) -> Iterator[None]:
    """Holds a key down through XTest from a separate connection."""
    c = display.Display(name)
    code = c.keysym_to_keycode(XK.string_to_keysym(keysym))
    xtest.fake_input(c, X.KeyPress, code)
    c.sync()
    try:
        yield
    finally:
        xtest.fake_input(c, X.KeyRelease, code)
        c.sync()
        c.close()
