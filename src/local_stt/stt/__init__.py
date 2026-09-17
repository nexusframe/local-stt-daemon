"""STT engine registry (docs/06-stt-engine.md §6.9): `stt.engine` name -> implementation."""

from local_stt.stt.whisper_server import WhisperServerEngine

ENGINES = {"whisper-server": WhisperServerEngine}
