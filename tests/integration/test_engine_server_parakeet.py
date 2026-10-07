"""Engine server with the real Parakeet model (task 4.2, needs_parakeet), via the whisper client."""

import threading
from collections.abc import Iterator

import pytest

from local_stt.audio.wav import wav_bytes_to_float32
from local_stt.engine_server import PARAKEET_MODEL, EngineServer, load_parakeet
from local_stt.interfaces import EngineHealth
from local_stt.stt import whisper_server as ws

from .test_whisper_server import FIXTURE

pytestmark = pytest.mark.needs_parakeet

MODEL_DIR = ws.DATA_DIR / "models" / PARAKEET_MODEL
REQUEST_PATH = "/" + "ab" * 16


@pytest.fixture(scope="module")
def engine() -> Iterator[ws.WhisperServerEngine]:
    if not MODEL_DIR.is_dir():
        pytest.skip(f"{MODEL_DIR} not installed")
    server = EngineServer(0, REQUEST_PATH, load_parakeet(MODEL_DIR, threads=4))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield ws.WhisperServerEngine(
        port=server.server_address[1], request_path=REQUEST_PATH, model=PARAKEET_MODEL
    )
    server.shutdown()
    server.server_close()


def test_transcribes_polish_speech(engine: ws.WhisperServerEngine) -> None:
    assert engine.health() is EngineHealth.READY
    audio, rate = wav_bytes_to_float32(FIXTURE.read_bytes())
    t = engine.transcribe(audio, sample_rate=rate, language="pl", prompt=None, timeout_s=30)

    assert "spacer" in t.text.lower()
    assert t.processing_s < 5
    assert len(t.segments) == 1 and t.segments[0].end_s == pytest.approx(len(audio) / rate, 1e-3)
