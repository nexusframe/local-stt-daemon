"""Sounds and notifications (docs/10-cli-ipc-status.md §10.6) with mocked subprocesses."""

import dataclasses
import subprocess
import threading
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from local_stt.audio.wav import wav_bytes_to_float32
from local_stt.config import Config, FeedbackConfig
from local_stt.feedback import (
    PATTERNS,
    SOUND_GAP_S,
    SOUND_RATE,
    DesktopFeedback,
    render,
    sounds_dir,
)


class Proc:
    def __init__(self, args: list[str], **kwargs: Any) -> None:
        self.args = args
        self.done = False

    def poll(self) -> int | None:
        return 0 if self.done else None


class Tools:
    """which/popen/run fakes; notify-send returns increasing ids."""

    def __init__(self, available: tuple[str, ...] = ("pw-play", "paplay", "notify-send")) -> None:
        self.available = available
        self.played: list[Proc] = []
        self.notified: list[list[str]] = []
        self.next_id = 7
        self.sent = threading.Event()
        self.result: Any = None  # override: exception or CompletedProcess

    def which(self, name: str) -> str | None:
        return f"/usr/bin/{name}" if name in self.available else None

    def popen(self, args: list[str], **kwargs: Any) -> Proc:
        proc = Proc(args)
        self.played.append(proc)
        return proc

    def run(self, args: list[str], **kwargs: Any) -> Any:
        self.notified.append(args)
        self.sent.set()
        if isinstance(self.result, BaseException):
            raise self.result
        if self.result is not None:
            return self.result
        self.next_id += 1
        return subprocess.CompletedProcess(args, 0, f"{self.next_id - 1}\n", "")


class Clock:
    """Manual time for sound sequencing; `later` records deferred starts."""

    def __init__(self) -> None:
        self.now = 0.0
        self.scheduled: list[tuple[float, Any]] = []

    def __call__(self) -> float:
        return self.now

    def later(self, delay_s: float, start: Any) -> None:
        self.scheduled.append((delay_s, start))


def make(
    tmp_path: Path, tools: Tools, clock: Clock | None = None, **config: Any
) -> DesktopFeedback:
    clock = clock or Clock()
    fb = DesktopFeedback(
        FeedbackConfig(**config),
        tmp_path / "sounds",
        which=tools.which,
        popen=tools.popen,
        run=tools.run,
        clock=clock,
        later=clock.later,
    )
    fb.start()
    return fb


def flush(fb: DesktopFeedback) -> None:
    fb.stop()  # processes the queue, then ends the thread


@pytest.mark.parametrize(
    ("sound", "seconds"),
    [
        ("start", 0.13),
        ("stop", 0.13),
        ("cancel", 0.12),
        ("error", 0.25),
        ("language", 0.08),
        ("language_alt", 0.18),
    ],
)
def test_generated_wav_files(tmp_path: Path, sound: str, seconds: float) -> None:
    fb = make(tmp_path, Tools(), sound_volume=0.4)
    samples, rate = wav_bytes_to_float32((tmp_path / "sounds" / f"{sound}.wav").read_bytes())
    assert rate == SOUND_RATE
    assert len(samples) == round(seconds * SOUND_RATE)
    assert abs(float(np.mean(samples))) < 1e-3  # no DC offset
    assert float(np.max(np.abs(samples))) == pytest.approx(0.4, abs=0.01)
    assert abs(samples[0]) < 1e-3 and abs(samples[-1]) < 0.01  # faded in and out
    flush(fb)


def test_patterns_and_error_pauses() -> None:
    assert [f for f, _ in PATTERNS["start"]] == [660.0, 880.0]
    assert [f for f, _ in PATTERNS["stop"]] == [880.0, 660.0]
    error = render("error", 0.4)
    pause = error[int(0.05 * SOUND_RATE) : int(0.10 * SOUND_RATE)]
    assert not pause.any()


def test_play_starts_player_without_waiting(tmp_path: Path) -> None:
    tools = Tools()
    fb = make(tmp_path, tools)
    assert fb.play("start") == pytest.approx(0.13)
    assert tools.played[0].args == ["/usr/bin/pw-play", str(tmp_path / "sounds" / "start.wav")]
    flush(fb)


def test_finished_players_are_reaped(tmp_path: Path) -> None:
    tools, clock = Tools(), Clock()
    fb = make(tmp_path, tools, clock)
    fb.play("start")
    clock.now = 1.0
    fb.play("stop")
    tools.played[0].done = True
    clock.now = 2.0
    fb.play("cancel")
    assert fb._playing == tools.played[1:]
    flush(fb)


def test_overlapping_sound_waits_for_the_previous_one(tmp_path: Path) -> None:
    """PTT without speech: `cancel` comes ~70 ms after `stop` and would be masked by it."""
    tools, clock = Tools(), Clock()
    fb = make(tmp_path, tools, clock)
    assert fb.play("stop") == pytest.approx(0.13)
    clock.now = 0.07
    # starts after the rest of `stop` (0.06 s) and the gap, then plays for 0.12 s
    assert fb.play("cancel") == pytest.approx(0.06 + SOUND_GAP_S + 0.12)
    assert [p.args[1] for p in tools.played] == [str(tmp_path / "sounds" / "stop.wav")]
    [(delay, start)] = clock.scheduled
    assert delay == pytest.approx(0.06 + SOUND_GAP_S)
    start()
    assert tools.played[-1].args[1] == str(tmp_path / "sounds" / "cancel.wav")
    flush(fb)


def test_sounds_queue_in_order(tmp_path: Path) -> None:
    """`stop` + `error` at the same moment (backlog, microphone lost) play one after the other."""
    tools, clock = Tools(), Clock()
    fb = make(tmp_path, tools, clock)
    fb.play("stop")
    fb.play("error")
    fb.play("cancel")
    delays = [d for d, _ in clock.scheduled]
    assert delays == pytest.approx([0.13 + SOUND_GAP_S, 0.13 + 0.25 + 2 * SOUND_GAP_S])
    clock.now = 5.0  # long after: plays at once again
    fb.play("start")
    assert len(clock.scheduled) == 2 and len(tools.played) == 2
    flush(fb)


def test_paplay_fallback(tmp_path: Path) -> None:
    tools = Tools(("paplay",))
    fb = make(tmp_path, tools)
    fb.play("cancel")
    assert tools.played[0].args[0] == "/usr/bin/paplay"
    flush(fb)


@pytest.mark.parametrize("available", [(), ("notify-send",)])
def test_no_player_means_no_masking_window(tmp_path: Path, available: tuple[str, ...]) -> None:
    fb = make(tmp_path, Tools(available))
    assert fb.play("start") is None
    flush(fb)


def test_sounds_disabled(tmp_path: Path) -> None:
    tools = Tools()
    fb = make(tmp_path, tools, sounds=False)
    assert fb.play("start") is None
    assert tools.played == []
    flush(fb)


def test_player_failure(tmp_path: Path) -> None:
    tools = Tools()

    def broken(args: list[str], **kwargs: Any) -> Proc:
        raise PermissionError("denied")

    tools.popen = broken  # type: ignore[method-assign]
    fb = make(tmp_path, tools)
    assert fb.play("error") is None
    flush(fb)


def test_unwritable_sound_directory(tmp_path: Path) -> None:
    (tmp_path / "sounds").write_text("a file, not a directory")
    fb = make(tmp_path, Tools())
    assert fb.play("start") is None
    flush(fb)


def test_volume_change_rewrites_sounds(tmp_path: Path) -> None:
    fb = make(tmp_path, Tools(), sound_volume=0.4)
    config = Config()
    fb.update_config(dataclasses.replace(config, feedback=FeedbackConfig(sound_volume=0.1)))
    samples, _ = wav_bytes_to_float32((tmp_path / "sounds" / "cancel.wav").read_bytes())
    assert float(np.max(np.abs(samples))) == pytest.approx(0.1, abs=0.01)
    fb.update_config(
        dataclasses.replace(config, feedback=FeedbackConfig(sound_volume=0.1, sounds=False))
    )
    assert fb.play("cancel") is None
    flush(fb)


def test_notifications_replace_per_key(tmp_path: Path) -> None:
    tools = Tools()
    fb = make(tmp_path, tools)
    fb.notify("engine", "STT engine unavailable", "Run: local-stt doctor")
    fb.notify("engine", "STT engine unavailable", "still")
    fb.notify("clipboard", "-starts with a dash")
    flush(fb)
    base = ["/usr/bin/notify-send", "-a", "local-stt", "-i", "audio-input-microphone", "-p"]
    assert tools.notified == [
        [*base, "--", "STT engine unavailable", "Run: local-stt doctor"],
        [*base, "-r", "7", "--", "STT engine unavailable", "still"],
        [*base, "--", "-starts with a dash", ""],
    ]


@pytest.mark.parametrize(
    ("level", "sent"),
    [("none", []), ("errors", ["error"]), ("all", ["error", "info"])],
)
def test_notification_levels(tmp_path: Path, level: str, sent: list[str]) -> None:
    tools = Tools()
    fb = make(tmp_path, tools, notifications=level)
    fb.notify("a", "error")
    fb.notify("b", "info", informational=True)
    flush(fb)
    assert [args[-2] for args in tools.notified] == sent
    if "info" in sent:
        assert "-e" in tools.notified[1]  # informational = transient
        assert "-e" not in tools.notified[0]


@pytest.mark.parametrize(
    "result",
    [
        FileNotFoundError(),
        subprocess.TimeoutExpired("notify-send", 5),
        subprocess.CompletedProcess([], 1, "", "no session bus"),
    ],
)
def test_notify_send_failure_is_not_fatal(tmp_path: Path, result: Any) -> None:
    tools = Tools()
    tools.result = result
    fb = make(tmp_path, tools)
    fb.notify("engine", "first")
    fb.notify("engine", "second")
    flush(fb)
    assert all("-r" not in args for args in tools.notified)  # no id remembered


def test_without_notify_send(tmp_path: Path) -> None:
    tools = Tools(("pw-play",))
    fb = make(tmp_path, tools)
    fb.notify("engine", "x")
    flush(fb)
    assert tools.notified == []


def test_sounds_dir() -> None:
    assert sounds_dir({"XDG_RUNTIME_DIR": "/run/user/1000"}) == Path(
        "/run/user/1000/local-stt/sounds"
    )
