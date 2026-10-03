"""XdotoolTypeInjector and AutoInjector against Xvfb with a real xdotool (needs_x11)."""

import dataclasses
import shutil
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

from local_stt.cancellation import CancellationToken
from local_stt.config import Config
from local_stt.inject import xdotool as xdotool_mod
from local_stt.inject.auto import AutoInjector, build_injector
from local_stt.inject.clipboard import ClipboardOwner

from .x11_clients import Content, Receiver, SelectionOwner, read_selection, xvfb

pytestmark = [
    pytest.mark.needs_x11,
    pytest.mark.skipif(shutil.which("xdotool") is None, reason="xdotool is not installed"),
]


class Env:
    def __init__(self, name: str, **injection: Any) -> None:
        self.name = name
        self.owner = ClipboardOwner(name)
        self.owner.start()
        base = Config()
        self.config = dataclasses.replace(
            base, injection=dataclasses.replace(base.injection, **injection)
        )
        self.injector: AutoInjector = build_injector(self.config, self.owner, name)
        self.lock = threading.Lock()
        self.token = CancellationToken(self.lock)

    def cancel(self) -> None:
        with self.lock:
            self.token.cancel_locked()

    def close(self) -> None:
        self.injector._x.close()
        self.owner.stop()


@pytest.fixture
def name() -> Iterator[str]:
    with xvfb() as display_name:
        yield display_name


def typed(receiver: Receiver) -> list[str]:
    time.sleep(0.3)  # xdotool returns before the receiver has read every event
    return [k.keysym for k in receiver.keys]


def test_xterm_is_typed_with_return_for_newline(name: str) -> None:
    env = Env(name)
    receiver = Receiver(name, ("xterm", "XTerm"), pastes=False)
    owner = SelectionOwner(name, {"UTF8_STRING": Content("UTF8_STRING", 8, b"user")})
    try:
        result = env.injector.inject("ab\nv", cancel=env.token)
        assert (result.ok, result.backend, result.chars) == (True, "type", 4)
        assert typed(receiver) == ["a", "b", "Return", "v"]
        assert not owner.lost.is_set()  # the clipboard is not touched
    finally:
        owner.close()
        receiver.close()
        env.close()


def test_unrestorable_clipboard_is_typed(name: str) -> None:
    env = Env(name)
    receiver = Receiver(name)
    owner = SelectionOwner(name, {"x": Content("x", 8, b"1")}, incr=True)
    try:
        result = env.injector.inject("ok", cancel=env.token)
        assert (result.ok, result.backend) == (True, "type")
        assert typed(receiver) == ["o", "k"]
        assert receiver.received == []
        assert not owner.lost.is_set()
    finally:
        owner.close()
        receiver.close()
        env.close()


def test_gedit_gets_clipboard_paste(name: str) -> None:
    env = Env(name)
    receiver = Receiver(name)
    try:
        result = env.injector.inject("tekst", cancel=env.token)
        assert (result.ok, result.backend) == (True, "clipboard")
        assert receiver.received == ["tekst"]
    finally:
        receiver.close()
        env.close()


def test_cancel_between_chunks_with_real_xdotool(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(xdotool_mod, "CHUNK_CHARS", 3)
    monkeypatch.setattr(xdotool_mod.chunks, "__defaults__", (3,))
    env = Env(name, backend="type")
    receiver = Receiver(name, pastes=False)
    typer = env.injector._typer
    assert typer is not None
    original = typer._type
    calls: list[str] = []

    def type_then_cancel(chunk: str, delay_ms: int) -> str | None:
        calls.append(chunk)
        error = original(chunk, delay_ms)
        env.cancel()  # cancel_all() while the first chunk runs
        return error

    monkeypatch.setattr(typer, "_type", type_then_cancel)
    try:
        result = env.injector.inject("abcdefghi", cancel=env.token)
        assert calls == ["abc"]
        assert (result.cancelled, result.chars) == (True, 3)
        assert typed(receiver) == ["a", "b", "c"]
        assert read_selection(name, "UTF8_STRING") is None  # no clipboard fallback
    finally:
        receiver.close()
        env.close()


def test_xdotool_failure_leaves_text_in_clipboard(name: str) -> None:
    env = Env(name, backend="type")
    typer = env.injector._typer
    assert typer is not None
    typer._xdotool = "/nonexistent/xdotool"
    try:
        result = env.injector.inject("ratunek", cancel=env.token)
        assert (result.ok, result.left_in_clipboard) == (False, True)
        assert read_selection(name, "UTF8_STRING") == ("UTF8_STRING", 8, b"ratunek")
    finally:
        env.close()
