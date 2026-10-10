"""Segmenter (05 §5.5, 14 §14.2): ScriptedVad returns a prescribed p sequence; frame samples
encode their sample index, so segment boundaries are asserted to the sample."""

import numpy as np
import pytest
from numpy.typing import NDArray
from pytest import approx

from local_stt.audio.capture import FRAME_SAMPLES, SAMPLE_RATE, AudioFrame
from local_stt.audio.segmenter import (
    Segmenter,
    SegmenterOutput,
    SpeculationRetracted,
    SpeculativeSegment,
    SpeechEnd,
    SpeechStart,
)
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


def kinds(out: list[SegmenterOutput]) -> list[str]:
    """Speech events as "speech_started" / "speech_ended", segments as "segment"."""
    names = {
        SpeechStart: "speech_started",
        SpeechEnd: "speech_ended",
        AudioSegment: "segment",
        SpeculativeSegment: "speculative",
        SpeculationRetracted: "retracted",
    }
    return [names[type(o)] for o in out]


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
    # 8th frame: 256 ms, between thresholds counts
    assert kinds(run.feed([M])) == ["speech_started"]
    assert run.seg.state == "SPEECH"


def test_speech_events_carry_the_speech_times() -> None:
    """6.1: the start is the first speech frame (also from CANDIDATE), not the confirmation;
    the end is the last frame with p >= end_threshold, not the end of the silence."""
    run = Run()
    (start,) = run.feed([Q] * 20 + [S] * 8)
    assert start == SpeechStart(at=approx(T0 + 20 * FRAME_S), confirmed_at=approx(run.end_of(27)))
    out = run.feed([S] * 10 + [M, M] + [Q] * 22)  # speech frames 20..39 (M counts)
    assert out[-1] == SpeechEnd(start=approx(T0 + 20 * FRAME_S), end=approx(run.end_of(39)))


def test_speech_end_after_flush_and_split_keeps_the_utterance_start() -> None:
    run = Run(max_segment_s=3.2, split_search_s=1.6)
    out = run.feed([Q] * 10 + [S] * 70 + [Q] * 21 + [S] * 10 + [Q] * 3)  # split, then speech
    assert kinds(out) == ["speech_started", "segment"]
    end = run.seg.flush(at=200.0)[-1]
    assert end == SpeechEnd(start=approx(T0 + 10 * FRAME_S), end=approx(run.end_of(110)))


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
    assert kinds(out) == ["speech_started"]
    assert run.seg.state == "TRAILING"
    out = run.feed([Q])  # 22 x 32 ms = 704 ms >= 700 ms
    assert kinds(out)[1:] == ["speech_ended"]
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
    assert kinds(out) == ["speech_started"]
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
    assert kinds(out) == ["speech_ended"]  # only silence left: no segment, seq not used
    (nxt,) = segments(run.feed([S] * 10 + [Q] * 22))
    assert nxt.seq == 2


# --- flush, reset ---------------------------------------------------------------------------


def test_flush_emits_the_utterance_in_progress() -> None:
    run = Run()
    run.feed([Q] * 10 + [S] * 10 + [Q] * 5)
    out = run.seg.flush(at=123.0)
    (seg,) = segments(out)
    assert kinds(out)[-1] == "speech_ended"
    assert span(seg) == (10 * FRAME_SAMPLES - PAD, 25 * FRAME_SAMPLES)  # pad clipped to data
    assert (seg.cut, seg.ended_at) == ("flush", 123.0)
    assert run.seg.state == "SILENCE"


def test_flush_drops_a_candidate_and_short_remainders() -> None:
    run = Run()
    run.feed([Q] * 10 + [S] * 5)
    assert run.seg.flush(at=1.0) == []
    run = Run(max_segment_s=3.2, split_search_s=1.6)
    run.feed([Q] * 10 + [S] * 70 + [Q] * 21 + [S] * 3)  # split at 90, remainder 90..103
    assert kinds(run.seg.flush(at=1.0)) == ["speech_ended"]  # 96 ms of speech < 250 ms


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


# --- pause before an utterance (task 3.4) ----------------------------------------------------


def test_pause_runs_from_the_last_speech_frame_to_the_next_first_one() -> None:
    run = Run()
    (first,) = segments(run.feed([Q] * 10 + [S] * 10 + [Q] * 22))  # speech ends with frame 19
    (second,) = segments(run.feed([Q] * 5 + [S] * 10 + [Q] * 22))  # and starts again at 47
    assert first.pause_before_s is None  # nothing before it in this session
    assert second.pause_before_s == pytest.approx(27 * FRAME_S)


def test_rest_of_a_split_has_no_pause() -> None:
    run = Run(max_segment_s=3.2, split_search_s=1.6)
    (seg,) = segments(run.feed([Q] * 10 + [S] * 91))
    (rest,) = segments(run.feed([Q] * 22))
    assert (seg.pause_before_s, rest.pause_before_s) == (None, 0.0)


def test_pause_after_a_dropped_remainder_counts_from_the_emitted_part() -> None:
    # A split whose rest is only silence: the next pause runs from the first part's speech
    # (frame 79), not from an older segment.
    run = Run(max_segment_s=3.2, split_search_s=1.6)
    run.feed([Q] * 10 + [S] * 70 + [Q] * 22)
    (nxt,) = segments(run.feed([S] * 10 + [Q] * 22))  # speech again from frame 102
    assert nxt.pause_before_s == pytest.approx(22 * FRAME_S)


def test_reset_forgets_the_previous_speech() -> None:
    run = Run()
    run.feed([Q] * 10 + [S] * 10 + [Q] * 22)
    run.seg.reset(session_id=7)  # a reconnect
    (seg,) = segments(run.feed([S] * 10 + [Q] * 22))
    assert seg.pause_before_s is None


# --- speculative cuts (task 6.4) ------------------------------------------------------------


def spec_run(speculative_ms: int = 250, **config: float) -> Run:
    run = Run(**config)
    run.seg.reset(session_id=7, speculative_ms=speculative_ms)
    return run


def test_a_short_silence_gives_a_speculative_segment_once() -> None:
    run = spec_run()
    assert kinds(run.feed([Q] * 10 + [S] * 20 + [Q] * 7)) == ["speech_started"]  # 224 ms
    (spec,) = run.feed([Q])  # 256 ms >= 250 ms
    assert isinstance(spec, SpeculativeSegment)
    # Speech 10..29 plus the pre-roll; the trailing pad is cut to the 8 silent frames there.
    assert span(spec.segment) == (10 * FRAME_SAMPLES - PAD, 38 * FRAME_SAMPLES)
    assert (spec.segment.seq, spec.segment.cut, spec.segment.speech_ms) == (1, "silence", 640)
    assert run.feed([Q] * 13) == []  # no second speculation in the same pause
    out = run.feed([Q])  # 704 ms: the end of the utterance
    assert kinds(out) == ["segment", "speech_ended"]
    final = segments(out)[0]
    assert (final.seq, final.reuses_speculative) == (1, True)


def test_speech_after_the_cut_retracts_it() -> None:
    run = spec_run()
    run.feed([Q] * 10 + [S] * 20 + [Q] * 8)
    assert kinds(run.feed([S])) == ["retracted"]
    assert kinds(run.feed([S] * 5 + [Q] * 8)) == ["speculative"]  # the next pause
    out = run.feed([Q] * 14)
    assert segments(out)[0].reuses_speculative is True


def test_a_frame_between_the_thresholds_also_retracts() -> None:
    """It extends the speech range of the final segment, so the text could differ."""
    run = spec_run()
    run.feed([Q] * 10 + [S] * 20 + [Q] * 8)
    assert kinds(run.feed([M])) == ["retracted"]


def test_a_retracted_utterance_does_not_reuse_without_a_new_cut() -> None:
    run = spec_run(speculative_ms=500)
    run.feed([Q] * 10 + [S] * 20 + [Q] * 16)  # cut at 512 ms
    run.feed([M])  # retracted; the silence counter goes on (rule 3)
    out = run.feed([Q] * 6)  # 704 ms: the end, and no new cut (the threshold is behind)
    assert segments(out)[0].reuses_speculative is False


def test_no_speculation_when_it_is_off() -> None:
    run = Run()
    out = run.feed([Q] * 10 + [S] * 20 + [Q] * 22)
    assert kinds(out) == ["speech_started", "segment", "speech_ended"]
    assert segments(out)[0].reuses_speculative is False


def test_a_flush_after_the_cut_reuses_it() -> None:
    run = spec_run()
    run.feed([Q] * 10 + [S] * 20 + [Q] * 10)
    assert segments(run.seg.flush(at=5.0))[0].reuses_speculative is True


def test_no_speculation_after_a_split_in_the_same_utterance() -> None:
    run = spec_run(max_segment_s=3.2, split_search_s=1.6)
    out = run.feed([Q] * 10 + [S] * 95)  # split once
    assert kinds(out) == ["speech_started", "segment"]
    out = run.feed([Q] * 22)
    assert kinds(out) == ["segment", "speech_ended"]  # no speculative part, no reuse
    assert segments(out)[0].reuses_speculative is False


def test_reset_turns_speculation_off_by_default() -> None:
    run = spec_run()
    run.seg.reset(session_id=8)
    assert "speculative" not in kinds(run.feed([Q] * 10 + [S] * 20 + [Q] * 22))
