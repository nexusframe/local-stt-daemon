import queue
import time
from pathlib import Path

import numpy as np
import pytest

from local_stt.audio.capture import FRAME_SAMPLES, AudioFrame
from local_stt.audio.file_source import FRAME_S, FileAudioSource
from local_stt.audio.wav import float32_to_wav_bytes, wav_bytes_to_float32
from local_stt.interfaces import AudioOpenError

# 2.5 frames: the third one is completed with silence.
LENGTH = 2 * FRAME_SAMPLES + FRAME_SAMPLES // 2


@pytest.fixture
def wav(tmp_path: Path) -> Path:
    path = tmp_path / "speech.wav"
    ramp = np.linspace(0.1, 0.9, LENGTH, dtype=np.float32)
    path.write_bytes(float32_to_wav_bytes(ramp))
    return path


def expected_audio(path: Path) -> np.ndarray:
    audio, _ = wav_bytes_to_float32(path.read_bytes())  # s16-quantized like the source
    return audio


def take(frames: "queue.SimpleQueue[AudioFrame]", count: int) -> list[AudioFrame]:
    return [frames.get(timeout=2) for _ in range(count)]


def test_plays_file_then_silence_with_ids_and_timestamps(wav: Path) -> None:
    frames: queue.SimpleQueue[AudioFrame] = queue.SimpleQueue()
    source = FileAudioSource(frames, wav)
    before = time.monotonic()
    source.open(7, 3)
    got = take(frames, 5)
    source.close()

    assert {(f.recording_id, f.capture_id) for f in got} == {(7, 3)}
    assert all(len(f.samples) == FRAME_SAMPLES and f.samples.dtype == np.float32 for f in got)
    assert before <= got[0].started_at <= before + 0.05
    assert [f.started_at - got[0].started_at for f in got] == pytest.approx(
        [i * FRAME_S for i in range(5)]
    )
    played = np.concatenate([f.samples for f in got])
    assert np.array_equal(played[:LENGTH], expected_audio(wav))
    assert not played[LENGTH:].any()


def test_frames_are_paced_in_real_time(wav: Path) -> None:
    frames: queue.SimpleQueue[AudioFrame] = queue.SimpleQueue()
    source = FileAudioSource(frames, wav)
    source.open(1, 1)
    started = time.monotonic()
    take(frames, 8)
    elapsed = time.monotonic() - started
    source.close()
    assert 7 * FRAME_S <= elapsed < 8 * FRAME_S + 0.15


def test_loop_restarts_the_file(wav: Path) -> None:
    frames: queue.SimpleQueue[AudioFrame] = queue.SimpleQueue()
    source = FileAudioSource(frames, wav, loop=True)
    source.open(1, 1)
    got = take(frames, 4)
    source.close()
    assert np.array_equal(got[3].samples, got[0].samples)


def test_no_frame_after_close_and_reopen_starts_over(wav: Path) -> None:
    frames: queue.SimpleQueue[AudioFrame] = queue.SimpleQueue()
    source = FileAudioSource(frames, wav)
    source.open(1, 1)
    first = take(frames, 2)
    source.close()
    assert not source.is_open
    time.sleep(2 * FRAME_S)
    while not frames.empty():  # frames put before close() returned
        assert frames.get().capture_id == 1
    assert frames.empty()

    source.open(2, 5)
    again = frames.get(timeout=2)
    source.close()
    assert (again.recording_id, again.capture_id) == (2, 5)
    assert np.array_equal(again.samples, first[0].samples)


def test_close_without_open_and_double_open(wav: Path) -> None:
    source = FileAudioSource(queue.SimpleQueue(), wav)
    source.close()
    source.open(1, 1)
    with pytest.raises(AudioOpenError):
        source.open(2, 2)
    source.close()


def test_rejects_other_sample_rates(tmp_path: Path) -> None:
    path = tmp_path / "44k.wav"
    path.write_bytes(float32_to_wav_bytes(np.zeros(100, dtype=np.float32), 44100))
    with pytest.raises(ValueError, match="44100 Hz"):
        FileAudioSource(queue.SimpleQueue(), path)


def test_empty_file_gives_silence_even_when_looping(tmp_path: Path) -> None:
    path = tmp_path / "empty.wav"
    path.write_bytes(float32_to_wav_bytes(np.zeros(0, dtype=np.float32)))
    frames: queue.SimpleQueue[AudioFrame] = queue.SimpleQueue()
    source = FileAudioSource(frames, path, loop=True)
    source.open(1, 1)
    got = take(frames, 2)
    source.close()
    assert not any(f.samples.any() for f in got)
