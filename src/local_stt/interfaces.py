"""Component interfaces and shared data types (docs/02-architecture.md §2.6).

Components depend on each other only through these types; `app.py` wires implementations.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Literal, Protocol

import numpy as np
from numpy.typing import NDArray

if TYPE_CHECKING:
    from local_stt.cancellation import CancellationToken
    from local_stt.config import Config, HotkeysConfig
    from local_stt.events import Event


class EngineHealth(Enum):
    READY = "READY"
    STARTING = "STARTING"  # server reports that it is loading the model
    # No connection; EngineMonitor maps it to STARTING or DOWN by elapsed time (04 §4.5).
    DOWN = "DOWN"


@dataclass(frozen=True)
class TranscriptSegment:
    text: str  # original spacing; a boundary may fall within a word
    start_s: float
    end_s: float
    no_speech_prob: float | None
    avg_logprob: float | None


@dataclass(frozen=True)
class Transcript:
    text: str  # concatenation of segment texts, without separators added by the server
    segments: list[TranscriptSegment]
    audio_duration_s: float
    processing_s: float  # measured on the client side
    engine: str  # "whisper.cpp"
    model: str  # "small-q8_0"


class SttEngine(Protocol):
    """Speech-to-text engine (docs/06-stt-engine.md §6.9)."""

    name: str

    def health(self) -> EngineHealth: ...

    def transcribe(
        self,
        audio: NDArray[np.float32],  # mono, [-1, 1]
        *,
        sample_rate: int,
        language: str,
        prompt: str | None,
        timeout_s: float,
    ) -> Transcript: ...


# --- audio, jobs and injection (02 §2.6, 08 §8.3) ----------------------------------------

Sound = Literal["start", "stop", "cancel", "error", "language", "language_alt"]  # 10 §10.6
Cut = Literal["release", "max_duration", "silence", "max_length", "flush"]
JobSource = Literal["ptt", "continuous", "history"]  # history: `local-stt last` (5.2)
# Where a job's text goes: the active window, or only IPC subscribers (conversation, 6.3).
JobSink = Literal["inject", "subscriber"]


@dataclass(frozen=True)
class AudioClip:
    """A finished PTT recording (05 §5.3)."""

    samples: NDArray[np.float32]  # mono 16 kHz, start-sound window excluded
    sample_rate: int  # always 16000
    duration_s: float
    started_at: float  # time.monotonic()
    ended_at: float  # release/limit time, not frame-queue drain time


SegmentCut = Literal["silence", "max_length", "flush"]


@dataclass(frozen=True)
class AudioSegment:
    """One utterance from continuous mode (05 §5.5)."""

    samples: NDArray[np.float32]  # mono 16 kHz
    session_id: int
    seq: int  # number within the session, from 1; also increases after reconnect
    ended_at: float  # monotonic: silence detection / limit / flush request
    speech_ms: int  # speech duration without padding
    cut: SegmentCut
    # Silence from the previous emitted segment's last speech frame to this utterance's first,
    # in stream time; 0 for the rest of a max_length split, None for the first after reset().
    pause_before_s: float | None = None
    # Conversation mode (task 6.4): no speech frame came after the speculative cut, so the
    # speculative text is this segment's text; the Controller sends no second engine request.
    reuses_speculative: bool = False


@dataclass(frozen=True)
class Job:
    id: int
    source: JobSource
    audio: NDArray[np.float32]
    ended_at: float  # end of utterance: start of the latency measurement
    generation: int
    session_id: int | None
    seq: int | None
    cut: Cut
    language: str  # active when the recording (PTT) or segment (continuous) was taken
    pause_before_s: float | None = None  # continuous: `AudioSegment.pause_before_s`
    text: str | None = None  # source "history": the text to insert again, no audio (task 5.2)
    sink: JobSink = "inject"
    # Conversation mode (task 6.4): a speculative request; it does not change the session's
    # context (`prev_cut`, prompt tail), because speech may still go on.
    speculative: bool = False

    @property
    def duration_s(self) -> float:
        return len(self.audio) / 16000


@dataclass(frozen=True)
class InjectResult:
    ok: bool
    backend: str  # "clipboard" | "type" | "clipboard-only" | "none" (conversation, task 6.3)
    chars: int
    window_class: str | None  # WM_CLASS of the target window, informational
    left_in_clipboard: bool  # text intentionally left in the clipboard
    error: str | None
    cancelled: bool = False  # cancellation; no emergency clipboard fallback
    no_target: bool = False  # no active window: text placed in the clipboard (08 §8.5 step 2)


@dataclass(frozen=True)
class TextContext:
    """Processing context of one job (08 §8.1)."""

    source: JobSource
    session_id: int | None
    seq: int | None
    cut: Cut
    prev_cut: Cut | None  # cut of the previous segment from this session (continuous)
    prompt_tail: str | None  # session context sent in the prompt; None in PTT


class TextProcessor(Protocol):
    """Transcript → text to inject, or None when nothing remains (08 §8.2)."""

    def process(self, transcript: Transcript, ctx: TextContext) -> str | None: ...


class Injector(Protocol):
    """Enters text into the active window (08 §8.3); never raises for X11 failures."""

    def inject(self, text: str, *, cancel: "CancellationToken") -> InjectResult: ...


@dataclass(frozen=True)
class CancelResult:
    """What `PipelineControl.cancel_all()` discarded (04 §4.4)."""

    drained_job_ids: tuple[int, ...]  # removed from the queue; the worker never reports them
    in_flight_cancelled: bool  # the current job will report JobDiscarded(cancelled)
    injection_in_flight: bool  # an input operation had already started (08 §8.3)

    @property
    def discarded_any(self) -> bool:
        return bool(self.drained_job_ids) or self.in_flight_cancelled


class AudioOpenError(Exception):
    """The microphone stream could not be opened (05 §5.6)."""


# --- effects used by the Controller (04 §4.3) ---------------------------------------------


class AudioCaptureControl(Protocol):
    def open(self, recording_id: int, capture_id: int) -> None:
        """Opens the stream synchronously (30-150 ms); raises AudioOpenError."""

    def close(self) -> None:
        """Stops the stream; returns after the last callback (no frames arrive later)."""


class AudioConsumerControl(Protocol):
    """Commands to the audio-consumer thread, executed in FIFO order (04 §4.3)."""

    def begin_ptt(self, recording_id: int, capture_id: int) -> None: ...

    def mask_start_sound(self, recording_id: int, capture_id: int, until: float) -> None:
        """Discard samples up to monotonic time `until` (end of the start sound + 80 ms)."""

    def finish_ptt(
        self, recording_id: int, capture_id: int, operation_id: int, ended_at: float, cut: Cut
    ) -> None: ...

    def discard(self, recording_id: int, capture_id: int) -> None: ...

    @property
    def overflows(self) -> int:
        """Input overflows since startup (05 §5.6), for `status`."""

    @property
    def continuous_available(self) -> bool:
        """The Segmenter has a VAD model: `vad.enabled` and Silero loaded (05 §5.5)."""

    def reset_continuous(
        self, recording_id: int, capture_id: int, *, speculative_ms: int = 0
    ) -> None:
        """Starts feeding this stream to the Segmenter with clean buffers and VAD state; the
        same `recording_id` (a reconnect) keeps the session's `seq` numbering."""

    def flush(
        self,
        recording_id: int,
        capture_id: int,
        operation_id: int,
        purpose: Literal["stop", "reconnect"],
        at: float,
    ) -> None:
        """Ends the utterance in progress (`SegmentReady(cut="flush")`), then `FlushDone`.
        `at` is the flush request time, the segment's `ended_at`."""


class PipelineControl(Protocol):
    @property
    def generation(self) -> int: ...

    def submit(self, job: Job) -> None: ...

    def reinject(self, job_id: int, text: str) -> None: ...

    def withdraw(self, job_id: int) -> bool: ...

    def cancel_all(self) -> CancelResult: ...

    def pause(self) -> None: ...

    def resume(self) -> None: ...


class Feedback(Protocol):
    """Sounds and notifications (10 §10.6); notifications never contain transcript text."""

    def play(self, sound: Sound) -> float | None:
        """Starts playback without waiting, after any sound still playing; returns the seconds
        until it ends (queueing delay + duration), or None if nothing plays."""

    def notify(self, key: str, title: str, body: str = "", *, informational: bool = False) -> None:
        """A notification with the same `key` replaces the previous one; `informational`
        notifications are shown only with `feedback.notifications = "all"`."""


class DaemonLifecycle(Protocol):
    def shutdown(self, *, x11_alive: bool) -> None:
        """Ungrabs hotkeys (only if `x11_alive`) and closes the IPC socket."""


class ReloadTarget(Protocol):
    """Applies a reloaded config to components (04 §4.6)."""

    def apply_live(self, config: "Config") -> None: ...

    def apply_at_idle(self, config: "Config") -> list["HotkeyProblem"]:
        """Applies audio/vad/hotkeys settings; returns the hotkeys that are not active."""

    def restart_server(self, config: "Config") -> None:
        """Writes whisper-server.env and restarts the unit in a helper thread, which posts
        ServerRestartDone; must not block."""

    def use_server(self, config: "Config") -> None:
        """Points the STT client and EngineMonitor at the restarted server and re-applies
        the live components, so none holds a config older than the controller's. `config` is
        the controller's effective config, not the one the restart began with."""


# --- hotkeys (07 §7.6) ---------------------------------------------------------------------


@dataclass(frozen=True)
class HotkeyProblem:
    """A configured hotkey that is not active; reported as `hotkeys: degraded` (10 §10.4)."""

    hotkey: str  # config key: "push_to_talk" | "continuous_toggle" | "ptt_cancel_key"
    value: str  # e.g. "Shift+Control_R"
    reason: str


class HotkeyBackend(Protocol):
    def start(self, sink: "Callable[[Event], None]") -> None: ...

    def apply(self, config: "HotkeysConfig") -> list[HotkeyProblem]:
        """Replaces all grabs; blocks until applied. Called at startup and on reload at IDLE."""

    def stop(self) -> None:
        """Ungrabs (if the X connection is alive) and ends the listener thread."""
