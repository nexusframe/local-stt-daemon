import io
import wave

import numpy as np
import pytest

from local_stt.audio.wav import float32_to_wav_bytes, wav_bytes_to_float32


def test_header_is_mono_s16_16k() -> None:
    data = float32_to_wav_bytes(np.zeros(1600, dtype=np.float32))
    assert data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    with wave.open(io.BytesIO(data), "rb") as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (
            1,
            2,
            16000,
            1600,
        )


def test_clipping() -> None:
    samples = np.array([2.0, -2.0, 1.0, -1.0, 0.0], dtype=np.float32)
    decoded, _ = wav_bytes_to_float32(float32_to_wav_bytes(samples))
    np.testing.assert_array_equal(decoded, [1.0, -1.0, 1.0, -1.0, 0.0])


def test_round_trip() -> None:
    rng = np.random.default_rng(0)
    samples = rng.uniform(-1, 1, 16000).astype(np.float32)
    decoded, rate = wav_bytes_to_float32(float32_to_wav_bytes(samples, sample_rate=16000))
    assert rate == 16000
    assert decoded.dtype == np.float32
    assert np.max(np.abs(decoded - samples)) <= 0.5 / 32767 + 1e-7


def test_rejects_non_mono_input() -> None:
    with pytest.raises(ValueError, match="mono"):
        float32_to_wav_bytes(np.zeros((10, 2), dtype=np.float32))
