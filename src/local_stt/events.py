"""Events delivered to the Controller's queue (docs/04-state-machine.md §4.2).

Continuous-mode events (`SpeechStarted`, `SegmentReady`, `FlushDone`, `ReconnectTick`,
`CaptureOpenDue`) are added with continuous mode in v0.2 (task 2.3).
"""

from collections.abc import Mapping
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, Literal

from local_stt.interfaces import AudioClip, Cut, EngineHealth, InjectResult, JobSource

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
    | CancelRequested
    | RecordingStarted
    | RecordingLimitReached
    | RecordingFinished
    | AudioError
    | ServerRestartDone
    | JobStarted
    | JobFinished
    | JobDiscarded
    | JobFailed
    | EngineStateChanged
    | ReloadRequested
    | ShutdownRequested
    | X11ConnectionLost
)
