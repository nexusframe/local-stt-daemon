"""Parakeet engine server (task 4.2) with a fake recognizer, driven by WhisperServerEngine.

The client is the unchanged whisper-server client: these tests prove that the server speaks the
subset of the 06 §6.5 contract the daemon uses.
"""

import http.client
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest

from local_stt import cli, engine_server
from local_stt.audio.wav import float32_to_wav_bytes
from local_stt.engine_server import EngineServer, parse_wav_upload, verbose_json
from local_stt.interfaces import EngineHealth
from local_stt.stt import whisper_server as ws

REQUEST_PATH = "/" + "ab" * 16


class FakeRecognizer:
    def __init__(self, text: str = "Dzień dobry.", delay_s: float = 0.0):
        self.text = text
        self.delay_s = delay_s
        self.calls: list[tuple[int, int]] = []
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def __call__(self, audio: np.ndarray, sample_rate: int) -> str:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(self.delay_s)
        with self._lock:
            self.active -= 1
            self.calls.append((len(audio), sample_rate))
        if self.text == "<raise>":
            raise RuntimeError("model exploded")
        return self.text


@pytest.fixture
def fake() -> FakeRecognizer:
    return FakeRecognizer()


@pytest.fixture
def server(fake: FakeRecognizer) -> Iterator[EngineServer]:
    srv = EngineServer(0, REQUEST_PATH, fake)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _engine(srv: EngineServer, request_path: str = REQUEST_PATH) -> ws.WhisperServerEngine:
    return ws.WhisperServerEngine(
        port=srv.server_address[1], request_path=request_path, model="parakeet"
    )


def _audio(seconds: float = 1.5) -> np.ndarray:
    return np.zeros(int(16000 * seconds), dtype=np.float32)


def _raw(srv: EngineServer, method: str, path: str, body: bytes = b"", content_type: str = ""):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    headers = {"Content-Type": content_type} if content_type else {}
    try:
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def test_listens_on_loopback_only(server: EngineServer) -> None:
    assert server.server_address[0] == "127.0.0.1"


def test_health_ready(server: EngineServer) -> None:
    assert _engine(server).health() is EngineHealth.READY


def test_wrong_request_path_is_404(server: EngineServer) -> None:
    assert _engine(server, "/" + "cd" * 16).health() is EngineHealth.DOWN
    assert _raw(server, "GET", "/health")[0] == 404
    assert _raw(server, "POST", "/inference")[0] == 404


def test_transcribe_round_trip(server: EngineServer, fake: FakeRecognizer) -> None:
    t = _engine(server).transcribe(
        _audio(1.5), sample_rate=16000, language="en", prompt="ignored", timeout_s=5
    )
    assert t.text.strip() == "Dzień dobry."
    assert len(t.segments) == 1
    seg = t.segments[0]
    assert (seg.start_s, seg.end_s) == (0.0, 1.5)
    assert seg.no_speech_prob is None and seg.avg_logprob is None
    assert t.audio_duration_s == 1.5
    assert fake.calls == [(24000, 16000)]


def test_empty_result_has_no_segments(server: EngineServer, fake: FakeRecognizer) -> None:
    fake.text = "  "
    t = _engine(server).transcribe(
        _audio(), sample_rate=16000, language="pl", prompt=None, timeout_s=5
    )
    assert t.text == "" and t.segments == []


def test_model_failure_is_500_and_server_survives(
    server: EngineServer, fake: FakeRecognizer
) -> None:
    fake.text = "<raise>"
    with pytest.raises(ws.EngineHttpError) as e:
        _engine(server).transcribe(
            _audio(), sample_rate=16000, language="pl", prompt=None, timeout_s=5
        )
    assert e.value.status == 500
    assert _engine(server).health() is EngineHealth.READY


def test_one_inference_at_a_time_while_health_answers(
    server: EngineServer, fake: FakeRecognizer
) -> None:
    fake.delay_s = 0.3
    engine = _engine(server)

    def job() -> None:
        engine.transcribe(_audio(), sample_rate=16000, language="pl", prompt=None, timeout_s=5)

    threads = [threading.Thread(target=job) for _ in range(3)]
    for t in threads:
        t.start()
    time.sleep(0.1)
    started = time.monotonic()
    assert engine.health() is EngineHealth.READY
    assert time.monotonic() - started < 0.2  # health does not wait for the inference lock
    for t in threads:
        t.join()
    assert fake.max_active == 1
    assert len(fake.calls) == 3


def test_rejects_wrong_sample_rate(server: EngineServer) -> None:
    content_type, body = ws.build_multipart(
        [], "file", "a.wav", float32_to_wav_bytes(_audio(), 8000)
    )
    status, response = _raw(server, "POST", REQUEST_PATH + "/inference", body, content_type)
    assert status == 400 and b"16000" in response


def test_rejects_non_wav_and_missing_file(server: EngineServer) -> None:
    content_type, body = ws.build_multipart([], "file", "a.wav", b"not a wav")
    assert _raw(server, "POST", REQUEST_PATH + "/inference", body, content_type)[0] == 400
    content_type, body = ws.build_multipart([], "other", "a.wav", b"x")
    assert _raw(server, "POST", REQUEST_PATH + "/inference", body, content_type)[0] == 400
    assert _raw(server, "POST", REQUEST_PATH + "/inference", b"x", "text/plain")[0] == 400


def test_rejects_oversized_body(server: EngineServer, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine_server, "MAX_BODY_BYTES", 100)
    content_type, body = ws.build_multipart([], "file", "a.wav", float32_to_wav_bytes(_audio()))
    assert _raw(server, "POST", REQUEST_PATH + "/inference", body, content_type)[0] == 413


def test_parse_wav_upload_ignores_other_fields() -> None:
    wav = float32_to_wav_bytes(_audio(0.1))
    content_type, body = ws.build_multipart(
        [("language", "pl"), ("prompt", "zażółć")], "file", "audio.wav", wav
    )
    assert parse_wav_upload(content_type, body) == wav


def test_verbose_json_shape() -> None:
    data = verbose_json("Tak.", 2.0)
    assert data["text"] == " Tak."
    assert data["segments"][0]["text"] == " Tak." and data["segments"][0]["end"] == 2.0
    assert verbose_json("", 1.0) == {"text": "", "duration": 1.0, "segments": []}


def test_missing_model_exits_78(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = tmp_path / "secret"
    secret.write_text("ab" * 16)
    monkeypatch.setattr(engine_server, "DEFAULT_SECRET_FILE", secret)
    config = tmp_path / "config.toml"
    config.write_text(f'[stt]\nmodels_dir = "{tmp_path}"\n')
    assert cli.main(["engine-server", "--config", str(config)]) == 78


def test_missing_secret_exits_78(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine_server, "DEFAULT_SECRET_FILE", tmp_path / "nope")
    (tmp_path / engine_server.PARAKEET_MODEL).mkdir()
    config = tmp_path / "config.toml"
    config.write_text(f'[stt]\nmodels_dir = "{tmp_path}"\n')
    assert cli.main(["engine-server", "--config", str(config)]) == 78
