"""WER/CER for Polish transcripts (docs/13-benchmark.md §13.3), without dependencies.

Normalization: lowercase, remove punctuation, collapse whitespace. Polish letters are kept (losing
a diacritic is an error). Numbers are not normalized: `2024` vs "dwa tysiące dwadzieścia cztery"
counts as word errors and is additionally flagged by `numeric_mismatch`.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

# 13.3 list plus the typographic quotes that occur in FLEURS and Wolne Lektury texts.
_PUNCTUATION = ".,;:!?…„”\"'()-–—“‘’«»"  # noqa: RUF001
_PUNCT_RE = re.compile("[" + re.escape(_PUNCTUATION) + "]")
_DIGITS_RE = re.compile(r"\d+")


def normalize(text: str) -> str:
    return " ".join(_PUNCT_RE.sub(" ", text.lower()).split())


def edit_distance(ref: Sequence[str], hyp: Sequence[str]) -> int:
    """Levenshtein distance (substitutions, insertions, deletions all cost 1)."""
    previous = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, start=1):
        current = [i]
        for j, h in enumerate(hyp, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (r != h)))
        previous = current
    return previous[-1]


@dataclass(frozen=True)
class ErrorCounts:
    word_errors: int
    ref_words: int
    char_errors: int
    ref_chars: int

    @property
    def wer(self) -> float:
        return self.word_errors / self.ref_words if self.ref_words else 0.0

    @property
    def cer(self) -> float:
        return self.char_errors / self.ref_chars if self.ref_chars else 0.0


def error_counts(reference: str, hypothesis: str) -> ErrorCounts:
    """Word and character edit counts after normalization; CER includes single spaces."""
    ref, hyp = normalize(reference), normalize(hypothesis)
    ref_words, hyp_words = ref.split(), hyp.split()
    return ErrorCounts(
        word_errors=edit_distance(ref_words, hyp_words),
        ref_words=len(ref_words),
        char_errors=edit_distance(ref, hyp),
        ref_chars=len(ref),
    )


def corpus_error_rates(counts: Sequence[ErrorCounts]) -> tuple[float, float]:
    """Corpus-level (WER, CER): total edits divided by total reference length."""
    words = sum(c.ref_words for c in counts)
    chars = sum(c.ref_chars for c in counts)
    wer = sum(c.word_errors for c in counts) / words if words else 0.0
    cer = sum(c.char_errors for c in counts) / chars if chars else 0.0
    return wer, cer


def numeric_mismatch(reference: str, hypothesis: str) -> bool:
    """True if the digit sequences differ, e.g. `2024` written out as words by the model."""
    return _DIGITS_RE.findall(reference) != _DIGITS_RE.findall(hypothesis)
