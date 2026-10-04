"""TextProcessor: Transcript -> text to inject (docs/08-text-injection.md §8.2).

Steps 1-7; step 5 (continuous-mode continuity) since task 2.4.
"""

import logging
import re

from local_stt.config import Config, Replacement
from local_stt.interfaces import Cut, TextContext, Transcript, TranscriptSegment
from local_stt.text import filters

log = logging.getLogger("local_stt.text")

_SPACES = re.compile(r"[ \t]+")
_NEWLINES = re.compile(r"\r\n|\r|\n")


def assemble(segments: list[TranscriptSegment], rejected: list[str | None]) -> str:
    """Step 2: join kept runs without strip(); one space where removed segments were."""
    parts: list[str] = []
    gap = False
    for segment, reason in zip(segments, rejected, strict=True):
        if reason is not None:
            gap = True
            continue
        if gap and parts:
            parts.append(" ")
        gap = False
        parts.append(segment.text)
    return "".join(parts)


def normalize_whitespace(text: str) -> str:
    """Step 3: Whisper line breaks -> spaces, runs of spaces/tabs -> one space, strip."""
    return _SPACES.sub(" ", _NEWLINES.sub(" ", text)).strip()


def apply_replacements(text: str, replacements: tuple[Replacement, ...]) -> str:
    """Step 4: user replacements in order (regex or literal, case-sensitive)."""
    for rule in replacements:
        if rule.regex:
            text = re.sub(rule.pattern, rule.replace, text)
        else:
            text = text.replace(rule.pattern, rule.replace)
    return text


def continuity(text: str, cut: Cut, prev_cut: Cut | None) -> str:
    """Step 5: a cut in the middle of an utterance loses its final period; after one, the
    next segment starts lowercase when its second word is not capitalized (so not a name)."""
    if cut in ("max_length", "max_duration") and text.endswith(".") and not text.endswith(".."):
        text = text[:-1]
    if prev_cut == "max_length" and text[:1].isupper():
        words = text.split()
        if len(words) >= 2 and not words[1][:1].isupper():  # one word: may be a name
            text = text[0].lower() + text[1:]
    return text


class DefaultTextProcessor:
    """Implements `interfaces.TextProcessor`; used only by the pipeline thread."""

    def __init__(self, config: Config):
        self.update_config(config)

    def update_config(self, config: Config) -> None:
        """Live reload (04 §4.6); called between jobs."""
        self._config = config
        self._patterns = filters.compile_patterns(config.text.hallucination_patterns)

    def process(self, transcript: Transcript, ctx: TextContext) -> str | None:
        stt, text_cfg = self._config.stt, self._config.text
        segments = transcript.segments
        thresholds = (stt.no_speech_threshold, stt.logprob_threshold)
        # Loops first: dropping identical 60-character segments of a loop would leave too
        # few copies to detect it. The identical-segment rule applies when there is no loop.
        rejected = filters.segment_rejections(
            segments, self._patterns, *thresholds, repetitions=False
        )
        assembled = assemble(segments, rejected)
        collapsed = filters.collapse_loops(assembled)
        if collapsed != assembled:
            reasons = [*rejected, "loop"]
        else:
            rejected = filters.segment_rejections(segments, self._patterns, *thresholds)
            collapsed = assemble(segments, rejected)
            reasons = rejected
        for reason in reasons:
            if reason is not None:
                log.debug("filtered: %s", reason)
        text = normalize_whitespace(collapsed)
        if filters.is_prompt_echo(text, ctx.prompt_tail):
            log.debug("filtered: prompt_echo")
            return None

        text = apply_replacements(text, text_cfg.replacements)
        text = continuity(text, ctx.cut, ctx.prev_cut)
        if not text:
            return None
        return text + " " if text_cfg.append_space else text
