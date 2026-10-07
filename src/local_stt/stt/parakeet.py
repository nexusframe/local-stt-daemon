"""Client of the Parakeet engine server, `local-stt-engine.service` (task 4.3, ADR-018).

The server (`local_stt.engine_server`) speaks the whisper-server HTTP subset of 06 §6.5, so this
is `WhisperServerEngine` with Parakeet's name and model and without the inputs Parakeet does
not have: it detects the language itself and takes no prompt, so the prompt is never sent and
`audio_ctx` is always 0 (the full input).
"""

import numpy as np
from numpy.typing import NDArray

from local_stt.interfaces import Transcript
from local_stt.stt.whisper_server import WhisperServerEngine

ENGINE_NAME = "parakeet"
PARAKEET_MODEL = "parakeet-tdt-0.6b-v3-int8"  # directory under stt.models_dir


class ParakeetEngine(WhisperServerEngine):
    name = ENGINE_NAME

    def __init__(self, *, port: int, request_path: str, health_timeout_s: float = 2.0):
        super().__init__(
            port=port,
            request_path=request_path,
            model=PARAKEET_MODEL,
            health_timeout_s=health_timeout_s,
        )

    def transcribe(
        self,
        audio: NDArray[np.float32],
        *,
        sample_rate: int,
        language: str,
        prompt: str | None,
        timeout_s: float,
    ) -> Transcript:
        return super().transcribe(
            audio, sample_rate=sample_rate, language=language, prompt=None, timeout_s=timeout_s
        )
