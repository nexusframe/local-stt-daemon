"""`bench --context`: continuous-mode context policies on a long recording (task 3.4).

The recording is cut once, offline, by the daemon's Segmenter with its own Silero session (the
cut is deterministic, so it matches a real-time soak run of the same file). Every policy then
sends the same segments, in order, through the real PipelineWorker and TextProcessor to a fresh
temporary `whisper-server`; only the injector is replaced by one that records the text. A
policy is the length of the session text sent in the prompt (`context_chars`, 0 = none) and the
pause after which that text is dropped as a new paragraph (`context_reset_s`, None = never).

Metrics against the reference text: WER after the 13 §13.3 normalization, and *raw* WER, where
words keep their case and the punctuation attached to them ("Kota." != "kota"), because the
context mainly carries punctuation and capitalization across segments. Tokens without a letter
or digit (a lone dash) are left out of the raw comparison on both sides.
"""

import itertools
import json
import logging
import re
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from local_stt import events as ev
from local_stt.audio.capture import FRAME_SAMPLES, SAMPLE_RATE, AudioFrame
from local_stt.audio.segmenter import Segmenter
from local_stt.audio.vad import SileroVad
from local_stt.audio.wav import wav_bytes_to_float32
from local_stt.bench.runner import concurrency_error, system_info
from local_stt.bench.wer import edit_distance, error_counts
from local_stt.cancellation import CancellationToken
from local_stt.config import Config
from local_stt.interfaces import AudioSegment, InjectResult, Job, SttEngine
from local_stt.pipeline import PipelineWorker
from local_stt.stt import whisper_server as ws
from local_stt.text.processor import DefaultTextProcessor

log = logging.getLogger("local_stt.bench")

FRAME_S = FRAME_SAMPLES / SAMPLE_RATE
JOB_TIMEOUT_S = 300.0
_WORD = re.compile(r"\w")


@dataclass(frozen=True)
class Policy:
    context_chars: int
    context_reset_s: float | None

    @property
    def label(self) -> str:
        reset = "off" if self.context_reset_s is None else f"{self.context_reset_s:g}s"
        return f"chars={self.context_chars} reset={reset}"


def policies(chars: Sequence[int], resets: Sequence[float | None]) -> list[Policy]:
    return [Policy(c, r) for c, r in itertools.product(chars, resets)]


def raw_words(text: str) -> list[str]:
    return [w for w in text.split() if _WORD.search(w)]


def raw_wer(reference: str, hypothesis: str) -> tuple[int, int]:
    """(word errors, reference words) without normalization."""
    ref = raw_words(reference)
    return edit_distance(ref, raw_words(hypothesis)), len(ref)


def segment_audio(audio: NDArray[np.float32], config: Config) -> list[AudioSegment]:
    """Cuts the recording the way continuous mode does; stream time starts at 0."""
    segmenter = Segmenter(config.vad, SileroVad(config.vad_model_path))
    segmenter.reset(session_id=1)
    padded = np.concatenate([audio, np.zeros(-len(audio) % FRAME_SAMPLES, dtype=np.float32)])
    out: list[Any] = []
    for i in range(len(padded) // FRAME_SAMPLES):
        samples = padded[i * FRAME_SAMPLES : (i + 1) * FRAME_SAMPLES]
        out += segmenter.add(AudioFrame(1, 1, i * FRAME_S, samples))
    out += segmenter.flush(at=len(padded) / SAMPLE_RATE)
    return [o for o in out if isinstance(o, AudioSegment)]


class _RecordingInjector:
    def __init__(self) -> None:
        self.last: str | None = None

    def inject(self, text: str, *, cancel: CancellationToken) -> InjectResult:
        self.last = text
        return InjectResult(True, "none", len(text), None, False, None)


def transcribe_policy(
    engine: SttEngine, segments: Sequence[AudioSegment], config: Config, policy: Policy
) -> list[str | None]:
    """The text entered for each segment (None: filtered or failed), in order."""
    injector = _RecordingInjector()
    texts: dict[int, str | None] = {}
    done = threading.Event()

    def post(event: ev.Event) -> None:
        if isinstance(event, ev.JobFinished):
            texts[event.job_id] = injector.last
        elif isinstance(event, ev.JobDiscarded | ev.JobFailed):
            if isinstance(event, ev.JobFailed):
                log.warning("job %d failed: %s", event.job_id, event.error)
            texts[event.job_id] = None
        else:
            return
        if len(texts) == len(segments):
            done.set()

    pipeline = PipelineWorker(
        engine=engine,
        processor=DefaultTextProcessor(config),
        injector=injector,
        post=post,
        report_connection_failure=lambda: None,
        config=config,
        context_chars=policy.context_chars,
        context_reset_s=policy.context_reset_s,
    )
    pipeline.start()
    try:
        for i, s in enumerate(segments, start=1):
            pipeline.submit(
                Job(
                    id=i,
                    source="continuous",
                    audio=s.samples,
                    ended_at=s.ended_at,
                    generation=0,
                    session_id=s.session_id,
                    seq=s.seq,
                    cut=s.cut,
                    language=config.stt.languages[0],
                    pause_before_s=s.pause_before_s,
                )
            )
        if segments and not done.wait(JOB_TIMEOUT_S * len(segments)):
            raise RuntimeError("the pipeline did not finish the segments")
    finally:
        pipeline.stop()
    return [texts.get(i) for i in range(1, len(segments) + 1)]


def score(reference: str, texts: Sequence[str | None]) -> dict[str, Any]:
    hypothesis = " ".join(t.strip() for t in texts if t)
    counts = error_counts(reference, hypothesis)
    raw_errors, raw_ref = raw_wer(reference, hypothesis)
    return {
        "wer": counts.wer,
        "word_errors": counts.word_errors,
        "ref_words": counts.ref_words,
        "raw_wer": raw_errors / raw_ref if raw_ref else 0.0,
        "raw_word_errors": raw_errors,
        "raw_ref_words": raw_ref,
        "filtered": sum(t is None for t in texts),
    }


def run_context(
    long_wav: Path,
    reference_path: Path,
    out_dir: Path,
    policies_: Sequence[Policy],
    *,
    allow_concurrent: bool = False,
    on_progress: Callable[[str], None] = print,
) -> dict[str, Any]:
    error = None if allow_concurrent else concurrency_error()
    if error:
        raise RuntimeError(error)
    audio, rate = wav_bytes_to_float32(long_wav.read_bytes())
    if rate != SAMPLE_RATE:
        raise ValueError(f"{long_wav}: {rate} Hz, expected {SAMPLE_RATE} Hz")
    reference = reference_path.read_text(encoding="utf-8")
    base = Config()
    config = replace(base, logging=replace(base.logging, timings=False))
    stt = config.stt
    segments = segment_audio(audio, config)
    pauses = [s.pause_before_s for s in segments if s.pause_before_s]
    on_progress(
        f"{len(segments)} segments, longest pause {max(pauses, default=0):.1f} s; "
        f"{len(policies_)} policies"
    )
    results = []
    for policy in policies_:
        server = ws.TemporaryWhisperServer(
            stt.model_path, model=stt.model, threads=stt.threads, audio_ctx=stt.audio_ctx
        )
        server.start()  # a fresh server per policy: no request history carried over (06 §6.7)
        assert server.engine is not None
        try:
            texts = transcribe_policy(server.engine, segments, config, policy)
        finally:
            server.stop()
        result = {"policy": policy.label, **score(reference, texts)}
        on_progress(
            f"{policy.label}: WER {result['wer']:.2%}, raw WER {result['raw_wer']:.2%}, "
            f"filtered {result['filtered']}"
        )
        result["segments"] = [
            {"seq": s.seq, "cut": s.cut, "pause_before_s": s.pause_before_s, "text": t}
            for s, t in zip(segments, texts, strict=True)
        ]
        results.append(result)

    info = system_info(long_wav.parent.parent)
    keys = ("timestamp", "cpu", "governor", "platform_profile", "power_source")
    document = {
        "config": {
            "model": stt.model,
            "threads": stt.threads,
            "audio_ctx": stt.audio_ctx,
            "vocabulary_prompt": stt.vocabulary_prompt,
            "long_wav": str(long_wav),
            "reference": str(reference_path),
            "file_s": len(audio) / SAMPLE_RATE,
            "segments": len(segments),
            "by_cut": {c: sum(s.cut == c for s in segments) for c in {s.cut for s in segments}},
        },
        "system": {k: info[k] for k in keys},
        "results": results,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"context-{long_wav.stem}.json"
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    document["path"] = str(path)
    return document
