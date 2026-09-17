import email.parser
import email.policy
import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest

from local_stt.interfaces import EngineHealth
from local_stt.stt import ENGINES
from local_stt.stt import whisper_server as ws

FIXTURES = Path(__file__).parent.parent / "fixtures"
VERBOSE_JSON = (FIXTURES / "whisper_v1.9.4_verbose_json.json").read_bytes()
REQUEST_PATH = "/" + "ab" * 16


class Stub:
    """Scripted response for the stub server; records requests."""

    def __init__(self) -> None:
        self.status = 200
        self.body = VERBOSE_JSON
        self.delay_s = 0.0
        self.requests: list[tuple[str, str, dict[str, str], bytes]] = []


@pytest.fixture
def stub() -> Iterator[tuple[Stub, int]]:
    state = Stub()

    class Handler(BaseHTTPRequestHandler):
        def _respond(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            state.requests.append((method, self.path, dict(self.headers), body))
            time.sleep(state.delay_s)
            self.send_response(state.status)
            self.send_header("Content-Length", str(len(state.body)))
            self.end_headers()
            self.wfile.write(state.body)

        def do_GET(self) -> None:
            self._respond("GET")

        def do_POST(self) -> None:
            self._respond("POST")

        def log_message(self, format: str, *args: object) -> None:
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield state, httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()


def _engine(port: int, **kwargs: object) -> ws.WhisperServerEngine:
    return ws.WhisperServerEngine(
        port=port,
        request_path=REQUEST_PATH,
        model="base-q5_1",
        **kwargs,  # type: ignore[arg-type]
    )


def _form_fields(headers: dict[str, str], body: bytes) -> dict[str, bytes]:
    raw = f"Content-Type: {headers['Content-Type']}\r\n\r\n".encode() + body
    message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(raw)
    return {
        part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
        for part in message.iter_parts()
    }


def _audio(seconds: float = 1.0) -> np.ndarray:
    return np.zeros(int(16000 * seconds), dtype=np.float32)


def test_registry() -> None:
    assert ENGINES["whisper-server"] is ws.WhisperServerEngine


def test_transcribe_request_contract(stub: tuple[Stub, int]) -> None:
    state, port = stub
    _engine(port).transcribe(_audio(), sample_rate=16000, language="pl", prompt=None, timeout_s=5)

    method, path, headers, body = state.requests[0]
    assert (method, path) == ("POST", REQUEST_PATH + "/inference")
    fields = _form_fields(headers, body)
    assert {k: v for k, v in fields.items() if k != "file"} == {
        "response_format": b"verbose_json",
        "language": b"pl",
        "no_language_probabilities": b"true",
        "temperature": b"0.0",
        "temperature_inc": b"0.2",
        "no_timestamps": b"false",
        "audio_ctx": b"0",
    }
    assert fields["file"][:4] == b"RIFF"


def test_prompt_and_fixed_audio_ctx(stub: tuple[Stub, int]) -> None:
    state, port = stub
    engine = _engine(port, audio_ctx=1000, audio_ctx_margin=128)
    engine.transcribe(
        _audio(3.0), sample_rate=16000, language="pl", prompt="Gdańsk, PipeWire.", timeout_s=5
    )

    fields = _form_fields(state.requests[0][2], state.requests[0][3])
    assert fields["prompt"].decode() == "Gdańsk, PipeWire."
    assert fields["audio_ctx"] == b"1000"  # 3 s fits: ceil(3.0 * 50) + 128 <= 1000


@pytest.mark.parametrize(
    ("duration", "audio_ctx", "expected"),
    [
        (3.0, 1000, 1000),
        (17.4, 1000, 1000),  # 870 + 128 = 998 frames: fits
        (17.46, 1000, 0),  # 873 + 128 = 1001 frames -> full window
        (40.0, 1000, 0),
        (3.0, 0, 0),  # full window configured
    ],
)
def test_select_audio_ctx_sends_only_fixed_value_or_full_window(
    duration: float, audio_ctx: int, expected: int
) -> None:
    assert ws.select_audio_ctx(duration, audio_ctx, 128) == expected


@pytest.mark.parametrize(("audio_ctx", "margin"), [(1500, 128), (100, 128), (-1, 0), (1000, -5)])
def test_invalid_audio_ctx_is_rejected(audio_ctx: int, margin: int) -> None:
    with pytest.raises(ValueError, match="audio_ctx"):
        ws.WhisperServerEngine(
            port=1,
            request_path=REQUEST_PATH,
            model="m",
            audio_ctx=audio_ctx,
            audio_ctx_margin=margin,
        )


def test_long_recording_falls_back_to_full_window(stub: tuple[Stub, int]) -> None:
    state, port = stub
    _engine(port, audio_ctx=1000).transcribe(
        _audio(20.0), sample_rate=16000, language="pl", prompt=None, timeout_s=5
    )
    assert _form_fields(state.requests[0][2], state.requests[0][3])["audio_ctx"] == b"0"


def test_parse_real_v1_9_4_response_preserves_segment_text(stub: tuple[Stub, int]) -> None:
    _, port = stub
    t = _engine(port).transcribe(
        _audio(4.2), sample_rate=16000, language="pl", prompt=None, timeout_s=5
    )

    # v1.9.4 wraps segments at word pieces: " ...intrygującej w" + "iosce."
    assert [s.text for s in t.segments] == [
        " Warto poświęcić po godzinę na spacer po tej intrygującej w",
        "iosce.",
    ]
    assert t.text == " Warto poświęcić po godzinę na spacer po tej intrygującej wiosce."
    assert t.segments[0].no_speech_prob is not None and t.segments[0].avg_logprob is not None
    assert (t.engine, t.model, t.audio_duration_s) == ("whisper.cpp", "base-q5_1", 4.2)
    assert t.processing_s >= 0


@pytest.mark.parametrize("status", [400, 404, 500, 503])
def test_http_error_mapping(stub: tuple[Stub, int], status: int) -> None:
    state, port = stub
    state.status, state.body = status, b'{"error":"' + b"x" * 500 + b'"}'
    with pytest.raises(ws.EngineHttpError) as exc:
        _engine(port).transcribe(
            _audio(), sample_rate=16000, language="pl", prompt=None, timeout_s=5
        )
    assert exc.value.status == status
    assert len(exc.value.body) == 200


def test_invalid_json_is_response_error(stub: tuple[Stub, int]) -> None:
    state, port = stub
    state.body = b"not json"
    with pytest.raises(ws.EngineResponseError):
        _engine(port).transcribe(
            _audio(), sample_rate=16000, language="pl", prompt=None, timeout_s=5
        )


def test_timeout_mapping(stub: tuple[Stub, int]) -> None:
    state, port = stub
    state.delay_s = 1.0
    with pytest.raises(ws.EngineTimeoutError):
        _engine(port).transcribe(
            _audio(), sample_rate=16000, language="pl", prompt=None, timeout_s=0.2
        )


def test_connection_refused_mapping() -> None:
    engine = _engine(ws._free_port())
    with pytest.raises(ws.EngineConnectionError):
        engine.transcribe(_audio(), sample_rate=16000, language="pl", prompt=None, timeout_s=5)
    assert engine.health() is EngineHealth.DOWN


@pytest.mark.parametrize(
    ("status", "expected"),
    [(200, EngineHealth.READY), (503, EngineHealth.STARTING), (500, EngineHealth.DOWN)],
)
def test_health(stub: tuple[Stub, int], status: int, expected: EngineHealth) -> None:
    state, port = stub
    state.status, state.body = status, json.dumps({"status": "x"}).encode()
    assert _engine(port).health() is expected
    assert state.requests[0][:2] == ("GET", REQUEST_PATH + "/health")


def test_rejects_other_sample_rates() -> None:
    with pytest.raises(ValueError, match="16000"):
        _engine(1).transcribe(_audio(), sample_rate=48000, language="pl", prompt=None, timeout_s=1)


def test_read_request_path(tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.write_text("0123456789abcdef" * 2 + "\n")
    assert ws.read_request_path(secret) == "/0123456789abcdef0123456789abcdef"
    secret.write_text("too-short")
    with pytest.raises(ValueError):
        ws.read_request_path(secret)
