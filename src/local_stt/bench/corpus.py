"""`local-stt record-corpus`: record benchmark corpus A (docs/13-benchmark.md §13.2).

Layout (one subdirectory per group, numbers follow prompts_pl.txt order):

    DIR/short/001.wav + 001.txt ... DIR/medium/013.wav ... DIR/long_utt/029.wav ...
    DIR/difficult/037.wav ...       DIR/long/001.wav + 001.txt  (continuous recording, --long)

Audio is written to disk only here, at the user's explicit request (docs/12 §12.2).
"""

import queue
import sys
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

from local_stt.audio.capture import SAMPLE_RATE, AudioCapture, AudioFrame
from local_stt.audio.wav import float32_to_wav_bytes

GROUPS = ("short", "medium", "long_utt", "difficult")
LONG_DIR = "long"
# Target spoken durations in seconds; a recording outside the range only produces a warning.
DURATION_RANGES = {
    "short": (1.0, 3.0),
    "medium": (4.0, 10.0),
    "long_utt": (12.0, 25.0),
    "difficult": (5.0, 10.0),
}


@dataclass(frozen=True)
class Prompt:
    number: int
    group: str
    text: str

    @property
    def stem(self) -> str:
        return f"{self.number:03d}"


class Recorder(Protocol):
    def start(self) -> None: ...

    def stop(self) -> NDArray[np.float32]: ...


class CaptureRecorder:
    """Records from the microphone through AudioCapture until stop()."""

    def __init__(self, device: str = "default"):
        self._frames: queue.SimpleQueue[AudioFrame] = queue.SimpleQueue()
        self._capture = AudioCapture(self._frames, device=device)
        self._recording_id = 0

    def start(self) -> None:
        self._recording_id += 1
        while not self._frames.empty():
            self._frames.get_nowait()
        self._capture.open(recording_id=self._recording_id, capture_id=self._recording_id)

    def stop(self) -> NDArray[np.float32]:
        self._capture.close()  # waits for callbacks: every frame of this take is already queued
        chunks = []
        while not self._frames.empty():
            frame = self._frames.get_nowait()
            if frame.recording_id == self._recording_id:
                chunks.append(frame.samples)
        return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)


def parse_prompts(text: str) -> list[Prompt]:
    prompts: list[Prompt] = []
    group: str | None = None
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            group = line[1:-1]
            if group not in GROUPS:
                raise ValueError(f"prompts:{lineno}: unknown group {group!r}")
            continue
        if group is None:
            raise ValueError(f"prompts:{lineno}: sentence before the first [group] header")
        prompts.append(Prompt(len(prompts) + 1, group, line))
    return prompts


def load_prompts() -> list[Prompt]:
    return parse_prompts(_package_text("prompts_pl.txt"))


def load_long_text() -> str:
    return _package_text("long_pl.txt")


def _package_text(name: str) -> str:
    return resources.files("local_stt.bench").joinpath(name).read_text(encoding="utf-8")


class _Quit(Exception):
    pass


class CorpusSession:
    """Interactive recording loop; input and output are injectable for tests."""

    def __init__(
        self,
        out_dir: Path,
        recorder: Recorder,
        *,
        ask: Callable[[str], str] = input,
        say: Callable[[str], None] = print,
    ):
        self.out_dir = out_dir
        self.recorder = recorder
        self._ask = ask
        self._say = say

    def record_prompts(self, prompts: list[Prompt]) -> int:
        """Returns the number of recordings saved in this session."""
        saved = 0
        try:
            for prompt in prompts:
                target = self.out_dir / prompt.group / prompt.stem
                if target.with_suffix(".wav").is_file() and target.with_suffix(".txt").is_file():
                    continue  # already recorded: allows resuming an interrupted session
                header = f"[{prompt.number:03d}/{len(prompts):03d} {prompt.group}]"
                if self._take(header, prompt.text, target, DURATION_RANGES[prompt.group]):
                    saved += 1
        except _Quit:
            self._say("Stopped; run the command again to continue where you left off.")
        self._say(f"Saved {saved} recording(s) in {self.out_dir}")
        return saved

    def record_long(self, text: str) -> bool:
        long_dir = self.out_dir / LONG_DIR
        number = 1
        while (long_dir / f"{number:03d}.wav").exists():
            number += 1
        self._say("Read the whole text aloud at a natural pace, with natural pauses (~5 min).")
        try:
            return self._take("[long]", text, long_dir / f"{number:03d}", None)
        except _Quit:
            return False

    def _take(
        self, header: str, text: str, target: Path, expected: tuple[float, float] | None
    ) -> bool:
        while True:
            self._say(f"\n{header}\n{text}\n")
            choice = self._prompt("Enter = start recording, s = skip, q = quit: ")
            if choice == "s":
                return False
            self.recorder.start()
            try:
                self._prompt("Recording... Enter = stop: ")
            finally:
                audio = self.recorder.stop()
            duration = len(audio) / SAMPLE_RATE
            note = ""
            if expected and not expected[0] <= duration <= expected[1]:
                note = f" (expected {expected[0]:g}-{expected[1]:g} s)"
            choice = self._prompt(f"{duration:.1f} s{note}. Enter = save, r = repeat, s = skip: ")
            if choice == "r":
                continue
            if choice == "s":
                return False
            target.parent.mkdir(parents=True, exist_ok=True)
            target.with_suffix(".wav").write_bytes(float32_to_wav_bytes(audio, SAMPLE_RATE))
            target.with_suffix(".txt").write_text(text.strip() + "\n", encoding="utf-8")
            self._say(f"Saved {target.with_suffix('.wav')}")
            return True

    def _prompt(self, message: str) -> str:
        try:
            answer = self._ask(message).strip().lower()
        except EOFError:
            raise _Quit from None
        if answer == "q":
            raise _Quit
        return answer


def cmd_record_corpus(out_dir: Path, *, long: bool) -> int:
    session = CorpusSession(out_dir, CaptureRecorder())
    try:
        if long:
            session.record_long(load_long_text())
        else:
            session.record_prompts(load_prompts())
    except KeyboardInterrupt:
        print("\nInterrupted; the current take was not saved.", file=sys.stderr)
        return 1
    return 0
