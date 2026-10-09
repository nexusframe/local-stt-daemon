"""ClipboardOwner + ClipboardPasteInjector against Xvfb (docs/14-tests.md §14.2, needs_x11)."""

import dataclasses
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from Xlib import X

from local_stt.cancellation import CancellationToken
from local_stt.config import Config
from local_stt.inject import clipboard as clipboard_mod
from local_stt.inject.clipboard import (
    MAX_TARGET_BYTES,
    ClipboardOnlyInjector,
    ClipboardOwner,
    ClipboardPasteInjector,
    ClipboardUnrestorable,
)
from local_stt.inject.x11util import X11Session
from local_stt.interfaces import InjectResult

from .x11_clients import (
    WAIT_S,
    ClipboardManager,
    Content,
    Paster,
    Receiver,
    SelectionOwner,
    TargetsWatcher,
    clipboard_owner,
    held_key,
    read_selection,
    set_desktop_active,
    xvfb,
)

pytestmark = pytest.mark.needs_x11

TEXT = "Zażółć gęślą jaźń. "
FIREFOX_LIKE = {
    "UTF8_STRING": Content("UTF8_STRING", 8, b"Skopiowany tekst"),
    "text/html": Content("text/html", 8, "<b>Skopiowany</b> tekst".encode("utf-16-le")),
    "text/x-moz-url-priv": Content("text/x-moz-url-priv", 16, [104, 116, 116, 112]),
    "application/x-custom": Content("CARDINAL", 32, [1, 2, 0xFFFFFFFF]),
}


class Env:
    def __init__(self, name: str) -> None:
        self.name = name
        self.lost = threading.Event()
        self.owner = ClipboardOwner(name, on_connection_lost=self.lost.set)
        self.owner.start()
        self.session = X11Session(name)
        self.lock = threading.Lock()
        self.token = CancellationToken(self.lock)
        self.config = Config()
        self._clients: list[Any] = []

    def injector(self, *, type_fallback: bool = True, **injection: Any) -> ClipboardPasteInjector:
        config = dataclasses.replace(
            self.config, injection=dataclasses.replace(self.config.injection, **injection)
        )
        return ClipboardPasteInjector(self.owner, self.session, config, type_fallback=type_fallback)

    def inject(self, text: str = TEXT, **kwargs: Any) -> InjectResult:
        return self.injector(**kwargs).inject(text, cancel=self.token)

    def cancel(self) -> bool:
        with self.lock:
            return self.token.cancel_locked()

    def client(self, client: Any) -> Any:
        self._clients.append(client)
        return client

    def close(self) -> None:
        for c in self._clients:
            c.close()
        self.session.close()
        self.owner.stop()


# Every helper client runs in this test process, so real XRes PIDs would all match: the
# PID rule of 08 §8.5 step 7 is tested with assigned PIDs (client base → PID) instead.
PIDS: dict[int, int] = {}
REAL_CLIENT_PID = ClipboardOwner._client_pid


@pytest.fixture(autouse=True)
def assigned_pids(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[int, int]]:
    PIDS.clear()

    def client_pid(self: ClipboardOwner, resource: int) -> int | None:
        return PIDS.get(resource & ~self._d.display.info.resource_id_mask)

    monkeypatch.setattr(ClipboardOwner, "_client_pid", client_pid)
    yield PIDS


def base(client: Any) -> int:
    mask: int = client.d.display.info.resource_id_mask
    return int(client.window.id) & ~mask


@pytest.fixture
def env() -> Iterator[Env]:
    with xvfb() as name:
        e = Env(name)
        try:
            yield e
        finally:
            e.close()


def assert_restored(name: str, targets: dict[str, Content]) -> None:
    for target, content in targets.items():
        got = read_selection(name, target)
        assert got is not None, target
        type_name, fmt, value = got
        assert (type_name, fmt) == (content.type, content.format), target
        expected = content.value if fmt == 8 else list(content.value)
        assert (bytes(value) if fmt == 8 else list(value)) == expected, target


# --- paste and restoration ----------------------------------------------------------------


def test_paste_polish_text_and_restore_all_targets(env: Env) -> None:
    env.client(SelectionOwner(env.name, FIREFOX_LIKE))
    receiver = env.client(Receiver(env.name))
    result = env.inject()
    assert result == InjectResult(True, "clipboard", len(TEXT), "Gedit", False, None)
    assert receiver.received == [TEXT]
    assert_restored(env.name, FIREFOX_LIKE)


def test_empty_clipboard_has_no_owner_after_paste(env: Env) -> None:
    receiver = env.client(Receiver(env.name))
    assert env.inject().ok
    assert receiver.received == [TEXT]
    assert clipboard_owner(env.name) == 0


def test_restore_disabled_leaves_text(env: Env) -> None:
    env.client(SelectionOwner(env.name, FIREFOX_LIKE))
    env.client(Receiver(env.name))
    assert env.inject(restore_clipboard=False).ok
    assert read_selection(env.name, "UTF8_STRING") == ("UTF8_STRING", 8, TEXT.encode())


def test_terminal_gets_ctrl_shift_v(env: Env) -> None:
    receiver = env.client(Receiver(env.name, ("gnome-terminal-server", "Gnome-terminal")))
    assert env.inject().ok
    v = [k for k in receiver.keys if k.keysym == "v"]
    assert v and v[0].state & X.ShiftMask and v[0].state & X.ControlMask


def test_shortcut_override(env: Env) -> None:
    receiver = env.client(Receiver(env.name, ("emacs", "Emacs"), pastes=False))
    result = env.inject(paste_shortcut_overrides={"Emacs": "Ctrl+Y"}, paste_timeout_ms=200)
    assert [k.keysym for k in receiver.keys if k.keysym != "Control_L"] == ["y"]
    assert result.left_in_clipboard


NAUTILUS_LIKE = {
    "x-special/gnome-copied-files": Content(
        "x-special/gnome-copied-files", 8, b"copy\nfile:///tmp/a.txt"
    ),
    "text/uri-list": Content("text/uri-list", 8, b"file:///tmp/a.txt\r\n"),
}


def test_restore_announces_the_restored_targets(env: Env) -> None:
    """Acceptance 14.4 item 16: Nautilus caches TARGETS on owner changes; a silent swap
    left it believing the clipboard held our text."""
    env.client(SelectionOwner(env.name, NAUTILUS_LIKE))
    watcher = env.client(TargetsWatcher(env.name))
    receiver = env.client(Receiver(env.name))
    assert env.inject().ok
    assert receiver.received == [TEXT]
    assert watcher.wait_for("x-special/gnome-copied-files"), watcher.snapshots
    assert_restored(env.name, NAUTILUS_LIKE)


def test_restore_announcement_never_takes_back_a_newer_clipboard(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.client(SelectionOwner(env.name, NAUTILUS_LIKE))
    env.client(Receiver(env.name))
    newer = {"UTF8_STRING": Content("UTF8_STRING", 8, b"nowszy")}

    def restore_after_takeover(self: ClipboardOwner, saved: Any) -> bool:
        # Someone copies between our ownership check and the announcement.
        env.client(SelectionOwner(env.name, newer))
        monkeypatch.setattr(ClipboardOwner, "_owner_is_us", lambda self: True)
        return restore(self, saved)

    restore = ClipboardOwner._restore
    monkeypatch.setattr(ClipboardOwner, "_restore", restore_after_takeover)
    assert env.inject().ok
    assert_restored(env.name, newer)


# --- confirmation (08 §8.5 step 7) ----------------------------------------------------------


def test_no_receiver_leaves_text_and_keeps_user_content_for_next_paste(env: Env) -> None:
    env.client(SelectionOwner(env.name, FIREFOX_LIKE))
    deaf = env.client(Receiver(env.name, pastes=False))
    result = env.inject(paste_timeout_ms=300)
    assert (result.ok, result.left_in_clipboard, result.no_target) == (False, True, False)
    assert read_selection(env.name, "UTF8_STRING") == ("UTF8_STRING", 8, TEXT.encode())
    deaf.close()
    env._clients.remove(deaf)

    receiver = env.client(Receiver(env.name))
    assert env.inject("drugi ").ok
    assert receiver.received == ["drugi "]
    assert_restored(env.name, FIREFOX_LIKE)  # what the user had before the first dictation


def test_clipboard_manager_does_not_confirm(env: Env) -> None:
    manager = env.client(ClipboardManager(env.name))
    env.client(Receiver(env.name, pastes=False))
    result = env.inject(paste_timeout_ms=300)
    assert manager.fetched.wait(WAIT_S)
    assert result.left_in_clipboard and not result.ok


def test_paste_confirmed_with_clipboard_manager_present(env: Env) -> None:
    env.client(ClipboardManager(env.name))
    receiver = env.client(Receiver(env.name))
    assert env.inject().ok
    assert receiver.received == [TEXT]


def test_paste_by_another_connection_of_the_same_process_confirms(env: Env) -> None:
    paster = env.client(Paster(env.name))
    receiver = env.client(Receiver(env.name, paste_via=paster))
    PIDS.update({base(receiver): 100, base(paster): 100})
    result = env.inject()
    assert result.ok and not result.left_in_clipboard
    assert paster.received == [TEXT]


def test_paste_by_a_child_process_confirms(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Claude Code's CLI in VS Code's terminal: code → node service → zsh → claude."""
    paster = env.client(Paster(env.name))
    receiver = env.client(Receiver(env.name, paste_via=paster))
    PIDS.update({base(receiver): 100, base(paster): 400})
    parents = {400: 300, 300: 200, 200: 100, 100: 50}
    monkeypatch.setattr(clipboard_mod, "parent_pid", parents.get)
    assert env.inject().ok


@pytest.mark.parametrize("pid", [50, 999, None])  # parent (gnome-shell), unrelated, unknown
def test_paste_by_another_process_is_not_confirmed(
    env: Env, monkeypatch: pytest.MonkeyPatch, pid: int | None
) -> None:
    paster = env.client(Paster(env.name))
    receiver = env.client(Receiver(env.name, paste_via=paster))
    PIDS[base(receiver)] = 100
    if pid is not None:
        PIDS[base(paster)] = pid
    monkeypatch.setattr(clipboard_mod, "parent_pid", {100: 50, 999: 1}.get)
    result = env.inject(paste_timeout_ms=300)
    assert paster.pasted.wait(WAIT_S)  # the text did arrive, but from another process
    assert result.left_in_clipboard and not result.ok


def test_client_pid_from_xres(env: Env) -> None:
    import os

    receiver = env.client(Receiver(env.name))
    assert env.owner._xres
    assert REAL_CLIENT_PID(env.owner, receiver.window.id) == os.getpid()
    assert REAL_CLIENT_PID(env.owner, 0x7F00000) is None  # no such client


# --- target window (08 §8.5 step 2) ---------------------------------------------------------


def test_no_active_window(env: Env) -> None:
    result = env.inject()
    assert (result.ok, result.left_in_clipboard, result.no_target) == (False, True, True)
    assert read_selection(env.name, "UTF8_STRING") == ("UTF8_STRING", 8, TEXT.encode())


def test_desktop_window_is_no_target(env: Env) -> None:
    set_desktop_active(env.name)
    assert env.inject().no_target


# --- unrestorable clipboard (08 §8.5 step 3) ------------------------------------------------


@pytest.mark.parametrize(
    "owner_kwargs",
    [
        {"targets": {"image/png": Content("image/png", 8, b"\x89" * (MAX_TARGET_BYTES + 1))}},
        {"targets": {"UTF8_STRING": Content("UTF8_STRING", 8, b"x")}, "incr": True},
    ],
    ids=["too-large", "incr"],
)
def test_unrestorable_clipboard_switches_to_type(env: Env, owner_kwargs: dict[str, Any]) -> None:
    owner = env.client(SelectionOwner(env.name, **owner_kwargs))
    receiver = env.client(Receiver(env.name))
    with pytest.raises(ClipboardUnrestorable, match=r"too large|INCR"):
        env.inject()
    assert receiver.received == []
    assert not owner.lost.is_set()  # the clipboard was left untouched


def test_largest_restorable_target(env: Env) -> None:
    big = {"image/png": Content("image/png", 8, b"\x89" * MAX_TARGET_BYTES)}
    env.client(SelectionOwner(env.name, big))
    env.client(Receiver(env.name))
    assert env.inject().ok
    assert_restored(env.name, big)


def test_unrestorable_without_type_fallback_pastes_and_keeps_text(env: Env) -> None:
    env.client(SelectionOwner(env.name, {"x": Content("x", 8, b"1")}, incr=True))
    receiver = env.client(Receiver(env.name))
    assert env.inject(type_fallback=False).ok
    assert receiver.received == [TEXT]
    assert read_selection(env.name, "UTF8_STRING") == ("UTF8_STRING", 8, TEXT.encode())


# --- waiting for keys (08 §8.5 step 1) ------------------------------------------------------


def run_async(fn: Callable[[], InjectResult]) -> tuple[threading.Thread, list[InjectResult]]:
    out: list[InjectResult] = []
    thread = threading.Thread(target=lambda: out.append(fn()))
    thread.start()
    return thread, out


def test_waits_for_modifier_release(env: Env) -> None:
    receiver = env.client(Receiver(env.name))
    with held_key(env.name, "Shift_L"):
        thread, out = run_async(lambda: env.inject(modifier_wait_ms=5000))
        time.sleep(0.3)
        assert receiver.received == [] and not out
    thread.join(WAIT_S)
    assert out[0].ok
    assert receiver.received == [TEXT]
    assert all(not k.state & X.ShiftMask for k in receiver.keys if k.keysym == "v")


def test_modifier_wait_times_out(env: Env) -> None:
    env.client(Receiver(env.name))
    with held_key(env.name, "Alt_L"):
        started = time.monotonic()
        env.inject(modifier_wait_ms=200, paste_timeout_ms=200)
        assert time.monotonic() - started < 2.0


def test_held_ptt_waits_beyond_modifier_limit_and_cancel_stops_it(env: Env) -> None:
    receiver = env.client(Receiver(env.name))
    with held_key(env.name, "Control_R"):  # default push_to_talk
        thread, out = run_async(lambda: env.inject(modifier_wait_ms=100))
        time.sleep(0.5)  # well past modifier_wait_ms
        assert not out
        assert env.cancel() is False  # no input operation started
        thread.join(WAIT_S)
    assert out[0].cancelled
    time.sleep(0.2)
    assert receiver.received == []
    assert [k.keysym for k in receiver.keys] == ["Control_R"]  # only the held PTT key


# --- cancellation (08 §8.3, 8.5 step 4) -----------------------------------------------------


def test_cancel_while_saving_slow_clipboard(env: Env) -> None:
    owner = env.client(SelectionOwner(env.name, {"UTF8_STRING": Content("UTF8_STRING", 8, b"a")}))
    owner.delay_s = 0.2
    receiver = env.client(Receiver(env.name))
    thread, out = run_async(env.inject)
    time.sleep(0.1)
    env.cancel()
    thread.join(WAIT_S)
    assert out[0].cancelled
    assert receiver.keys == []
    assert not owner.lost.is_set()


def test_cancel_after_takeover_restores_previous_content(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.client(SelectionOwner(env.name, FIREFOX_LIKE))
    receiver = env.client(Receiver(env.name))
    original = ClipboardPasteInjector._shortcut_for

    def cancel_then_shortcut(self: ClipboardPasteInjector, target: Any) -> str:
        env.cancel()  # after take_text, before the shortcut operation
        return original(self, target)

    monkeypatch.setattr(ClipboardPasteInjector, "_shortcut_for", cancel_then_shortcut)
    result = env.inject(restore_clipboard=False)  # rollback ignores restore_clipboard
    assert result.cancelled
    assert receiver.keys == []
    assert_restored(env.name, FIREFOX_LIKE)


def test_cancel_after_takeover_keeps_a_newer_owner(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.client(SelectionOwner(env.name, FIREFOX_LIKE))
    env.client(Receiver(env.name))
    newer = {"UTF8_STRING": Content("UTF8_STRING", 8, b"nowe")}
    original = ClipboardPasteInjector._shortcut_for

    def user_copies_then_cancel(self: ClipboardPasteInjector, target: Any) -> str:
        env.client(SelectionOwner(env.name, newer))
        env.cancel()
        return original(self, target)

    monkeypatch.setattr(ClipboardPasteInjector, "_shortcut_for", user_copies_then_cancel)
    assert env.inject().cancelled
    assert_restored(env.name, newer)


def test_cancel_during_started_paste_completes_it(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    receiver = env.client(Receiver(env.name))
    original = X11Session.send_shortcut
    in_flight: list[bool] = []

    def send_and_cancel(self: X11Session, shortcut: Any) -> None:
        in_flight.append(env.cancel())  # cancel_all() during the XTest sequence
        original(self, shortcut)

    monkeypatch.setattr(X11Session, "send_shortcut", send_and_cancel)
    result = env.inject()
    assert in_flight == [True]
    assert result.ok and not result.cancelled
    assert receiver.received == [TEXT]
    assert [k.keysym for k in receiver.keys] == ["Control_L", "v"]


def test_text_over_limit_uses_type(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clipboard_mod, "MAX_TEXT_BYTES", 10)
    env.client(Receiver(env.name))
    with pytest.raises(ClipboardUnrestorable):
        env.inject("x" * 11)


# --- connection loss --------------------------------------------------------------------


def test_connection_loss_is_reported() -> None:
    with xvfb() as name:
        lost = threading.Event()
        owner = ClipboardOwner(name, on_connection_lost=lost.set)
        owner.start()
    # Xvfb is gone now
    assert lost.wait(WAIT_S)


# --- clipboard-only (task 5.3) ----------------------------------------------------------------


def test_clipboard_only_sends_no_keys_and_replaces_the_clipboard(env: Env) -> None:
    env.client(SelectionOwner(env.name, FIREFOX_LIKE))
    receiver = env.client(Receiver(env.name))
    result = ClipboardOnlyInjector(env.owner).inject(TEXT, cancel=env.token)
    assert result == InjectResult(True, "clipboard-only", len(TEXT), None, True, None)
    time.sleep(0.2)
    assert receiver.received == []
    assert read_selection(env.name, "UTF8_STRING") == ("UTF8_STRING", 8, TEXT.encode())
    assert read_selection(env.name, "text/html") is None  # the old content is gone


def test_paste_after_clipboard_only_restores_the_dictated_text(env: Env) -> None:
    env.client(SelectionOwner(env.name, FIREFOX_LIKE))
    assert ClipboardOnlyInjector(env.owner).inject(TEXT, cancel=env.token).ok
    receiver = env.client(Receiver(env.name))
    assert env.inject("drugi ").ok
    assert receiver.received == ["drugi "]
    assert read_selection(env.name, "UTF8_STRING") == ("UTF8_STRING", 8, TEXT.encode())


def test_cancelled_clipboard_only_keeps_the_clipboard(env: Env) -> None:
    env.client(SelectionOwner(env.name, FIREFOX_LIKE))
    env.cancel()
    result = ClipboardOnlyInjector(env.owner).inject(TEXT, cancel=env.token)
    assert result.cancelled and not result.left_in_clipboard
    assert_restored(env.name, FIREFOX_LIKE)
