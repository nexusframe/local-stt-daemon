"""KeyRouter and the listener's event path (docs/07 §7.3) without an X server."""

from Xlib import X

from local_stt.events import ContinuousToggle, Event, PttCancelKey, PttPressed, PttReleased
from local_stt.hotkeys.x11 import Binding, KeyRouter, X11GrabHotkeys

CTRL_R, ESC, F9 = 105, 9, 75
RELEVANT = X.ShiftMask | X.ControlMask | X.Mod1Mask | X.Mod4Mask
NUMLOCK = X.Mod2Mask


def router() -> KeyRouter:
    r = KeyRouter()
    r.configure(Binding(CTRL_R, 0), Binding(CTRL_R, X.ShiftMask), ESC, RELEVANT)
    return r


def test_ptt_press_and_release() -> None:
    r = router()
    assert r.press(CTRL_R, 0, 1.0) == PttPressed(1.0)
    assert r.press(CTRL_R, 0, 1.1) is None  # repeated press while held
    assert r.release(CTRL_R, 2.0) == PttReleased(2.0)  # state is not compared on release
    assert r.release(CTRL_R, 2.1) is None


def test_lock_modifiers_are_ignored() -> None:
    r = router()
    assert r.press(CTRL_R, X.LockMask | NUMLOCK, 1.0) == PttPressed(1.0)


def test_toggle_with_shift_and_its_release_ignored() -> None:
    r = router()
    assert r.press(CTRL_R, X.ShiftMask, 1.0) == ContinuousToggle()
    assert r.release(CTRL_R, 1.5) is None
    assert not r.ptt_down


def test_cancel_key_only_while_ptt_held() -> None:
    r = router()
    assert r.press(ESC, 0, 1.0) is None
    r.press(CTRL_R, 0, 1.0)
    assert r.press(ESC, 0, 1.2) == PttCancelKey()
    assert r.press(F9, 0, 1.3) is None  # other keys during the active grab
    assert r.press(CTRL_R, X.ShiftMask, 1.4) is None  # no toggle while PTT is held


def test_other_modifier_combination_is_not_ptt() -> None:
    r = router()
    assert r.press(CTRL_R, X.Mod1Mask, 1.0) is None


def test_lost_release() -> None:
    r = router()
    assert r.lost_release(1.0) is None
    r.press(CTRL_R, 0, 1.0)
    assert r.lost_release(2.0) == PttReleased(2.0)
    assert r.release(CTRL_R, 2.1) is None  # the late real release is ignored


def test_reconfigure_resets_held_ptt_only_when_its_key_changes() -> None:
    r = router()
    r.press(CTRL_R, 0, 1.0)
    r.configure(Binding(CTRL_R, 0), None, ESC, RELEVANT)
    assert r.ptt_down
    r.configure(Binding(F9, 0), None, ESC, RELEVANT)
    assert not r.ptt_down
    r.press(F9, 0, 2.0)
    r.configure(None, None, 0, 0)
    assert not r.ptt_down


class _Ev:
    def __init__(self, type: int, detail: int = 0, state: int = 0, time: int = 0) -> None:
        self.type, self.detail, self.state, self.time, self.request = type, detail, state, time, 1


class _QueueDisplay:
    """Xlib's event queue: refresh_keyboard_mapping reads later events while waiting."""

    def __init__(self, events: list[_Ev], during_refresh: list[_Ev]) -> None:
        self.queue, self.during_refresh = events, during_refresh

    def pending_events(self) -> int:
        return len(self.queue)

    def next_event(self) -> _Ev:
        return self.queue.pop(0)

    def refresh_keyboard_mapping(self, ev: _Ev) -> None:
        self.queue += self.during_refresh
        self.during_refresh = []


def listener(d: _QueueDisplay) -> tuple[X11GrabHotkeys, list[Event]]:
    hk = object.__new__(X11GrabHotkeys)  # no X connection: only the event path is used
    events: list[Event] = []
    hk._d, hk._router, hk._sink = d, router(), events.append
    return hk, events


def test_events_read_during_a_round_trip_are_processed() -> None:
    d = _QueueDisplay(
        [_Ev(X.MappingNotify)], [_Ev(X.KeyPress, CTRL_R, 0, 5), _Ev(X.KeyRelease, CTRL_R, 4, 6)]
    )
    hk, events = listener(d)
    hk._drain_events()
    assert [type(e) for e in events] == [PttPressed, PttReleased]


def test_autorepeat_pair_is_dropped() -> None:
    d = _QueueDisplay(
        [
            _Ev(X.KeyPress, F9, 0, 1),
            _Ev(X.KeyRelease, F9, 0, 9),
            _Ev(X.KeyPress, F9, 0, 9),  # same keycode and time: auto-repeat
            _Ev(X.KeyRelease, F9, 0, 12),
        ],
        [],
    )
    hk, events = listener(d)
    hk._router.configure(Binding(F9, 0), None, ESC, RELEVANT)
    hk._drain_events()
    assert [type(e) for e in events] == [PttPressed, PttReleased]
