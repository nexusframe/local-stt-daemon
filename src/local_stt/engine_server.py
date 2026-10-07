"""Parakeet inference server: `local-stt engine-server` (task 4.2, ADR-018).

The `local-stt-engine.service` process. It speaks the subset of the whisper-server HTTP contract
the daemon uses (docs/06-stt-engine.md §6.5), so the client side stays the same:

- `GET <request-path>/health` → 200 `{"status": "ok"}`,
- `POST <request-path>/inference` (multipart/form-data, `file` = 16 kHz mono s16 WAV) → 200
  `verbose_json` with one segment; the other form fields (`language`, `prompt`, `audio_ctx`, ...)
  are accepted and ignored, because Parakeet has no language or prompt input.

Like whisper-server v1.9.4, the model is loaded before the socket listens, so a client sees
connection errors and then 200, never 503. One request is decoded at a time. It listens only on
127.0.0.1 (N5) under the random request path from the 0600 secret (ADR-002). Transcripts are
never logged (docs/12).
"""

import email.parser
import email.policy
import json
import logging
import threading
import time
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
from numpy.typing import NDArray

from local_stt.audio.wav import SAMPLE_RATE, wav_bytes_to_float32
from local_stt.stt.parakeet import PARAKEET_MODEL
from local_stt.stt.whisper_server import DEFAULT_SECRET_FILE, HOST, read_request_path

if TYPE_CHECKING:
    from local_stt.config import Config

log = logging.getLogger("local_stt.engine_server")

EXIT_CONFIG = 78  # EX_CONFIG, as cli.EXIT_CONFIG: systemd does not restart on it

ONNX_ASR_MODEL = "nemo-parakeet-tdt-0.6b-v3"  # onnx-asr model type for that directory
# 64 MiB of s16 WAV is ~35 min of audio; the daemon sends segments of at most ~30 s.
MAX_BODY_BYTES = 64 * 1024 * 1024

Recognizer = Callable[[NDArray[np.float32], int], str]


def load_parakeet(model_dir: Path, threads: int) -> Recognizer:
    """Loads the int8 Parakeet export (`istupakov/parakeet-tdt-0.6b-v3-onnx`) via onnx-asr."""
    import onnx_asr
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.intra_op_num_threads = threads
    model = onnx_asr.load_model(
        ONNX_ASR_MODEL, model_dir, quantization="int8", sess_options=options
    )

    def recognize(audio: NDArray[np.float32], sample_rate: int) -> str:
        if sample_rate != SAMPLE_RATE:  # the handler rejects other rates with HTTP 400
            raise ValueError(f"expected {SAMPLE_RATE} Hz, got {sample_rate}")
        return model.recognize(audio, sample_rate=16000)

    return recognize


class RequestError(Exception):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status


def parse_wav_upload(content_type: str, body: bytes) -> bytes:
    """The `file` part of a multipart/form-data body (the whisper-server field name)."""
    if not content_type.lower().startswith("multipart/form-data"):
        raise RequestError(HTTPStatus.BAD_REQUEST, "expected multipart/form-data")
    message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        f"Content-Type: {content_type}\r\n\r\n".encode("latin-1") + body
    )
    if not message.is_multipart():
        raise RequestError(HTTPStatus.BAD_REQUEST, "malformed multipart body")
    for part in message.iter_parts():
        if part.get_param("name", header="content-disposition") == "file":
            payload = part.get_payload(decode=True)
            if isinstance(payload, bytes):
                return payload
    raise RequestError(HTTPStatus.BAD_REQUEST, "missing 'file' field")


def verbose_json(text: str, duration_s: float) -> dict[str, Any]:
    """The whisper-server `verbose_json` subset the client reads (06 §6.5, §6.8).

    Parakeet has no segment confidences, so both are null and the no-speech filter
    (06 §6.8 rule 1) does not apply. An empty result has no segments.
    """
    segments = []
    if text:
        segments.append(
            {"id": 0, "text": " " + text, "start": 0.0, "end": round(duration_s, 3),
             "avg_logprob": None, "no_speech_prob": None}
        )  # fmt: skip
    return {"text": " " + text if text else "", "duration": duration_s, "segments": segments}


class EngineServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, request_path: str, recognize: Recognizer):
        self.request_path = request_path
        self.recognize = recognize
        self.inference_lock = threading.Lock()
        super().__init__((HOST, port), _Handler)


class _Handler(BaseHTTPRequestHandler):
    server: EngineServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        if self.path != self.server.request_path + "/health":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        self._send_json(HTTPStatus.OK, {"status": "ok"})

    def do_POST(self) -> None:
        if self.path != self.server.request_path + "/inference":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            response = self._inference()
        except RequestError as e:
            self._send_json(e.status, {"error": str(e)})
            return
        except Exception as e:  # a model failure must not kill the server thread silently
            log.exception("inference failed")
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"inference failed: {e}"})
            return
        self._send_json(HTTPStatus.OK, response)

    def _inference(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise RequestError(HTTPStatus.LENGTH_REQUIRED, "Content-Length required") from None
        if not 0 < length <= MAX_BODY_BYTES:
            raise RequestError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"body of {length} bytes")
        body = self.rfile.read(length)
        wav = parse_wav_upload(self.headers.get("Content-Type", ""), body)
        try:
            audio, rate = wav_bytes_to_float32(wav)
        except ValueError as e:
            raise RequestError(HTTPStatus.BAD_REQUEST, str(e)) from None
        if rate != SAMPLE_RATE:
            raise RequestError(HTTPStatus.BAD_REQUEST, f"expected {SAMPLE_RATE} Hz, got {rate}")
        duration_s = len(audio) / rate
        with self.server.inference_lock:
            started = time.monotonic()
            text = self.server.recognize(audio, rate).strip()
            elapsed = time.monotonic() - started
        log.info("inference: %.2f s of audio in %.2f s", duration_s, elapsed)
        return verbose_json(text, duration_s)

    def _send_json(self, status: HTTPStatus, data: dict[str, Any]) -> None:
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        log.debug("%s " + format, self.address_string(), *args)


def run_engine_server(config: "Config") -> int:
    """Entry point of `local-stt engine-server` (the systemd unit's ExecStart)."""
    from local_stt.logging_setup import setup_logging
    from local_stt.sdnotify import SdNotifier

    setup_logging(logging.INFO)
    stt = config.stt
    model_dir = stt.models_dir / PARAKEET_MODEL
    try:
        request_path = read_request_path(DEFAULT_SECRET_FILE)
    except (OSError, ValueError) as e:
        log.error("secret: %s", e)
        return EXIT_CONFIG
    if not model_dir.is_dir():
        log.error("%s not found (run scripts/install.sh)", model_dir)
        return EXIT_CONFIG

    started = time.monotonic()
    recognize = load_parakeet(model_dir, stt.threads)
    log.info("loaded %s in %.1f s (%d threads)", PARAKEET_MODEL, time.monotonic() - started,
             stt.threads)  # fmt: skip
    server = EngineServer(stt.port, request_path, recognize)
    log.info("listening on %s:%d", HOST, stt.port)
    notifier = SdNotifier()
    notifier.ready()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        notifier.stopping()
        server.server_close()
    return 0
