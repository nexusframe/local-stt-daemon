"""Segment and result filters (docs/06-stt-engine.md §6.8).

Pure functions; `TextProcessor` decides the order. Loops are collapsed on the assembled text
rather than rejected per segment: whisper-server wraps segments at ~60 characters, so a
decoder loop almost always spans several segments (user decision 2026-10-03).
"""

import re
import unicodedata
from collections.abc import Sequence

from local_stt.interfaces import TranscriptSegment

# An n-gram of 3+ words followed by 3+ more consecutive copies; the last copy may be
# followed by punctuation. n is capped so backtracking stays bounded on long PTT results.
_LOOP = re.compile(r"(?<!\S)(\S+(?:\s+\S+){2,29}?)(?:\s+\1){3,}(?![^\s.,;:!?…])", re.IGNORECASE)
_WS = re.compile(r"\s+")

# Parakeet's output for non-speech that passed the VAD gate (task 4.4, measured 2026-10-07 on
# the user's takes: cough -> "Cool.", humming -> "Hm", "Mm.", "Um", "Mm, mm, um"; quiet
# breath-like windows -> "Mm-mm.", "Mm-hmm.", "So"; earlier sherpa-onnx runs -> "Yeah.").
# Non-words in any number, or one of the English words alone; only the whole result matches.
_FILLER_WORD = r"(?:h+m+|m+(?:-?h?m+)*|u+[hm]+|u+h-?hu+h|e+h+|a+h+)"
_FILLER = re.compile(
    rf"[\s.,!?…-]*(?:{_FILLER_WORD}(?:[\s.,!?…-]+{_FILLER_WORD})*|yeah|cool|so)[\s.,!?…-]*",
    re.IGNORECASE,
)


def compile_patterns(patterns: Sequence[str]) -> list[re.Pattern[str]]:
    return [re.compile(p, re.IGNORECASE) for p in patterns]


def is_no_speech(
    segment: TranscriptSegment, no_speech_threshold: float, logprob_threshold: float
) -> bool:
    """Both conditions together: high no_speech_prob alone also occurs on real speech."""
    if segment.no_speech_prob is None or segment.avg_logprob is None:
        return False
    return segment.no_speech_prob > no_speech_threshold and segment.avg_logprob < logprob_threshold


def is_filler_only(text: str) -> bool:
    """True if the whole result is Parakeet's non-speech filler (06 §6.8, task 4.4)."""
    return _FILLER.fullmatch(text) is not None


def has_non_latin_letters(text: str) -> bool:
    """True if any letter is outside the Latin script (Cyrillic, Greek, ...; task 4.4).

    Polish letters are Latin. Digits and punctuation are not letters.
    """
    return any(c.isalpha() and not unicodedata.name(c, "").startswith("LATIN") for c in text)


def is_hallucination(text: str, patterns: Sequence[re.Pattern[str]]) -> bool:
    return any(p.search(text) for p in patterns)


def segment_rejections(
    segments: Sequence[TranscriptSegment],
    patterns: Sequence[re.Pattern[str]],
    no_speech_threshold: float,
    logprob_threshold: float,
    *,
    repetitions: bool = True,
) -> list[str | None]:
    """Rejection reason per segment (None = kept); `repetitions=False` skips the
    identical-segment rule."""
    reasons: list[str | None] = []
    previous: str | None = None
    for segment in segments:
        text = segment.text.strip()
        if is_no_speech(segment, no_speech_threshold, logprob_threshold):
            reasons.append("no_speech")
        elif is_hallucination(segment.text, patterns):
            reasons.append("hallucination")
        elif repetitions and text and text == previous:
            reasons.append("repeated_segment")
        else:
            reasons.append(None)
        previous = text
    return reasons


def collapse_loops(text: str) -> str:
    """Keeps one copy of an n-gram (n >= 3 words) repeated 4+ times in a row."""
    return _LOOP.sub(r"\1", text)


def is_prompt_echo(text: str, prompt_tail: str | None) -> bool:
    """Continuous mode: the result repeats the end of the session context sent as prompt.

    Skipped when there is no context (PTT): vocabulary_prompt is never an echo source.
    Compared after whitespace normalization, case-insensitively, at a word boundary.
    """
    if not prompt_tail:
        return False
    result = _normalize(text)
    tail = _normalize(prompt_tail)
    if not result or not tail.endswith(result):
        return False
    before = len(tail) - len(result)
    return before == 0 or tail[before - 1] == " "


def _normalize(text: str) -> str:
    return _WS.sub(" ", text).strip().casefold()
