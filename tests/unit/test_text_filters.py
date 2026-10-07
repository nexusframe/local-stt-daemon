import pytest

from local_stt.config import DEFAULT_HALLUCINATION_PATTERNS
from local_stt.interfaces import TranscriptSegment
from local_stt.text.filters import (
    collapse_loops,
    compile_patterns,
    has_non_latin_letters,
    is_filler_only,
    is_hallucination,
    is_no_speech,
    is_prompt_echo,
    segment_rejections,
)

PATTERNS = compile_patterns(DEFAULT_HALLUCINATION_PATTERNS)


def seg(
    text: str, no_speech: float | None = 0.01, logprob: float | None = -0.2
) -> TranscriptSegment:
    return TranscriptSegment(text, 0.0, 1.0, no_speech, logprob)


@pytest.mark.parametrize(
    "text",
    [
        " Napisy stworzone przez społeczność Amara.org",
        " napisy wykonane przez społeczność amara.org",
        " Zdjęcia i napisy stworzone przez społeczność Amara.org",
        " Tłumaczenie i napisy stworzone przez społeczność Amara.org",
        " Dziękuję za uwagę.",
        " Dzięki za obejrzenie!",
        "dziękuję za oglądanie",
        " Subskrybuj kanał!",
        " Zasubskrybuj i zostaw łapkę.",
    ],
)
def test_hallucination_patterns_match(text: str) -> None:
    assert is_hallucination(text, PATTERNS)


@pytest.mark.parametrize(
    "text",
    [
        " Dziękuję za uwagę, a teraz przejdźmy do pytań.",
        " Na koniec: dziękuję za uwagę.",
        " Nie zapomnij subskrybować kanału. A teraz dalej.",
        " Amara.org to strona z napisami.",
        " Ala ma kota.",
    ],
)
def test_hallucination_patterns_keep_normal_speech(text: str) -> None:
    assert not is_hallucination(text, PATTERNS)


@pytest.mark.parametrize(
    ("no_speech", "logprob", "rejected"),
    [
        (0.9, -1.5, True),
        (0.9, -0.5, False),  # confident text despite no_speech_prob
        (0.1, -1.5, False),
        (0.6, -1.5, False),  # thresholds are strict
        (0.9, -1.0, False),
        (None, -1.5, False),
        (0.9, None, False),
    ],
)
def test_no_speech_requires_both_conditions(
    no_speech: float | None, logprob: float | None, rejected: bool
) -> None:
    assert is_no_speech(seg(" x", no_speech, logprob), 0.6, -1.0) is rejected


def test_segment_rejections_reasons() -> None:
    segments = [
        seg(" Ala ma"),
        seg(" kota."),
        seg(" kota. "),  # identical to the previous one after strip
        seg(" Napisy stworzone przez społeczność Amara.org"),
        seg(" cisza", 0.9, -2.0),
        seg(" Koniec."),
    ]
    assert segment_rejections(segments, PATTERNS, 0.6, -1.0) == [
        None,
        None,
        "repeated_segment",
        "hallucination",
        "no_speech",
        None,
    ]


def test_repetition_rule_can_be_skipped() -> None:
    segments = [seg(" kota."), seg(" kota.")]
    assert segment_rejections(segments, PATTERNS, 0.6, -1.0, repetitions=False) == [None, None]


def test_repetition_compares_with_the_preceding_segment_only() -> None:
    segments = [seg(" tak."), seg(" nie."), seg(" tak.")]
    assert segment_rejections(segments, PATTERNS, 0.6, -1.0) == [None, None, None]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # a loop spanning several 60-character segments, after normal speech
        (
            " Mówię coś ważnego i nie wiem co dalej nie wiem co dalej nie wiem co dalej"
            " nie wiem co dalej nie wiem co dalej",
            " Mówię coś ważnego i nie wiem co dalej",
        ),
        ("raz dwa trzy raz dwa trzy Raz dwa trzy raz dwa trzy.", "raz dwa trzy."),
        ("a b c a b c a b c a b c, dalej", "a b c, dalej"),
        ("a b c a b c a b c a b c koniec", "a b c koniec"),
        ("a b c a b c a b c", "a b c a b c a b c"),  # only 3 copies
        ("tak tak tak tak tak tak", "tak tak tak tak tak tak"),  # 1-word n-gram is not enough
        ("ab c d ab c d ab c d ab c dx", "ab c d ab c d ab c d ab c dx"),  # word boundary
        ("", ""),
    ],
)
def test_collapse_loops(text: str, expected: str) -> None:
    assert collapse_loops(text) == expected


@pytest.mark.parametrize(
    ("text", "tail", "echo"),
    [
        ("ma kota.", "Wczoraj Ala ma kota.", True),
        ("Ala  ma\nKOTA.", "wczoraj ala ma kota.", True),
        ("Wczoraj Ala ma kota.", "Wczoraj Ala ma kota.", True),
        ("a kota.", "Wczoraj Ala ma kota.", False),  # not at a word boundary
        ("Ala ma psa.", "Wczoraj Ala ma kota.", False),
        ("Gdańsk", None, False),  # PTT: vocabulary_prompt is never an echo source
        ("Gdańsk", "", False),
        ("", "Wczoraj Ala ma kota.", False),
    ],
)
def test_prompt_echo(text: str, tail: str | None, echo: bool) -> None:
    assert is_prompt_echo(text, tail) is echo


# --- Parakeet fillers and non-Latin output (task 4.4) -----------------------------------------


@pytest.mark.parametrize(
    "text",
    # measured on the user's non-speech takes and quiet windows (2026-10-07)
    ["Hm", "Mm.", "Um", "Mm, mm, um", "Mm-mm.", "Mm-hmm.", "So", "Cool.", "Yeah.", "Uh-huh.",
     "mhm", "Ah!", "  Hmm…  "],
)  # fmt: skip
def test_filler_only_results(text: str) -> None:
    assert is_filler_only(text)


@pytest.mark.parametrize(
    "text",
    ["", "Yeah, I agree.", "So what?", "Mmm, dobrze.", "Ummah", "hmm tak", "Cool cool.",
     "Ech, nie wiem.", "No."],
)  # fmt: skip
def test_real_speech_is_not_a_filler(text: str) -> None:
    assert not is_filler_only(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Zażółć gęślą jaźń.", False),
        ("Po code review zrób rebase i force push.", False),
        ("naïve café, 12:30!", False),
        ("По код ревю зроб ребейс.", True),  # the ADR-018 failure
        ("Po code ревю.", True),
        ("αβγ", True),
    ],
)
def test_non_latin_letters(text: str, expected: bool) -> None:
    assert has_non_latin_letters(text) is expected
