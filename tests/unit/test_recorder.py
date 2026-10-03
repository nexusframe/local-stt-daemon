import numpy as np
import pytest

from local_stt.audio.capture import FRAME_SAMPLES, SAMPLE_RATE, AudioFrame
from local_stt.audio.recorder import Recorder
from local_stt.events import RecordingLimitReached

FRAME_S = FRAME_SAMPLES / SAMPLE_RATE  # 32 ms
T0 = 100.0


def frame(i: int, *, rid: int = 1, cid: int = 1) -> AudioFrame:
    """Frame `i` of a contiguous stream starting at T0; sample values encode their index."""
    first = i * FRAME_SAMPLES
    samples = np.arange(first, first + FRAME_SAMPLES, dtype=np.float32)
    return AudioFrame(rid, cid, T0 + i * FRAME_S, samples)


def feed(rec: Recorder, count: int) -> list[RecordingLimitReached]:
    return [e for i in range(count) if (e := rec.add(frame(i))) is not None]


def test_joins_frames_into_clip() -> None:
    rec = Recorder(1, 1, max_duration_s=120)
    feed(rec, 10)
    clip = rec.end(T0 + 10)
    assert clip.sample_rate == SAMPLE_RATE
    assert np.array_equal(clip.samples, np.arange(10 * FRAME_SAMPLES, dtype=np.float32))
    assert clip.duration_s == pytest.approx(10 * FRAME_S)
    assert clip.started_at == T0
    assert clip.ended_at == T0 + 10


def test_empty_buffer_gives_empty_clip() -> None:
    clip = Recorder(1, 1, max_duration_s=120).end(T0)
    assert clip.samples.dtype == np.float32
    assert len(clip.samples) == 0
    assert clip.duration_s == 0
    assert clip.started_at == clip.ended_at == T0


def test_trims_to_ended_at_within_a_frame() -> None:
    rec = Recorder(1, 1, max_duration_s=120)
    feed(rec, 10)
    ended_at = T0 + 0.1  # 1600 samples
    clip = rec.end(ended_at)
    assert np.array_equal(clip.samples, np.arange(1600, dtype=np.float32))
    assert clip.ended_at == ended_at


def test_masks_start_sound_retroactively() -> None:
    # The mask command arrives after frames were already buffered (04 §4.3).
    rec = Recorder(1, 1, max_duration_s=120)
    feed(rec, 5)
    rec.mask_start_sound(T0 + 0.21)  # 3360 samples
    feed_more = [rec.add(frame(i)) for i in range(5, 10)]
    assert feed_more == [None] * 5
    clip = rec.end(T0 + 10)
    assert clip.samples[0] == 3360
    assert len(clip.samples) == 10 * FRAME_SAMPLES - 3360
    assert clip.started_at == pytest.approx(T0 + 0.21)


def test_short_tap_inside_mask_is_empty() -> None:
    rec = Recorder(1, 1, max_duration_s=120)
    feed(rec, 5)
    rec.mask_start_sound(T0 + 0.21)
    clip = rec.end(T0 + 0.15)
    assert len(clip.samples) == 0
    assert clip.started_at == clip.ended_at == T0 + 0.15


def test_ignores_foreign_frames() -> None:
    rec = Recorder(1, 2, max_duration_s=120)
    assert rec.add(frame(0, rid=1, cid=1)) is None  # old stream of the same recording
    assert rec.add(frame(0, rid=0, cid=2)) is None
    rec.add(frame(0, rid=1, cid=2))
    clip = rec.end(T0 + 10)
    assert len(clip.samples) == FRAME_SAMPLES
    assert rec.matches(1, 2)
    assert not rec.matches(1, 1)


def test_limit_reached_once_and_audio_trimmed_to_limit() -> None:
    rec = Recorder(1, 1, max_duration_s=1.0)
    events = feed(rec, 40)  # 1.28 s
    assert events == [RecordingLimitReached(1, 1, ended_at=T0 + 1.0)]
    clip = rec.end(events[0].ended_at)
    assert len(clip.samples) == SAMPLE_RATE
    assert clip.duration_s == pytest.approx(1.0)


def test_limit_not_reached_below_max_duration() -> None:
    rec = Recorder(1, 1, max_duration_s=1.0)
    assert feed(rec, 31) == []  # 0.992 s
    assert rec.add(frame(31)) is not None  # ends at 1.024 s


def test_end_clears_buffer() -> None:
    rec = Recorder(1, 1, max_duration_s=120)
    feed(rec, 3)
    rec.end(T0 + 10)
    assert len(rec.end(T0 + 10).samples) == 0
