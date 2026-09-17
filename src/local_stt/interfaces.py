"""Component interfaces and shared data types (docs/02-architecture.md §2.6).

Components depend on each other only through these types; `app.py` wires implementations.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

import numpy as np
from numpy.typing import NDArray


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
    model: str  # "small-q5_1"


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
