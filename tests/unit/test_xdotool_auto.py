import dataclasses
import subprocess
import threading
from concurrent.futures import Future
from typing import Any, cast

import pytest

from local_stt.cancellation import CancellationToken
from local_stt.config import Config
from local_stt.inject.auto import AutoInjector
from local_stt.inject.clipboard import ClipboardUnrestorable
from local_stt.inject.x11util import TargetWindow, X11InjectError
from local_stt.inject.xdotool import XdotoolTypeInjector, chunk_timeout, chunks
from local_stt.interfaces import InjectResult


class FakeSession:
    def __init__(self, wm_class: str | None = "Gedit") -> None:
        self.target = TargetWindow(1, wm_class.lower(), wm_class) if wm_class else None
        self.released = True
        self.error: Exception | None = None

    def keycodes(self, name: str) -> set[int]:
        return {105}

    def wait_for_keys_released(self, cancel: CancellationToken, **kwargs: Any) -> bool:
        if self.error is not None:
            raise self.error
        return self.released and not cancel.cancelled

    def active_window(self) -> TargetWindow | None:
        return self.target


class FakeOwner:
    def __init__(self) -> None:
        self.taken: list[str] = []

    def take_text(self, text: str) -> "Future[bool]":
        self.taken.append(text)
        future: Future[bool] = Future()
        future.set_result(True)
        return future


class FakeRun:
    """Records xdotool calls; `on_call(n)` may cancel or fail the n-th call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result: Any = 0  # exit code, or an exception to raise
        self.on_call: Any = None

    def __call__(self, argv: list[str], **kwargs: Any) -> "subprocess.CompletedProcess[str]":
        self.calls.append({"argv": argv, **kwargs})
        if self.on_call is not None:
            self.on_call(len(self.calls))
        if isinstance(self.result, BaseException):
            raise self.result
        return subprocess.CompletedProcess(argv, self.result, "", "BadWindow")


class World:
    def __init__(self, **injection: Any) -> None:
        base = Config()
        self.config = dataclasses.replace(
            base, injection=dataclasses.replace(base.injection, **injection)
        )
        self.session = FakeSession()
        self.owner = FakeOwner()
        self.run = FakeRun()
        self.lock = threading.Lock()
        self.token = CancellationToken(self.lock)
        self.typer = XdotoolTypeInjector(
            cast(Any, self.session),
            cast(Any, self.owner),
            self.config,
            display_name=":99",
            run=self.run,
        )

    def cancel(self) -> None:
        with self.lock:
            self.token.cancel_locked()

    def inject(self, text: str) -> InjectResult:
        return self.typer.inject(text, cancel=self.token)


def test_chunks_and_timeout() -> None:
    assert [len(c) for c in chunks("x" * 450)] == [200, 200, 50]
    assert chunks("") == []
    assert chunk_timeout("x" * 10, 12) == 5.0
    assert chunk_timeout("x" * 200, 12) == pytest.approx(10.0)
    assert chunk_timeout("x" * 200, 50) == pytest.approx(20.0)  # slower typing, longer limit


def test_types_text_in_chunks_with_return_for_newline() -> None:
    w = World(type_delay_ms=15)
    text = "a" * 250 + "\nkoniec"
    result = w.inject(text)
    assert result == InjectResult(True, "type", len(text), "Gedit", False, None)
    argvs = [c["argv"] for c in w.run.calls]
    assert argvs[0][:5] == ["xdotool", "type", "--delay", "15", "--"]
    assert "".join(a[5] for a in argvs) == "a" * 250 + "\rkoniec"
    assert [len(a[5]) for a in argvs] == [200, 57]
    assert w.run.calls[0]["env"]["DISPLAY"] == ":99"
    assert w.owner.taken == []


def test_cancel_between_chunks_stops_without_clipboard_fallback() -> None:
    w = World()
    w.run.on_call = lambda n: w.cancel() if n == 1 else None
    result = w.inject("b" * 450)
    assert len(w.run.calls) == 1  # the next subprocess never starts
    assert (result.cancelled, result.ok, result.chars) == (True, False, 200)
    assert w.owner.taken == []


def test_cancel_while_waiting_for_keys() -> None:
    w = World()
    w.session.released = False
    result = w.inject("tekst")
    assert result.cancelled and w.run.calls == []


@pytest.mark.parametrize(
    ("outcome", "error"),
    [
        (1, "xdotool exited with 1: BadWindow"),
        (FileNotFoundError(), "xdotool not found"),
        (subprocess.TimeoutExpired("xdotool", 5), "xdotool timed out"),
    ],
)
def test_failure_leaves_untyped_text_in_clipboard(outcome: Any, error: str) -> None:
    w = World()
    w.run.on_call = lambda n: setattr(w.run, "result", outcome) if n == 2 else None
    text = "c" * 200 + "reszta"
    result = w.inject(text)
    assert (result.ok, result.chars, result.left_in_clipboard, result.error) == (
        False,
        200,
        True,
        error,
    )
    assert w.owner.taken == ["reszta"]


def test_x11_error_before_typing() -> None:
    w = World()
    w.session.error = X11InjectError("connection lost")
    result = w.inject("tekst")
    assert (result.ok, result.left_in_clipboard, result.error) == (False, True, "connection lost")
    assert w.owner.taken == ["tekst"]


def test_no_active_window_still_types() -> None:
    w = World()
    w.session.target = None
    result = w.inject("x")
    assert result.ok and result.window_class is None


# --- AutoInjector -----------------------------------------------------------------------


class FakeInjector:
    def __init__(self, backend: str, raises: Exception | None = None) -> None:
        self.backend = backend
        self.raises = raises
        self.texts: list[str] = []
        self.type_fallback = False
        self.configs: list[Config] = []

    def update_config(self, config: Config) -> None:
        self.configs.append(config)

    def inject(self, text: str, *, cancel: CancellationToken) -> InjectResult:
        self.texts.append(text)
        if self.raises is not None:
            raise self.raises
        return InjectResult(True, self.backend, len(text), "x", False, None)


def auto(
    wm_class: str | None = "Gedit",
    *,
    backend: str = "auto",
    typer: bool = True,
    clipboard_raises: Exception | None = None,
) -> tuple[AutoInjector, FakeInjector, FakeInjector]:
    base = Config()
    config = dataclasses.replace(
        base, injection=dataclasses.replace(base.injection, backend=backend)
    )
    clip = FakeInjector("clipboard", clipboard_raises)
    typ = FakeInjector("type")
    injector = AutoInjector(
        cast(Any, FakeSession(wm_class)), cast(Any, clip), cast(Any, typ) if typer else None, config
    )
    return injector, clip, typ


def token() -> CancellationToken:
    return CancellationToken(threading.Lock())


@pytest.mark.parametrize(
    ("wm_class", "backend", "expected"),
    [
        ("Gedit", "auto", "clipboard"),
        ("XTerm", "auto", "type"),  # instance "xterm" matches type_window_classes
        ("URxvt", "auto", "type"),
        (None, "auto", "clipboard"),  # no window: the clipboard path handles it
        ("XTerm", "clipboard", "clipboard"),
        ("Gedit", "type", "type"),
    ],
)
def test_backend_selection(wm_class: str | None, backend: str, expected: str) -> None:
    injector, _, _ = auto(wm_class, backend=backend)
    assert injector.inject("x", cancel=token()).backend == expected


def test_unrestorable_clipboard_switches_to_type() -> None:
    injector, clip, typ = auto(clipboard_raises=ClipboardUnrestorable("INCR"))
    assert clip.type_fallback is True
    assert injector.inject("tekst", cancel=token()).backend == "type"
    assert (clip.texts, typ.texts) == (["tekst"], ["tekst"])


def test_without_xdotool_only_clipboard_without_fallback(caplog: pytest.LogCaptureFixture) -> None:
    injector, clip, _ = auto("XTerm", backend="type", typer=False)
    assert clip.type_fallback is False
    assert injector.inject("x", cancel=token()).backend == "clipboard"
    assert "xdotool is missing" in caplog.text


def test_explicit_clipboard_backend_has_no_type_fallback() -> None:
    _, clip, _ = auto(backend="clipboard")
    assert clip.type_fallback is False


def test_update_config_reaches_both_injectors() -> None:
    injector, clip, typ = auto()
    new = Config()
    injector.update_config(new)
    assert clip.configs[-1] is new and typ.configs[-1] is new
