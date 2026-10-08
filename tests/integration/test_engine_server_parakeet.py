"""Engine server with the real Parakeet model (task 4.2, needs_parakeet), via the whisper client."""

import threading
from collections.abc import Iterator
from typing import Any

import numpy as np
import pytest

from local_stt import engine_server
from local_stt.audio.wav import wav_bytes_to_float32
from local_stt.engine_server import PARAKEET_MODEL, EngineServer, load_parakeet
from local_stt.interfaces import EngineHealth
from local_stt.stt import whisper_server as ws
from local_stt.stt.parakeet import TemporaryParakeetServer

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


def test_temporary_server_transcribes_in_its_own_process() -> None:
    # task 4.5: the server bench and `transcribe --model` start
    if not MODEL_DIR.is_dir():
        pytest.skip(f"{MODEL_DIR} not installed")
    audio, rate = wav_bytes_to_float32(FIXTURE.read_bytes())
    server = TemporaryParakeetServer(MODEL_DIR, threads=4)
    with server as engine:
        assert server.pid is not None
        t = engine.transcribe(audio, sample_rate=rate, language="pl", prompt=None, timeout_s=30)
    assert "spacer" in t.text.lower()
    assert t.model == PARAKEET_MODEL
    assert server.pid is None


def test_arena_shrinks_after_more_than_30_s_with_onnx_asr() -> None:
    # task 4.8: relies on onnx-asr's private asr._encoder and on 100 feature frames per second
    if not MODEL_DIR.is_dir():
        pytest.skip(f"{MODEL_DIR} not installed")
    import onnx_asr

    model = onnx_asr.load_model(engine_server.ONNX_ASR_MODEL, MODEL_DIR, quantization="int8")
    assert engine_server.shrink_arena_after_long_runs(model)
    wrapped = model.asr._encoder
    shrunk: list[bool] = []
    inner_run = wrapped._session.run

    def spy(output_names: Any, feeds: Any, run_options: Any = None) -> Any:
        shrunk.append(run_options is not None)
        return inner_run(output_names, feeds, run_options)

    wrapped._session = type("Spy", (), {"run": staticmethod(spy)})()
    audio, rate = wav_bytes_to_float32(FIXTURE.read_bytes())
    for seconds in (29, 31):
        clip = np.resize(audio, seconds * rate)
        assert "spacer" in model.recognize(clip, sample_rate=rate).lower()
    assert shrunk == [False, True]
