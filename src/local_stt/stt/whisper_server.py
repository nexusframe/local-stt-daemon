"""whisper.cpp `whisper-server` client and temporary server process (docs/06-stt-engine.md).

The client uses only `http.client` to 127.0.0.1 (docs/12 §12.2).
"""

import collections
import http.client
import json
import math
import secrets
import socket
import subprocess
import threading
import time
from pathlib import Path
from types import TracebackType
from typing import Any

import numpy as np
from numpy.typing import NDArray

from local_stt.audio.wav import SAMPLE_RATE, float32_to_wav_bytes
from local_stt.interfaces import EngineHealth, Transcript, TranscriptSegment

HOST = "127.0.0.1"  # hard-coded, never configurable (N5)
ENGINE_NAME = "whisper.cpp"
DATA_DIR = Path.home() / ".local/share/local-stt"
DEFAULT_BINARY = DATA_DIR / "bin/whisper-server"
DEFAULT_SECRET_FILE = Path.home() / ".config/local-stt/secret"
# Tag install.sh builds (DEFAULT_WHISPER_TAG there; a unit test keeps them equal). `doctor`
# compares it with bin/.whisper-tag, since whisper-server has no --version flag (06 §6.2).
WHISPER_TAG = "v1.9.4"

_ERROR_BODY_LIMIT = 200


class EngineError(Exception):
    """Base class for transcription failures."""


class EngineConnectionError(EngineError):
    """The server does not accept connections (worker pauses, engine DOWN — 04 §4.4)."""


class EngineTimeoutError(EngineError):
    """The request exceeded its timeout (E8: one retry)."""


class EngineHttpError(EngineError):
    """Non-200 HTTP status (E9: retry for 5xx, not for 4xx)."""

    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body


class EngineResponseError(EngineError):
    """HTTP 200 with a body that is not a valid verbose_json response."""


def read_request_path(secret_file: Path = DEFAULT_SECRET_FILE) -> str:
    secret = secret_file.read_text(encoding="ascii").strip()
    if len(secret) != 32 or any(c not in "0123456789abcdef" for c in secret):
        raise ValueError(f"{secret_file}: expected 32 lowercase hex characters")
    return "/" + secret


FULL_AUDIO_CTX = 1500  # 30 s encoder window


def select_audio_ctx(duration_s: float, audio_ctx: int, margin: int) -> int:
    """`audio_ctx` for one request (06 §6.7): the fixed value if the recording fits, else 0.

    Only these two values are ever sent to a server: per-request values break decoding in a
    long-running whisper-server v1.9.4.
    """
    needed = math.ceil(duration_s * 50) + margin
    return audio_ctx if audio_ctx > 0 and needed <= audio_ctx else 0


def _check_audio_ctx(audio_ctx: int, margin: int) -> None:
    if not (audio_ctx == 0 or 0 <= margin < audio_ctx < FULL_AUDIO_CTX):
        raise ValueError(
            f"audio_ctx must be 0 or in (margin, {FULL_AUDIO_CTX}), "
            f"got {audio_ctx} (margin {margin})"
        )


def build_multipart(
    fields: list[tuple[str, str]], file_field: str, filename: str, file_bytes: bytes
) -> tuple[str, bytes]:
    boundary = "local-stt-" + secrets.token_hex(16)
    parts: list[bytes] = []
    for name, value in fields:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
            + value.encode()
            + b"\r\n"
        )
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
        "Content-Type: audio/wav\r\n\r\n".encode()
        + file_bytes
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return f"multipart/form-data; boundary={boundary}", b"".join(parts)


def parse_verbose_json(
    body: bytes, *, audio_duration_s: float, processing_s: float, model: str
) -> Transcript:
    try:
        data = json.loads(body)
        segments = [
            TranscriptSegment(
                text=str(s["text"]),
                start_s=float(s["start"]),
                end_s=float(s["end"]),
                no_speech_prob=_optional_float(s.get("no_speech_prob")),
                avg_logprob=_optional_float(s.get("avg_logprob")),
            )
            for s in data["segments"]
        ]
    except (ValueError, KeyError, TypeError) as e:
        raise EngineResponseError(f"invalid verbose_json response: {e}") from e
    return Transcript(
        text="".join(s.text for s in segments),
        segments=segments,
        audio_duration_s=audio_duration_s,
        processing_s=processing_s,
        engine=ENGINE_NAME,
        model=model,
    )


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


class WhisperServerEngine:
    """SttEngine backed by a running `whisper-server` on loopback."""

    name = ENGINE_NAME

    def __init__(
        self,
        *,
        port: int,
        request_path: str,
        model: str,
        audio_ctx: int = 0,
        audio_ctx_margin: int = 128,
        health_timeout_s: float = 2.0,
    ):
        _check_audio_ctx(audio_ctx, audio_ctx_margin)
        self.port = port
        self.request_path = request_path
        self.model = model
        self.audio_ctx = audio_ctx
        self.audio_ctx_margin = audio_ctx_margin
        self.health_timeout_s = health_timeout_s

    def health(self) -> EngineHealth:
        try:
            status, _ = self._request("GET", "/health", timeout_s=self.health_timeout_s)
        except EngineError:
            return EngineHealth.DOWN
        if status == 200:
            return EngineHealth.READY
        if status == 503:
            return EngineHealth.STARTING
        return EngineHealth.DOWN

    def transcribe(
        self,
        audio: NDArray[np.float32],
        *,
        sample_rate: int,
        language: str,
        prompt: str | None,
        timeout_s: float,
    ) -> Transcript:
        if sample_rate != SAMPLE_RATE:
            raise ValueError(f"sample_rate must be {SAMPLE_RATE}, got {sample_rate}")
        duration_s = len(audio) / sample_rate
        audio_ctx = select_audio_ctx(duration_s, self.audio_ctx, self.audio_ctx_margin)
        fields = [
            ("response_format", "verbose_json"),
            ("language", language),
            ("no_language_probabilities", "true"),  # otherwise the encoder runs twice
            ("temperature", "0.0"),
            ("temperature_inc", "0.2"),
            ("no_timestamps", "false"),
            ("audio_ctx", str(audio_ctx)),
        ]
        if prompt:
            fields.append(("prompt", prompt))
        content_type, body = build_multipart(
            fields, "file", "audio.wav", float32_to_wav_bytes(audio, sample_rate)
        )

        started = time.monotonic()
        status, response = self._request(
            "POST", "/inference", body=body, content_type=content_type, timeout_s=timeout_s
        )
        processing_s = time.monotonic() - started
        if status != 200:
            raise EngineHttpError(
                status, response[:_ERROR_BODY_LIMIT].decode("utf-8", errors="replace")
            )
        return parse_verbose_json(
            response, audio_duration_s=duration_s, processing_s=processing_s, model=self.model
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        timeout_s: float,
        body: bytes | None = None,
        content_type: str | None = None,
    ) -> tuple[int, bytes]:
        conn = http.client.HTTPConnection(HOST, self.port, timeout=timeout_s)
        headers = {"Content-Type": content_type} if content_type else {}
        try:
            conn.request(method, self.request_path + path, body=body, headers=headers)
            response = conn.getresponse()
            return response.status, response.read()
        except TimeoutError as e:
            raise EngineTimeoutError(f"{method} {path}: timed out after {timeout_s:.1f} s") from e
        except OSError as e:  # refused, reset, remote disconnected
            raise EngineConnectionError(f"{method} {path}: {e}") from e
        finally:
            conn.close()


class TemporaryWhisperServer:
    """A private `whisper-server` on a random free loopback port (13 §13.4, 10 §10.1).

    Used by `transcribe --model` and `bench`: random port and request path, `nice -n 5`
    (matching the systemd unit), stopped on exit. Server stderr is kept in memory only.
    """

    _BIND_ATTEMPTS = 3

    def __init__(
        self,
        model_path: Path,
        *,
        model: str,
        threads: int = 4,
        beam_size: int = -1,
        audio_ctx: int = 0,
        audio_ctx_margin: int = 128,
        language: str = "pl",
        binary: Path = DEFAULT_BINARY,
        startup_timeout_s: float = 120.0,
    ):
        self.model_path = model_path
        self.model = model
        self.threads = threads
        self.beam_size = beam_size
        _check_audio_ctx(audio_ctx, audio_ctx_margin)
        self.audio_ctx = audio_ctx
        self.audio_ctx_margin = audio_ctx_margin
        self.language = language
        self.binary = binary
        self.startup_timeout_s = startup_timeout_s
        self._process: subprocess.Popen[bytes] | None = None
        self._stderr: collections.deque[str] = collections.deque(maxlen=20)
        self.engine: WhisperServerEngine | None = None

    @property
    def pid(self) -> int | None:
        """PID of the whisper-server process (`nice` execs it, so this is the server itself)."""
        return self._process.pid if self._process is not None else None

    def __enter__(self) -> WhisperServerEngine:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    def start(self) -> WhisperServerEngine:
        if not self.binary.is_file():
            raise EngineError(f"{self.binary} not found (run scripts/install.sh)")
        if not self.model_path.is_file():
            raise EngineError(f"{self.model_path} not found (run: local-stt models pull ...)")
        for _ in range(self._BIND_ATTEMPTS):
            engine = self._start_once(_free_port(), "/" + secrets.token_hex(16))
            if engine is not None:
                self.engine = engine
                return engine
        raise EngineError(f"could not bind a free port in {self._BIND_ATTEMPTS} attempts")

    def _start_once(self, port: int, request_path: str) -> WhisperServerEngine | None:
        """Returns None if the port was taken in the meantime (caller retries)."""
        self._stderr.clear()
        # fmt: off
        args = [
            "nice", "-n", "5", str(self.binary),
            "--host", HOST, "--port", str(port), "--request-path", request_path,
            "-m", str(self.model_path), "-l", self.language, "-t", str(self.threads),
            "-bs", str(self.beam_size), "-sns",
        ]
        # fmt: on
        process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self._process = process
        threading.Thread(target=self._drain_stderr, args=(process,), daemon=True).start()

        engine = WhisperServerEngine(
            port=port,
            request_path=request_path,
            model=self.model,
            audio_ctx=self.audio_ctx,
            audio_ctx_margin=self.audio_ctx_margin,
        )
        deadline = time.monotonic() + self.startup_timeout_s
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self._process = None
                if any("couldn't bind" in line for line in self._stderr):
                    return None
                raise EngineError(
                    f"whisper-server exited with code {process.returncode}: "
                    + " | ".join(self._stderr)
                )
            if engine.health() is EngineHealth.READY:
                return engine
            time.sleep(0.1)
        self.stop()
        raise EngineError(f"whisper-server not ready within {self.startup_timeout_s:.0f} s")

    def _drain_stderr(self, process: "subprocess.Popen[bytes]") -> None:
        assert process.stderr is not None
        for raw in process.stderr:
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                self._stderr.append(line)

    def stop(self) -> None:
        process, self._process = self._process, None
        self.engine = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        port: int = s.getsockname()[1]
        return port
