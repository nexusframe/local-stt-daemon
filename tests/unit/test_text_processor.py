import dataclasses
import logging

import pytest

from local_stt.config import Config, Replacement, TextConfig
from local_stt.interfaces import TextContext, Transcript, TranscriptSegment
from local_stt.text.processor import (
    DefaultTextProcessor,
    apply_replacements,
    assemble,
    normalize_whitespace,
)

PTT = TextContext("ptt", None, None, "release", None, None)


def seg(text: str, no_speech: float = 0.01, logprob: float = -0.2) -> TranscriptSegment:
    return TranscriptSegment(text, 0.0, 1.0, no_speech, logprob)


def transcript(*segments: TranscriptSegment) -> Transcript:
    return Transcript("".join(s.text for s in segments), list(segments), 1.0, 0.5, "w", "m")


def processor(**text: object) -> DefaultTextProcessor:
    config = Config()
    return DefaultTextProcessor(
        dataclasses.replace(config, text=dataclasses.replace(config.text, **text))
    )


@pytest.mark.parametrize(
    ("texts", "rejected", "expected"),
    [
        ([" trans", "krypcja"], [None, None], " transkrypcja"),  # boundary inside a word
        ([" Ala", " ma", " kota."], [None, None, None], " Ala ma kota."),
        ([" Ala ma", " XXX", "kota."], [None, "x", None], " Ala ma kota."),  # separator
        ([" XXX", " Ala"], ["x", None], " Ala"),
        ([" Ala", " XXX"], [None, "x"], " Ala"),
        ([" Ala", " X", " Y", "ma"], [None, "x", "x", None], " Ala ma"),  # one separator
        ([], [], ""),
    ],
)
def test_assemble(texts: list[str], rejected: list[str | None], expected: str) -> None:
    assert assemble([seg(t) for t in texts], rejected) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("  Ala   ma\tkota.  ", "Ala ma kota."),
        (" Ala ma\nkota.\r\nKoniec.", "Ala ma kota. Koniec."),
        (" \n ", ""),
    ],
)
def test_normalize_whitespace(text: str, expected: str) -> None:
    assert normalize_whitespace(text) == expected


def test_replacements_in_order_regex_and_literal() -> None:
    rules = (
        Replacement(r"(?i)\bnowa linia\b", "\n", regex=True),
        Replacement("whisper", "Whisper"),
        Replacement("Whisper.cpp", "whisper.cpp"),
    )
    text = "Pierwsza. Nowa linia whisper.cpp działa, a WHISPER nie."
    assert apply_replacements(text, rules) == "Pierwsza. \n whisper.cpp działa, a WHISPER nie."


def test_literal_replacement_does_not_interpret_regex() -> None:
    assert apply_replacements("a.b axb", (Replacement("a.b", "X"),)) == "X axb"


def test_full_processing_with_trailing_space() -> None:
    p = processor()
    result = p.process(transcript(seg(" Dzień dobry, trans"), seg("krypcja działa.")), PTT)
    assert result == "Dzień dobry, transkrypcja działa. "


def test_append_space_disabled() -> None:
    assert processor(append_space=False).process(transcript(seg(" Ala.")), PTT) == "Ala."


def test_all_segments_filtered_gives_none(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="local_stt.text")
    t = transcript(seg(" Napisy stworzone przez społeczność Amara.org"), seg(" hm", 0.9, -2.0))
    assert processor().process(t, PTT) is None
    assert [r.getMessage() for r in caplog.records] == [
        "filtered: hallucination",
        "filtered: no_speech",
    ]


def test_filtered_segment_between_runs_keeps_words_apart() -> None:
    t = transcript(seg(" Ala ma"), seg(" Dziękuję za uwagę."), seg("kota."))
    assert processor().process(t, PTT) == "Ala ma kota. "


def test_loop_across_segments_is_collapsed(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="local_stt.text")
    loop = " i nie wiem co dalej" * 6
    chunks = [loop[i : i + 60] for i in range(0, len(loop), 60)]  # 60-char server wrapping
    t = transcript(seg(" Zaczynam"), *[seg(c) for c in chunks])
    assert processor().process(t, PTT) == "Zaczynam i nie wiem co dalej "
    assert "filtered: loop" in [r.getMessage() for r in caplog.records]


def test_identical_segment_without_loop_is_dropped() -> None:
    t = transcript(seg(" Ala ma kota."), seg(" Ala ma kota."), seg(" Koniec."))
    assert processor().process(t, PTT) == "Ala ma kota. Koniec. "


def test_vocabulary_word_alone_is_kept_in_ptt() -> None:
    assert processor().process(transcript(seg(" Gdańsk.")), PTT) == "Gdańsk. "


def test_prompt_echo_rejected_in_continuous() -> None:
    ctx = TextContext("continuous", 1, 2, "silence", "silence", "Wczoraj Ala ma kota.")
    assert processor().process(transcript(seg(" Ala ma kota.")), ctx) is None
    assert processor().process(transcript(seg(" Ala ma psa.")), ctx) == "Ala ma psa. "


def test_replacement_to_newline_survives_normalization() -> None:
    p = processor(replacements=(Replacement(r"(?i)\s*nowa linia\s*", "\n", regex=True),))
    assert p.process(transcript(seg(" Punkt pierwszy nowa linia punkt drugi")), PTT) == (
        "Punkt pierwszy\npunkt drugi "
    )


def test_replacement_emptying_text_gives_none() -> None:
    p = processor(replacements=(Replacement("eee", ""),))
    assert p.process(transcript(seg(" eee")), PTT) is None


def test_update_config_recompiles_patterns() -> None:
    p = processor()
    t = transcript(seg(" Prywatna fraza."))
    assert p.process(t, PTT) == "Prywatna fraza. "
    config = Config()
    p.update_config(
        dataclasses.replace(config, text=TextConfig(hallucination_patterns=("prywatna",)))
    )
    assert p.process(t, PTT) is None
