import logging
from pathlib import Path

import numpy as np
import pytest
from numpy.typing import NDArray

from local_stt.audio.vad import (
    CONTEXT_SAMPLES,
    FRAME_SAMPLES,
    SileroVad,
    VadTrimmer,
    speech_frames,
)
from local_stt.audio.wav import wav_bytes_to_float32
from local_stt.config import Config, VadConfig
from local_stt.stt.whisper_server import DATA_DIR

MODEL = DATA_DIR / "models" / "silero_vad.onnx"
SPEECH = Path(__file__).parent.parent / "fixtures" / "pl_short.wav"
PAD = 300 * 16  # speech_pad_ms default, in samples


@pytest.fixture(scope="module")
def silero() -> SileroVad:
    if not MODEL.is_file():
        pytest.skip("silero_vad.onnx not installed (local-stt models pull silero-vad)")
    return SileroVad(MODEL)


def frames(audio: NDArray[np.float32]) -> list[NDArray[np.float32]]:
    count = len(audio) // FRAME_SAMPLES
    return [audio[i * FRAME_SAMPLES : (i + 1) * FRAME_SAMPLES] for i in range(count)]


def speech() -> NDArray[np.float32]:
    audio, _ = wav_bytes_to_float32(SPEECH.read_bytes())
    return audio


# --- SileroVad on the real model (14 §14.2) --------------------------------------------------


def test_silence_and_noise_are_not_speech(silero: SileroVad) -> None:
    rng = np.random.default_rng(0)
    for audio in (
        np.zeros(16000, dtype=np.float32),
        (rng.standard_normal(16000 * 3) * 10 ** (-28 / 20)).astype(np.float32),  # loud room noise
    ):
        silero.reset()
        assert max(silero(f) for f in frames(audio)) < 0.1


def test_speech_is_detected(silero: SileroVad) -> None:
    silero.reset()
    assert max(silero(f) for f in frames(speech())) > 0.8


def test_reset_restores_the_initial_state(silero: SileroVad) -> None:
    audio = frames(speech())[10:30]
    silero.reset()
    first = [silero(f) for f in audio]
    silero.reset()
    assert [silero(f) for f in audio] == first


def test_state_and_context_carry_over_between_frames(silero: SileroVad) -> None:
    audio = frames(speech())[14:20]
    silero.reset()
    in_sequence = [silero(f) for f in audio]
    isolated = []
    for f in audio:
        silero.reset()
        isolated.append(silero(f))
    assert in_sequence[0] == isolated[0]
    assert in_sequence[1:] != isolated[1:]


def test_input_tensor_has_context_prefix(
    silero: SileroVad, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, np.ndarray]] = []
    run = silero._session.run

    def spy(outputs: object, feeds: dict[str, np.ndarray]) -> object:
        seen.append({k: np.array(v, copy=True) for k, v in feeds.items()})
        return run(outputs, feeds)

    monkeypatch.setattr(silero._session, "run", spy)
    a, b = frames(speech())[14:16]
    silero.reset()
    silero(a)
    silero(b)
    assert seen[0]["input"].shape == (1, CONTEXT_SAMPLES + FRAME_SAMPLES)
    assert not seen[0]["input"][0, :CONTEXT_SAMPLES].any()
    assert np.array_equal(seen[1]["input"][0, :CONTEXT_SAMPLES], a[-CONTEXT_SAMPLES:])
    assert seen[0]["state"].shape == (2, 1, 128) and not seen[0]["state"].any()
    assert seen[1]["state"].any()
    assert int(seen[0]["sr"]) == 16000


def test_frame_size_is_checked(silero: SileroVad) -> None:
    with pytest.raises(ValueError, match="512"):
        silero(np.zeros(480, dtype=np.float32))


# --- speech_frames and VadTrimmer (05 §5.3) -------------------------------------------------


@pytest.mark.parametrize(
    ("probabilities", "expected"),
    [
        ([0.1, 0.2, 0.3], None),
        ([0.1, 0.6, 0.9, 0.2], (1, 3)),
        ([0.5], (0, 1)),
        ([0.4, 0.9, 0.4, 0.1, 0.36, 0.2], (1, 5)),  # tail: last p >= end_threshold (0.35)
        ([0.45, 0.45, 0.9, 0.1], (2, 3)),  # head: first p >= start_threshold only
    ],
)
def test_speech_frames(probabilities: list[float], expected: tuple[int, int] | None) -> None:
    assert speech_frames(probabilities, 0.5, 0.35) == expected


class ScriptedVad:
    """Returns the frame's first sample as its probability."""

    def __init__(self) -> None:
        self.resets = 0

    def reset(self) -> None:
        self.resets += 1

    def __call__(self, frame: NDArray[np.float32]) -> float:
        assert frame.shape == (FRAME_SAMPLES,)
        return float(frame[0])


def scripted_audio(probabilities: list[float], tail: int = 0) -> NDArray[np.float32]:
    audio = np.repeat(np.array(probabilities, dtype=np.float32), FRAME_SAMPLES)
    return np.concatenate([audio, np.full(tail, probabilities[-1], dtype=np.float32)])


def trimmer(**vad: object) -> tuple[VadTrimmer, list[Path]]:
    loaded: list[Path] = []

    def load(path: Path) -> ScriptedVad:
        loaded.append(path)
        return ScriptedVad()

    config = Config(vad=VadConfig(**vad))  # type: ignore[arg-type]
    return VadTrimmer(config, load=load), loaded


def test_trim_keeps_speech_with_padding() -> None:
    t, _ = trimmer()
    p = [0.0] * 20 + [0.9] * 10 + [0.0] * 20
    trimmed = t.trim(scripted_audio(p))
    assert trimmed is not None
    start, end = 20 * FRAME_SAMPLES - PAD, 30 * FRAME_SAMPLES + PAD
    assert np.array_equal(trimmed, scripted_audio(p)[start:end])


def test_trim_clips_padding_at_the_edges_and_reads_the_partial_last_frame() -> None:
    t, _ = trimmer()
    audio = scripted_audio([0.9, 0.0, 0.9], tail=100)  # the last partial frame is speech
    assert np.array_equal(t.trim(audio), audio)  # type: ignore[arg-type]


def test_trim_without_speech_is_none_and_resets_each_time() -> None:
    t, _ = trimmer()
    assert t.trim(scripted_audio([0.1, 0.4, 0.2])) is None
    assert t.trim(np.zeros(0, dtype=np.float32)) is None
    assert t._vad.resets == 2  # type: ignore[union-attr]


def test_disabled_vad_loads_nothing_and_is_inactive() -> None:
    t, loaded = trimmer(enabled=False)
    assert not t.active and loaded == []
    with pytest.raises(RuntimeError):
        t.trim(np.zeros(512, dtype=np.float32))


def test_update_reloads_only_a_changed_model_and_applies_settings() -> None:
    t, loaded = trimmer()
    first = t._vad
    t.update(Config(vad=VadConfig(speech_pad_ms=0)))
    assert t._vad is first and len(loaded) == 1
    trimmed = t.trim(scripted_audio([0.0, 0.9, 0.0]))
    assert trimmed is not None and len(trimmed) == FRAME_SAMPLES

    t.update(Config(vad=VadConfig(model="other.onnx")))
    assert [p.name for p in loaded] == ["silero_vad.onnx", "other.onnx"]
    t.update(Config(vad=VadConfig(enabled=False)))
    assert not t.active
    t.update(Config(vad=VadConfig(model="other.onnx")))
    assert t.active and len(loaded) == 3  # enabled again: loaded again


def test_load_failure_falls_back_to_rms_gate(caplog: pytest.LogCaptureFixture) -> None:
    def broken(path: Path) -> ScriptedVad:
        raise RuntimeError("bad model")

    caplog.set_level(logging.ERROR, logger="local_stt.audio")
    t = VadTrimmer(Config(), load=broken)
    assert not t.active
    assert "using the RMS gate" in caplog.text


def test_real_model_trims_leading_and_trailing_silence(silero: SileroVad) -> None:
    audio = np.concatenate(
        [np.zeros(32000, dtype=np.float32), speech(), np.zeros(32000, dtype=np.float32)]
    )
    t = VadTrimmer(Config(), load=lambda path: silero)
    trimmed = t.trim(audio)
    assert trimmed is not None
    assert len(speech()) * 0.6 < len(trimmed) < len(speech()) + 2 * PAD
    assert t.trim(np.zeros(48000, dtype=np.float32)) is None
