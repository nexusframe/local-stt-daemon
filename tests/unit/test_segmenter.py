"""Segmenter (05 §5.5, 14 §14.2): ScriptedVad returns a prescribed p sequence; frame samples
encode their sample index, so segment boundaries are asserted to the sample."""

import numpy as np
import pytest
from numpy.typing import NDArray

from local_stt.audio.capture import FRAME_SAMPLES, SAMPLE_RATE, AudioFrame
from local_stt.audio.segmenter import Segmenter, SegmenterOutput
from local_stt.config import VadConfig
from local_stt.interfaces import AudioSegment

FRAME_S = FRAME_SAMPLES / SAMPLE_RATE  # 32 ms
T0 = 100.0
PAD = 300 * 16  # speech_pad_ms default, in samples
S, M, Q = 0.9, 0.4, 0.1  # speech (>= start), between the thresholds, silence (< end)


class ScriptedVad:
    def __init__(self) -> None:
        self.script: list[float] = []
        self.resets = 0

    def reset(self) -> None:
        self.resets += 1

    def __call__(self, frame: NDArray[np.float32]) -> float:
        assert frame.shape == (FRAME_SAMPLES,)
        return self.script.pop(0)


class Run:
    """A Segmenter fed a contiguous stream from T0, frame by frame."""

    def __init__(self, **config: float) -> None:
        self.vad = ScriptedVad()
        self.seg = Segmenter(VadConfig(**config), self.vad)  # type: ignore[arg-type]
        self.seg.reset(session_id=7)
        self.next = 0

    def feed(self, probabilities: list[float]) -> list[SegmenterOutput]:
        out: list[SegmenterOutput] = []
        for p in probabilities:
            self.vad.script.append(p)
            first = self.next * FRAME_SAMPLES
            samples = np.arange(first, first + FRAME_SAMPLES, dtype=np.float32)
            out += self.seg.add(AudioFrame(1, 1, T0 + self.next * FRAME_S, samples))
            self.next += 1
        return out

    def end_of(self, frame: int) -> float:
        return T0 + (frame + 1) * FRAME_S


def segments(out: list[SegmenterOutput]) -> list[AudioSegment]:
    return [o for o in out if isinstance(o, AudioSegment)]


def span(segment: AudioSegment) -> tuple[int, int]:
    """Half-open sample range of the stream; the samples must be contiguous."""
    first, last = int(segment.samples[0]), int(segment.samples[-1]) + 1
    assert np.array_equal(segment.samples, np.arange(first, last, dtype=np.float32))
    return first, last


# --- start ----------------------------------------------------------------------------------


def test_speech_starts_after_min_speech_ms() -> None:
    run = Run()
    assert run.feed([Q] * 20 + [S] * 7) == []  # 224 ms < 250 ms
    assert run.seg.state == "CANDIDATE"
    assert run.feed([M]) == ["speech_started"]  # 8th frame: 256 ms, between thresholds counts
    assert run.seg.state == "SPEECH"


@pytest.mark.parametrize("dip", [Q, 0.34])
def test_short_impulse_is_rejected(dip: float) -> None:
    run = Run()
    assert run.feed([Q] * 20 + [S] * 7 + [dip]) == []
    assert run.seg.state == "SILENCE"
    assert run.feed([Q] * 30) == []


def test_between_thresholds_does_not_start_speech() -> None:
    run = Run()
    assert run.feed([M] * 50) == []
    assert run.seg.state == "SILENCE"


# --- end, hysteresis, padding ---------------------------------------------------------------


def test_segment_with_preroll_and_trailing_pad() -> None:
    run = Run()
    out = run.feed([Q] * 20 + [S] * 30 + [Q] * 21)
    assert out == ["speech_started"]
    assert run.seg.state == "TRAILING"
    out = run.feed([Q])  # 22 x 32 ms = 704 ms >= 700 ms
    assert out[1:] == ["speech_ended"]
    (seg,) = segments(out)
    assert span(seg) == (20 * FRAME_SAMPLES - PAD, 50 * FRAME_SAMPLES + PAD)
    assert (seg.session_id, seg.seq, seg.cut) == (7, 1, "silence")
    assert seg.speech_ms == 30 * 32
    assert seg.ended_at == pytest.approx(run.end_of(71))
    assert run.seg.state == "SILENCE"


def test_preroll_is_clipped_to_the_stream_start() -> None:
    run = Run()
    (seg,) = segments(run.feed([Q] * 3 + [S] * 10 + [Q] * 22))
    assert span(seg)[0] == 0


def test_hysteresis_between_thresholds_does_not_end_speech() -> None:
    run = Run()
    out = run.feed([Q] * 10 + [S] * 10 + [M] * 60 + [S, M] * 20)
    assert out == ["speech_started"]
    assert run.seg.state == "SPEECH"


def test_silence_counter_survives_between_frames_and_resets_on_speech() -> None:
    run = Run()
    run.feed([Q] * 10 + [S] * 10)
    assert run.feed([Q] * 21 + [S] + [Q] * 21) == []  # speech frame resets the counter
    out = run.feed([M] * 5 + [Q])  # 21 + 1 silent frames, M frames in between change nothing
    (seg,) = segments(out)
    assert span(seg)[1] == 69 * FRAME_SAMPLES  # through the M at 67, pad clipped to the data
    assert seg.speech_ms == (68 - 10) * 32


def test_seq_increases_and_next_preroll_is_not_duplicated() -> None:
    run = Run()
    (a,) = segments(run.feed([Q] * 10 + [S] * 10 + [Q] * 22))
    (b,) = segments(run.feed([S] * 10 + [Q] * 22))
    assert (a.seq, b.seq) == (1, 2)
    assert span(a)[1] == 20 * FRAME_SAMPLES + PAD
    assert span(b) == (42 * FRAME_SAMPLES - PAD, 52 * FRAME_SAMPLES + PAD)


def test_preroll_after_a_short_pause_does_not_overlap_the_previous_segment() -> None:
    run = Run(min_silence_ms=320)  # 10 frames of silence, pad 300 ms = 9.4 frames
    (a,) = segments(run.feed([Q] * 10 + [S] * 10 + [Q] * 10))
    (b,) = segments(run.feed([S] * 10 + [Q] * 10))
    assert span(b)[0] == span(a)[1]


# --- max_segment_s --------------------------------------------------------------------------


def test_split_in_the_middle_of_the_longest_silence_run() -> None:
    # 100 frames = 3.2 s; 9.4 frames of pre-roll + 91 frames from frame 10 reach it at 100.
    run = Run(max_segment_s=3.2, split_search_s=1.6)  # window: the last 50 frames
    out = run.feed([Q] * 10 + [S] * 63 + [Q] * 5 + [S] * 8 + [Q] * 3 + [S] * 11)
    assert segments(out) == []
    (seg,) = segments(run.feed([S]))
    assert seg.cut == "max_length"
    assert seg.ended_at == pytest.approx(run.end_of(100))
    assert span(seg) == (10 * FRAME_SAMPLES - PAD, 75 * FRAME_SAMPLES)  # run 73..77 > 86..88
    assert seg.speech_ms == 63 * 32
    assert run.seg.state == "SPEECH"
    (rest,) = segments(run.feed([Q] * 22))
    assert span(rest) == (75 * FRAME_SAMPLES, 101 * FRAME_SAMPLES + PAD)
    assert (rest.seq, rest.cut, rest.speech_ms) == (2, "silence", (101 - 78) * 32)


def test_split_at_the_lowest_p_without_a_silence_run() -> None:
    run = Run(max_segment_s=3.2, split_search_s=1.6)
    script = [Q] * 10 + [S] * 80 + [0.6, 0.55, 0.7] + [S] * 8
    (seg,) = segments(run.feed(script))
    assert span(seg)[1] == 91 * FRAME_SAMPLES  # frame 91 (p = 0.55) starts the remainder


def test_remainder_with_too_little_speech_is_dropped() -> None:
    run = Run(max_segment_s=3.2, split_search_s=1.6)
    out = run.feed([Q] * 10 + [S] * 70 + [Q] * 21)
    (first,) = segments(out)  # split in the middle of the trailing silence run (80..100)
    assert (first.cut, span(first)[1]) == ("max_length", 90 * FRAME_SAMPLES)
    out = run.feed([Q])
    assert out == ["speech_ended"]  # only silence left: no segment, seq not used
    (nxt,) = segments(run.feed([S] * 10 + [Q] * 22))
    assert nxt.seq == 2


# --- flush, reset ---------------------------------------------------------------------------


def test_flush_emits_the_utterance_in_progress() -> None:
    run = Run()
    run.feed([Q] * 10 + [S] * 10 + [Q] * 5)
    out = run.seg.flush(at=123.0)
    (seg,) = segments(out)
    assert out[-1] == "speech_ended"
    assert span(seg) == (10 * FRAME_SAMPLES - PAD, 25 * FRAME_SAMPLES)  # pad clipped to data
    assert (seg.cut, seg.ended_at) == ("flush", 123.0)
    assert run.seg.state == "SILENCE"


def test_flush_drops_a_candidate_and_short_remainders() -> None:
    run = Run()
    run.feed([Q] * 10 + [S] * 5)
    assert run.seg.flush(at=1.0) == []
    run = Run(max_segment_s=3.2, split_search_s=1.6)
    run.feed([Q] * 10 + [S] * 70 + [Q] * 21 + [S] * 3)  # split at 90, remainder 90..103
    assert run.seg.flush(at=1.0) == ["speech_ended"]  # 96 ms of speech < 250 ms


def test_reset_clears_buffers_and_vad_and_keeps_seq_within_a_session() -> None:
    run = Run()
    assert run.vad.resets == 1
    run.feed([Q] * 10 + [S] * 10 + [Q] * 22 + [S] * 5)
    run.seg.reset(session_id=7)  # reconnect
    assert (run.vad.resets, run.seg.state) == (2, "SILENCE")
    (seg,) = segments(run.feed([S] * 10 + [Q] * 22))
    assert seg.seq == 2
    assert span(seg)[0] == 47 * FRAME_SAMPLES  # no pre-roll from before the reset
    run.seg.reset(session_id=8)
    (seg,) = segments(run.feed([S] * 10 + [Q] * 22))
    assert (seg.session_id, seg.seq) == (8, 1)
