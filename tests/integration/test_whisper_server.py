"""WhisperServerEngine against a real whisper-server (docs/14-tests.md §14.3, needs_whisper)."""

import http.client
import time
from pathlib import Path

import pytest

from local_stt.audio.wav import wav_bytes_to_float32
from local_stt.interfaces import EngineHealth
from local_stt.stt import whisper_server as ws

pytestmark = pytest.mark.needs_whisper

FIXTURE = Path(__file__).parent.parent / "fixtures" / "pl_short.wav"
MODEL = "base-q5_1"
MODEL_PATH = ws.DATA_DIR / "models" / f"ggml-{MODEL}.bin"


@pytest.fixture(scope="module")
def server() -> "ws.TemporaryWhisperServer":
    if not ws.DEFAULT_BINARY.is_file() or not MODEL_PATH.is_file():
        pytest.skip("whisper-server binary or base-q5_1 model not installed")
    srv = ws.TemporaryWhisperServer(MODEL_PATH, model=MODEL)
    srv.start()
    yield srv
    srv.stop()


def test_transcribes_polish_speech(server: ws.TemporaryWhisperServer) -> None:
    assert server.engine is not None
    audio, rate = wav_bytes_to_float32(FIXTURE.read_bytes())
    started = time.monotonic()
    t = server.engine.transcribe(audio, sample_rate=rate, language="pl", prompt=None, timeout_s=30)

    assert time.monotonic() - started < 30
    assert t.text.strip()
    assert "spacer" in t.text.lower()
    assert t.segments and all(s.end_s >= s.start_s for s in t.segments)


def test_health_ready(server: ws.TemporaryWhisperServer) -> None:
    assert server.engine is not None
    assert server.engine.health() is EngineHealth.READY


def test_request_without_prefix_is_404(server: ws.TemporaryWhisperServer) -> None:
    assert server.engine is not None
    conn = http.client.HTTPConnection("127.0.0.1", server.engine.port, timeout=5)
    try:
        conn.request("GET", "/health")
        assert conn.getresponse().status == 404
    finally:
        conn.close()


def test_stop_releases_port() -> None:
    if not ws.DEFAULT_BINARY.is_file() or not MODEL_PATH.is_file():
        pytest.skip("whisper-server binary or base-q5_1 model not installed")
    srv = ws.TemporaryWhisperServer(MODEL_PATH, model=MODEL)
    engine = srv.start()
    srv.stop()
    assert engine.health() is EngineHealth.DOWN


def test_retries_when_port_is_taken(monkeypatch: pytest.MonkeyPatch) -> None:
    if not ws.DEFAULT_BINARY.is_file() or not MODEL_PATH.is_file():
        pytest.skip("whisper-server binary or base-q5_1 model not installed")
    import socket

    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        taken = occupied.getsockname()[1]
        ports = iter([taken, ws._free_port()])
        monkeypatch.setattr(ws, "_free_port", lambda: next(ports))

        srv = ws.TemporaryWhisperServer(MODEL_PATH, model=MODEL)
        try:
            engine = srv.start()
            assert engine.port != taken
            assert engine.health() is EngineHealth.READY
        finally:
            srv.stop()
