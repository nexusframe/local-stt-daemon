"""STT engine registry (docs/06-stt-engine.md §6.9): `stt.engine` name -> implementation."""

from local_stt.stt.parakeet import ParakeetEngine
from local_stt.stt.whisper_server import WhisperServerEngine

ENGINES: dict[str, type[WhisperServerEngine]] = {
    "whisper-server": WhisperServerEngine,
    "parakeet": ParakeetEngine,
}

# systemd user unit that serves each engine; the daemon runs only the selected one (task 4.3).
ENGINE_UNITS = {
    "whisper-server": "local-stt-whisper.service",
    "parakeet": "local-stt-engine.service",
}
