"""float32 <-> 16-bit PCM WAV conversion in memory (docs/05-audio-and-vad.md §5.1)."""

import io
import wave

import numpy as np
from numpy.typing import NDArray

SAMPLE_RATE = 16000
_S16_MAX = 32767


def float32_to_wav_bytes(samples: NDArray[np.float32], sample_rate: int = SAMPLE_RATE) -> bytes:
    """Encode mono float32 samples in [-1, 1] as RIFF WAV (PCM s16le); out-of-range values clip."""
    if samples.ndim != 1:
        raise ValueError(f"expected mono samples (1-D array), got shape {samples.shape}")
    pcm = np.rint(np.clip(samples, -1.0, 1.0) * _S16_MAX).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def wav_bytes_to_float32(data: bytes) -> tuple[NDArray[np.float32], int]:
    """Decode a mono PCM s16le WAV into float32 samples and its sample rate.

    Raises ValueError for anything else (not a WAV, float WAV, stereo, other bit depth).
    """
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            if w.getnchannels() != 1 or w.getsampwidth() != 2:
                raise ValueError(
                    f"expected mono 16-bit WAV, got {w.getnchannels()} ch, "
                    f"{8 * w.getsampwidth()} bit"
                )
            rate = w.getframerate()
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    except (wave.Error, EOFError) as e:
        raise ValueError(f"not a PCM WAV file: {e}") from e
    return pcm.astype(np.float32) / np.float32(_S16_MAX), rate
