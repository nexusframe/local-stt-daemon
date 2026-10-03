"""X11GrabHotkeys against a private Xvfb (needs_x11; docs/14-tests.md §14.3 item 2).

Xvfb has no Mutter, so GNOME conflicts are not covered here (14.4 checklist).
"""

import contextlib
import dataclasses
import logging
import queue
from collections.abc import Iterator
from typing import Any

import pytest
from Xlib import XK, X, display, error
from Xlib.ext import xtest

from local_stt.config import HotkeysConfig
from local_stt.events import (
    ContinuousToggle,
    Event,
    PttCancelKey,
    PttPressed,
    PttReleased,
    X11ConnectionLost,
)
from local_stt.hotkeys.x11 import X11GrabHotkeys
from local_stt.interfaces import HotkeyProblem

from .x11_clients import WAIT_S, _Client, xvfb

pytestmark = pytest.mark.needs_x11

QUIET_S = 0.4  # longer than one lost-release check (0.25 s)


class Keyboard(_Client):
    """Injects keys through XTest; its event thread is not started (server_time reads events)."""

    def code(self, keysym: str) -> int:
        code: int = self.d.keysym_to_keycode(XK.string_to_keysym(keysym))
        assert code, keysym
        return code

    def send(self, *steps: tuple[int, str]) -> None:
        for kind, keysym in steps:
            xtest.fake_input(self.d, kind, self.code(keysym))
        self.d.sync()

    def close(self) -> None:
        self.d.close()

    def tap(self, keysym: str) -> None:
        self.send((X.KeyPress, keysym), (X.KeyRelease, keysym))


class Env:
    def __init__(self, name: str) -> None:
        self.name = name
        self.events: queue.Queue[Event] = queue.Queue()
        self.hotkeys = X11GrabHotkeys(name)
        self.hotkeys.start(self.events.put)
        self.kb = Keyboard(name)

    def apply(self, **fields: Any) -> list[HotkeyProblem]:
        return self.hotkeys.apply(dataclasses.replace(HotkeysConfig(), **fields))

    def next(self) -> Event:
        return self.events.get(timeout=WAIT_S)

    def drain(self) -> None:
        with contextlib.suppress(queue.Empty):
            while True:
                self.events.get(timeout=QUIET_S)

    def quiet(self) -> None:
        with pytest.raises(queue.Empty):
            event = self.events.get(timeout=QUIET_S)
            pytest.fail(f"unexpected {event}")


@pytest.fixture
def env() -> Iterator[Env]:
    with xvfb() as name:
        e = Env(name)
        assert e.apply() == []
        yield e
        e.hotkeys.stop()
        e.kb.close()


def test_ptt_press_release(env: Env) -> None:
    env.kb.send((X.KeyPress, "Control_R"))
    assert isinstance(env.next(), PttPressed)
    env.kb.send((X.KeyRelease, "Control_R"))  # arrives with ControlMask in its state
    assert isinstance(env.next(), PttReleased)
    env.quiet()


def test_toggle_with_shift_released_first(env: Env) -> None:
    env.kb.send(
        (X.KeyPress, "Shift_R"),
        (X.KeyPress, "Control_R"),
        (X.KeyRelease, "Shift_R"),
        (X.KeyRelease, "Control_R"),
    )
    assert env.next() == ContinuousToggle()
    env.quiet()


def test_cancel_key_only_while_ptt_held(env: Env) -> None:
    env.kb.tap("Escape")
    env.quiet()
    env.kb.send((X.KeyPress, "Control_R"))
    assert isinstance(env.next(), PttPressed)
    env.kb.tap("Escape")
    assert env.next() == PttCancelKey()
    env.kb.send((X.KeyRelease, "Control_R"))
    assert isinstance(env.next(), PttReleased)


def test_left_ctrl_does_not_trigger(env: Env) -> None:
    env.kb.tap("Control_L")
    env.kb.send((X.KeyPress, "Shift_L"), (X.KeyPress, "Control_L"))
    env.kb.send((X.KeyRelease, "Control_L"), (X.KeyRelease, "Shift_L"))
    env.quiet()


def test_works_with_numlock_and_capslock_on(env: Env) -> None:
    env.kb.tap("Num_Lock")
    env.kb.tap("Caps_Lock")
    mask = env.kb.root.query_pointer().mask
    assert mask & X.Mod2Mask and mask & X.LockMask
    env.kb.tap("Control_R")
    assert isinstance(env.next(), PttPressed)
    assert isinstance(env.next(), PttReleased)
    env.kb.send((X.KeyPress, "Shift_R"), (X.KeyPress, "Control_R"))
    env.kb.send((X.KeyRelease, "Control_R"), (X.KeyRelease, "Shift_R"))
    assert env.next() == ContinuousToggle()


def test_autorepeat_is_not_a_release(env: Env) -> None:
    assert env.apply(push_to_talk="F9", continuous_toggle="Shift+F9") == []
    # XTest events share a timestamp only within one server millisecond (6 of 1000
    # batches crossed it on Xvfb); retry until the batch provably fits in one.
    for _ in range(10):
        before = env.kb.server_time()
        for kind in (X.KeyPress, X.KeyRelease, X.KeyPress):
            xtest.fake_input(env.kb.d, kind, env.kb.code("F9"))
        if env.kb.server_time() == before:  # sent right behind the batch, no round trip
            break
        env.kb.send((X.KeyRelease, "F9"))
        env.drain()
    else:
        pytest.fail("no single-millisecond batch")
    assert isinstance(env.next(), PttPressed)
    env.quiet()  # the release/press pair was auto-repeat; F9 is still held
    env.kb.send((X.KeyRelease, "F9"))
    assert isinstance(env.next(), PttReleased)


def test_lost_release_detected_by_keymap(
    env: Env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    env.kb.send((X.KeyPress, "Control_R"))
    assert isinstance(env.next(), PttPressed)
    router = env.hotkeys._router
    monkeypatch.setattr(router, "release", lambda keycode, at: None)  # e.g. VT switch
    with caplog.at_level(logging.WARNING, "local_stt.hotkeys"):
        env.kb.send((X.KeyRelease, "Control_R"))
        assert isinstance(env.next(), PttReleased)
    assert "push-to-talk release lost" in caplog.text


def test_grab_taken_by_another_client(env: Env) -> None:
    other = display.Display(env.name)
    try:
        env.apply(enabled=False)
        code = env.kb.code("Control_R")
        other.screen().root.grab_key(code, 0, False, X.GrabModeAsync, X.GrabModeAsync)
        other.sync()
        problems = env.apply()
        assert problems == [
            HotkeyProblem("push_to_talk", "Control_R", "already grabbed by another client")
        ]
        env.kb.send((X.KeyPress, "Shift_R"), (X.KeyPress, "Control_R"))
        env.kb.send((X.KeyRelease, "Control_R"), (X.KeyRelease, "Shift_R"))
        assert env.next() == ContinuousToggle()  # the other shortcut still works
    finally:
        other.close()


def test_reload_replaces_grabs(env: Env) -> None:
    assert env.apply(push_to_talk="F9", continuous_toggle="Ctrl+F9") == []
    env.kb.tap("Control_R")
    env.quiet()
    env.kb.tap("F9")
    assert isinstance(env.next(), PttPressed)
    assert isinstance(env.next(), PttReleased)


def test_disabled_grabs_nothing(env: Env) -> None:
    assert env.apply(enabled=False) == []
    env.kb.tap("Control_R")
    env.quiet()


def test_missing_keycode_reported() -> None:
    with xvfb() as name:
        hotkeys = X11GrabHotkeys(name)
        hotkeys.start(lambda _: None)
        try:
            problems = hotkeys.apply(HotkeysConfig(push_to_talk="F35"))
        finally:
            hotkeys.stop()
    assert [(p.hotkey, p.reason) for p in problems] == [
        ("push_to_talk", "F35 has no keycode in the current keyboard map")
    ]


def test_stop_ungrabs() -> None:
    with xvfb() as name:
        hotkeys = X11GrabHotkeys(name)
        hotkeys.start(lambda _: None)
        assert hotkeys.apply(HotkeysConfig()) == []
        hotkeys.stop()
        other = display.Display(name)
        code = other.keysym_to_keycode(XK.string_to_keysym("Control_R"))
        catch = error.CatchError(error.BadAccess)
        other.screen().root.grab_key(
            code, 0, False, X.GrabModeAsync, X.GrabModeAsync, onerror=catch
        )
        other.sync()
        assert catch.get_error() is None
        other.close()
        assert hotkeys.apply(HotkeysConfig()) == []  # after stop: ignored, no hang


def test_connection_lost() -> None:
    events: queue.Queue[Event] = queue.Queue()
    with contextlib.ExitStack() as stack:
        name = stack.enter_context(xvfb())
        hotkeys = X11GrabHotkeys(name)
        hotkeys.start(events.put)
        assert hotkeys.apply(HotkeysConfig()) == []
    # Xvfb terminated with the context
    assert events.get(timeout=WAIT_S) == X11ConnectionLost()
    hotkeys.stop()
    assert hotkeys.apply(HotkeysConfig()) == []
