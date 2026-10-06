"""bench --context (task 3.4): policies, raw WER, and the run through the real pipeline."""

import numpy as np
import pytest
from numpy.typing import NDArray

from local_stt.bench.context import Policy, policies, raw_wer, score, transcribe_policy
from local_stt.config import Config
from local_stt.interfaces import AudioSegment, EngineHealth, Transcript, TranscriptSegment


class ScriptedEngine:
    name = "scripted"

    def __init__(self, texts: list[str]) -> None:
        self.texts = texts
        self.prompts: list[str | None] = []

    def health(self) -> EngineHealth:
        return EngineHealth.READY

    def transcribe(
        self,
        audio: NDArray[np.float32],
        *,
        sample_rate: int,
        language: str,
        prompt: str | None,
        timeout_s: float,
    ) -> Transcript:
        self.prompts.append(prompt)
        text = self.texts.pop(0)
        return Transcript(text, [TranscriptSegment(text, 0.0, 1.0, 0.01, -0.2)], 1.0, 0.5, "w", "m")


def segment(seq: int, pause: float | None) -> AudioSegment:
    return AudioSegment(np.zeros(16000, dtype=np.float32), 1, seq, 0.0, 1000, "silence", pause)


def test_policies_are_the_product_of_lengths_and_resets() -> None:
    assert [p.label for p in policies([0, 200], [None, 5.0])] == [
        "chars=0 reset=off",
        "chars=0 reset=5s",
        "chars=200 reset=off",
        "chars=200 reset=5s",
    ]


@pytest.mark.parametrize(
    ("hypothesis", "errors"),
    [
        ("Ala ma kota. Kot ma Alę.", 0),
        ("ala ma kota kot ma Alę", 4),  # "Ala", "kota.", "Kot", "Alę." differ in case/punctuation
        ("Ala ma kota - Kot ma Alę.", 1),  # the dash is not a word; "kota" lost its period
    ],
)
def test_raw_wer_keeps_case_and_punctuation(hypothesis: str, errors: int) -> None:
    assert raw_wer("Ala ma kota. Kot ma Alę.", hypothesis) == (errors, 6)


def test_score_counts_filtered_segments() -> None:
    result = score("Ala ma kota.", ["Ala ma ", None, "kota. "])
    assert (result["word_errors"], result["raw_word_errors"], result["filtered"]) == (0, 0, 1)


def test_policy_runs_the_segments_in_order_through_the_pipeline() -> None:
    engine = ScriptedEngine([" Ala ma kota.", " Kot ma Alę.", " Koniec."])
    segments = [segment(1, None), segment(2, 1.0), segment(3, 6.0)]
    texts = transcribe_policy(engine, segments, Config(), Policy(200, 5.0))
    assert texts == ["Ala ma kota. ", "Kot ma Alę. ", "Koniec. "]
    assert engine.prompts == [None, "Ala ma kota.", None]  # the 6 s pause reset the context
