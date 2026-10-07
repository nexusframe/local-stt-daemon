"""Client of the Parakeet engine server, `local-stt-engine.service` (task 4.3, ADR-018).

The server (`local_stt.engine_server`) speaks the whisper-server HTTP subset of 06 §6.5, so this
is `WhisperServerEngine` with Parakeet's name and model and without the inputs Parakeet does
not have: it detects the language itself and takes no prompt, so the prompt is never sent and
`audio_ctx` is always 0 (the full input).
"""

import os
import sys
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from local_stt.interfaces import Transcript
from local_stt.stt.whisper_server import (
    EngineError,
    TemporaryWhisperServer,
    WhisperServerEngine,
)

ENGINE_NAME = "parakeet"
PARAKEET_MODEL = "parakeet-tdt-0.6b-v3-int8"  # directory under stt.models_dir
REQUEST_PATH_ENV = "LOCAL_STT_REQUEST_PATH"  # set only by TemporaryParakeetServer


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


class TemporaryParakeetServer(TemporaryWhisperServer):
    """A private `local-stt engine-server` on a random free loopback port (task 4.5).

    Used by `transcribe --model` and `bench` as `TemporaryWhisperServer` is: random port and
    request path (passed in the environment, not on the command line), `nice -n 5`, stopped on
    exit. `model_path` is the model directory; beam size, `audio_ctx` and language do not apply.
    """

    server_name = "engine-server"

    def __init__(self, model_path: Path, *, threads: int = 4, startup_timeout_s: float = 120.0):
        super().__init__(
            model_path, model=PARAKEET_MODEL, threads=threads, startup_timeout_s=startup_timeout_s
        )

    def _check_installed(self) -> None:
        if not self.model_path.is_dir():
            raise EngineError(
                f"{self.model_path} not found (run: local-stt models pull {PARAKEET_MODEL})"
            )

    def _command(self, port: int, request_path: str) -> tuple[list[str], dict[str, str] | None]:
        # fmt: off
        args = [
            "nice", "-n", "5", sys.executable, "-m", "local_stt", "engine-server",
            "--port", str(port), "--threads", str(self.threads),
            "--model-dir", str(self.model_path),
        ]
        # fmt: on
        return args, {**os.environ, REQUEST_PATH_ENV: request_path}

    def _client(self, port: int, request_path: str) -> WhisperServerEngine:
        return ParakeetEngine(port=port, request_path=request_path)
