"""Events delivered to the Controller's queue (docs/04-state-machine.md §4.2).

Continuous-mode events arrive in v0.2: the audio-consumer ones (`SpeechStarted`,
`SpeechEnded`, `SegmentReady`, `FlushDone`) in task 2.3a, the Controller's timers
(`CaptureOpenDue`, `ReconnectTick`) in task 2.3b.
"""

from collections.abc import Mapping
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, Literal

from local_stt.interfaces import (
    AudioClip,
    AudioSegment,
    Cut,
    EngineHealth,
    InjectResult,
    JobSource,
)

# IPC response future; an event without one comes from a hotkey or a signal.
Reply = Future[dict[str, Any]] | None


@dataclass(frozen=True)
class PttPressed:
    at: float  # time.monotonic() at the source
    reply: Reply = field(default=None, compare=False)


@dataclass(frozen=True)
class PttReleased:
    at: float
    reply: Reply = field(default=None, compare=False)


@dataclass(frozen=True)
class PttCancelKey:
    pass


@dataclass(frozen=True)
class ContinuousToggle:
    reply: Reply = field(default=None, compare=False)


@dataclass(frozen=True)
class LanguageSwitch:
    """Hotkey or IPC `language` (task 3.7): `target` None moves to the next of stt.languages,
    after the last back to the first."""

    target: str | None = None
    reply: Reply = field(default=None, compare=False)


@dataclass(frozen=True)
class CancelRequested:
    reply: Reply = field(default=None, compare=False)


@dataclass(frozen=True)
class RecordingStarted:
    recording_id: int
    capture_id: int


@dataclass(frozen=True)
class RecordingLimitReached:
    recording_id: int
    capture_id: int
    ended_at: float


@dataclass(frozen=True)
class RecordingFinished:
    recording_id: int
    capture_id: int
    operation_id: int
    clip: AudioClip = field(compare=False)
    cut: Cut


@dataclass(frozen=True)
class SpeechStarted:
    recording_id: int
    capture_id: int


@dataclass(frozen=True)
class SpeechEnded:
    recording_id: int
    capture_id: int


@dataclass(frozen=True)
class SegmentReady:
    recording_id: int
    capture_id: int
    segment: AudioSegment = field(compare=False)
    operation_id: int | None = None  # set when the segment comes from a flush


FlushPurpose = Literal["stop", "reconnect"]


@dataclass(frozen=True)
class FlushDone:
    recording_id: int
    capture_id: int
    operation_id: int
    purpose: FlushPurpose


@dataclass(frozen=True)
class MicrophoneSilent:
    """Digital silence for 5 s in continuous mode: the microphone is probably muted (05 §5.6)."""

    recording_id: int
    capture_id: int


@dataclass(frozen=True)
class CaptureOpenDue:
    """Controller timer: open the microphone 150 ms after continuous mode starts (10 §10.6)."""

    recording_id: int


@dataclass(frozen=True)
class ReconnectTick:
    """Controller timer: the next microphone reopen attempt (04 §4.3)."""

    recording_id: int
    operation_id: int
    attempt: int


@dataclass(frozen=True)
class AudioError:
    recording_id: int
    capture_id: int
    kind: Literal["open_failed", "device_lost"]
    description: str


@dataclass(frozen=True)
class ServerRestartDone:
    exit_code: int


@dataclass(frozen=True)
class JobStarted:
    job_id: int


@dataclass(frozen=True)
class JobFinished:
    job_id: int
    source: JobSource
    result: InjectResult
    timings: Mapping[str, float] = field(default_factory=dict, compare=False)
    non_latin: bool = False  # letters outside the Latin script, injected unchanged (task 4.4)
    session_id: int | None = None  # continuous session (one clipboard-only notification, 5.3)


@dataclass(frozen=True)
class JobDiscarded:
    job_id: int
    source: JobSource
    reason: Literal["no_speech", "filtered", "cancelled"]


@dataclass(frozen=True)
class JobFailed:
    job_id: int
    source: JobSource
    audio_s: float
    error: str


@dataclass(frozen=True)
class EngineStateChanged:
    state: EngineHealth


@dataclass(frozen=True)
class ReloadRequested:
    reply: Reply = field(default=None, compare=False)


@dataclass(frozen=True)
class HistoryInsert:
    """IPC `last` (task 5.2): insert the n-th newest history text again (1 = the newest)."""

    n: int = 1
    reply: Reply = field(default=None, compare=False)


@dataclass(frozen=True)
class HistoryRequested:
    """IPC `history` (task 5.2): the history texts, newest first."""

    reply: Reply = field(default=None, compare=False)


@dataclass(frozen=True)
class StatusRequested:
    """IPC `status` (10 §10.4): the controller composes the status in its own thread."""

    reply: Reply = field(default=None, compare=False)


@dataclass(frozen=True)
class ShutdownRequested:
    pass


@dataclass(frozen=True)
class X11ConnectionLost:
    pass


Event = (
    PttPressed
    | PttReleased
    | PttCancelKey
    | ContinuousToggle
    | LanguageSwitch
    | CancelRequested
    | RecordingStarted
    | RecordingLimitReached
    | RecordingFinished
    | SpeechStarted
    | SpeechEnded
    | SegmentReady
    | FlushDone
    | MicrophoneSilent
    | CaptureOpenDue
    | ReconnectTick
    | AudioError
    | ServerRestartDone
    | JobStarted
    | JobFinished
    | JobDiscarded
    | JobFailed
    | EngineStateChanged
    | ReloadRequested
    | StatusRequested
    | HistoryInsert
    | HistoryRequested
    | ShutdownRequested
    | X11ConnectionLost
)
