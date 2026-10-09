"""Job queue and the single PipelineWorker thread (docs/04-state-machine.md §4.4-4.5).

v0.1 scope: PTT speech gate (VAD trimming when `vad.enabled`, brought forward from task 2.6;
otherwise the RMS gate), generations, one retry for 5xx/timeouts, pause on DOWN with a
startup_timeout_s limit, timing line. Continuous jobs (task 2.4) carry their session's context:
the end of the session's text goes into the prompt (06 §6.6) and the previous segment's cut
into TextContext for continuity (08 §8.2 step 5).
"""

import collections
import dataclasses
import functools
import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

import numpy as np
from numpy.typing import NDArray

from local_stt.cancellation import CancellationToken
from local_stt.config import Config
from local_stt.events import Event, JobDiscarded, JobFailed, JobFinished, JobStarted
from local_stt.history import TranscriptHistory
from local_stt.interfaces import (
    CancelResult,
    Cut,
    Injector,
    InjectResult,
    Job,
    SttEngine,
    TextContext,
    TextProcessor,
    Transcript,
)
from local_stt.stt.whisper_server import (
    EngineConnectionError,
    EngineError,
    EngineHttpError,
    EngineTimeoutError,
)
from local_stt.text import filters

SAMPLE_RATE = 16000
RMS_WINDOW_S = 0.1  # 05 §5.3: 1 s of speech in 20 s of silence must still pass
RETRY_DELAY_S = 1.0  # 04 §4.4: one retry for HTTP 5xx and timeouts
MIN_REQUEST_TIMEOUT_S = 10.0  # 06: max(10 s, 4 * audio * RTF), capped
RTF_HISTORY = 10
CONTEXT_CHARS = 200  # session text passed in the prompt (06 §6.6)
# A longer pause would start a new paragraph and drop the context. Off: it raised WER on a
# recording with 4-60 s pauses (task 3.4, docs/15); `bench --context --context-reset` retests it.
CONTEXT_RESET_S: float | None = None
RTF_TIMEOUT_FACTOR = 4.0

DiscardReason = Literal["no_speech", "filtered", "cancelled"]

log = logging.getLogger("local_stt.pipeline")
timings_log = logging.getLogger("local_stt.timings")


def has_speech_rms(audio: NDArray[np.float32], threshold_dbfs: float) -> bool:
    """True if any 100 ms window (the last one may be shorter) exceeds the threshold."""
    window = int(RMS_WINDOW_S * SAMPLE_RATE)
    threshold = 10 ** (threshold_dbfs / 20)
    for start in range(0, len(audio), window):
        chunk = audio[start : start + window].astype(np.float64)
        if math.sqrt(float(np.mean(chunk * chunk))) > threshold:
            return True
    return False


class SpeechTrimmer(Protocol):
    """`audio.vad.VadTrimmer`: the PTT gate when `active` (05 §5.3)."""

    @property
    def active(self) -> bool: ...

    def trim(self, audio: NDArray[np.float32]) -> NDArray[np.float32] | None: ...


@dataclass
class _Queued:
    job: Job
    enqueued_at: float


class _CancelledBeforeRetry(Exception):
    pass


@dataclass
class _Session:
    """What the pipeline remembers of the continuous session in progress (04 §4.4)."""

    session_id: int
    tail: str = ""  # last CONTEXT_CHARS characters of the session's text
    prev_cut: Cut | None = None  # cut of the session's previous segment
    language: str | None = None  # of the text in `tail` (task 3.7)


class PipelineWorker:
    """Implements `PipelineControl`; all methods except `run` are called from other threads."""

    def __init__(
        self,
        *,
        engine: SttEngine,
        processor: TextProcessor,
        injector: Injector,
        post: Callable[[Event], None],
        report_connection_failure: Callable[[], None],
        config: Config,
        trimmer: SpeechTrimmer | None = None,
        history: TranscriptHistory | None = None,
        clock: Callable[[], float] = time.monotonic,
        context_chars: int = CONTEXT_CHARS,
        context_reset_s: float | None = CONTEXT_RESET_S,
    ):
        self._engine = engine
        self._processor = processor
        self._injector = injector
        self._post = post
        self._report_connection_failure = report_connection_failure
        self._config = config
        self._trimmer = trimmer
        self._history = history
        self._clock = clock
        # Constructor parameters only so `bench --context` can compare policies (task 3.4).
        self._context_chars = context_chars
        self._context_reset_s = context_reset_s
        # One lock for the queue, generation and the injector's operation marker (08 §8.3).
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._queue: collections.deque[_Queued] = collections.deque()
        self._generation = 0
        self._token = CancellationToken(self._lock)
        self._current: Job | None = None
        self._paused_since: float | None = None
        self._stopping = False
        self._rtf: collections.deque[float] = collections.deque(maxlen=RTF_HISTORY)
        self._session: _Session | None = None  # pipeline thread only
        self._thread: threading.Thread | None = None

    # --- PipelineControl (called by the Controller) -------------------------------------

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def submit(self, job: Job) -> None:
        with self._cond:
            self._queue.append(_Queued(job, self._clock()))
            self._cond.notify()

    def reinject(self, job_id: int, text: str) -> None:
        """Queues `text` from the history for injection only (`local-stt last`, task 5.2)."""
        job = Job(
            id=job_id,
            source="history",
            audio=np.zeros(0, dtype=np.float32),
            ended_at=self._clock(),
            generation=self.generation,
            session_id=None,
            seq=None,
            cut="release",
            language="",
            text=text,
        )
        self.submit(job)

    def cancel_all(self) -> CancelResult:
        with self._cond:
            self._generation += 1
            injection_in_flight = self._token.cancel_locked()
            self._token = CancellationToken(self._lock)
            drained = tuple(q.job.id for q in self._queue)
            self._queue.clear()
            self._cond.notify()
            # The in-flight job reports JobDiscarded(cancelled), or JobFinished if an input
            # operation that had already started enters the whole text (08 §8.3).
            return CancelResult(drained, self._current is not None, injection_in_flight)

    def pause(self) -> None:
        with self._cond:
            if self._paused_since is None:
                self._paused_since = self._clock()
            self._cond.notify()

    def resume(self) -> None:
        with self._cond:
            self._paused_since = None
            self._cond.notify()

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._paused_since is not None

    # --- lifecycle and reload -----------------------------------------------------------

    def update_config(self, config: Config) -> None:
        """Live reload (04 §4.6); the job in flight keeps the values it started with."""
        with self._cond:
            self._config = config
            self._cond.notify()  # startup_timeout_s may have changed

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, name="pipeline", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stops after the current job; queued jobs are dropped without events."""
        with self._cond:
            self._stopping = True
            self._token.cancel_locked()  # interrupts the retry delay and injector waits
            self._cond.notify()
        if self._thread is not None:
            self._thread.join()

    # --- worker thread -------------------------------------------------------------------

    def run(self) -> None:
        while (item := self._next()) is not None:
            queued, token = item
            self._process(queued, token)

    def _next(self) -> tuple[_Queued, CancellationToken] | None:
        """Blocks until a job may run; fails the queue if paused past startup_timeout_s."""
        while True:
            expired: list[_Queued] = []
            with self._cond:
                if self._stopping:
                    return None
                if self._paused_since is None:
                    if self._queue:
                        queued = self._queue.popleft()
                        self._current = queued.job
                        return queued, self._token
                    self._cond.wait()
                    continue
                deadline = self._paused_since + self._config.stt.startup_timeout_s
                remaining = deadline - self._clock()
                if remaining > 0 or not self._queue:
                    self._cond.wait(remaining if remaining > 0 else None)
                    continue
                expired = list(self._queue)
                self._queue.clear()
            # 04 §4.5: the engine did not return in time; the Controller aggregates these.
            for q in expired:
                self._post(
                    JobFailed(q.job.id, q.job.source, q.job.duration_s, "STT engine unavailable")
                )

    def _process(self, queued: _Queued, token: CancellationToken) -> None:
        job = queued.job
        session = self._session_of(job)
        requeued = False
        text: str | None = None
        started = self._clock()
        try:
            self._post(JobStarted(job.id))
            if job.text is not None:
                return self._reinsert(job, job.text, token)
            config = self._config
            if job.source == "ptt":
                speech = self._ptt_speech(job.audio, config)
                if speech is None:
                    return self._discard(job, "no_speech")
                job = dataclasses.replace(job, audio=speech)  # requeue keeps the original
            if self._stale(job):
                return self._discard(job, "cancelled")

            prompt_tail = (
                (session.tail or None) if session and config.stt.continuous_context else None
            )
            try:
                transcript = self._transcribe(job, token, config, prompt_tail)
            except EngineConnectionError as e:
                requeued = True
                return self._requeue(queued, e)
            except _CancelledBeforeRetry:
                return self._discard(job, "cancelled")
            except EngineError as e:
                log.error("job %d: transcription failed: %s", job.id, e)
                return self._post(JobFailed(job.id, job.source, job.duration_s, str(e)))
            if self._stale(job):
                return self._discard(job, "cancelled")

            text_started = self._clock()
            prev_cut = session.prev_cut if session else None
            ctx = TextContext(job.source, job.session_id, job.seq, job.cut, prev_cut, prompt_tail)
            text = self._processor.process(transcript, ctx)
            text_s = self._clock() - text_started
            if text is None:
                return self._discard(job, "filtered")
            if self._history is not None:  # before injection: a failed paste stays here (5.2)
                self._history.add(text)
            if config.logging.log_text:
                log.debug('text job=%d: "%s"', job.id, text)
            # Counted, not changed: the count is the evidence for a fallback engine (task 4.4,
            # backlog item 10). The text itself is logged only with logging.log_text (12).
            non_latin = filters.has_non_latin_letters(text)
            if non_latin:
                log.warning(
                    "job %d: non-Latin letters in the output (%s), injected unchanged%s",
                    job.id,
                    transcript.engine,
                    f': "{text.strip()}"' if config.logging.log_text else "",
                )
            if self._stale(job):
                return self._discard(job, "cancelled")

            inject_started = self._clock()
            result = self._injector.inject(text, cancel=token)
            done = self._clock()
            if result.cancelled:
                text = None  # not entered: it must not become context
                return self._discard(job, "cancelled")
            timings = {
                "audio": job.duration_s,
                "queued": started - queued.enqueued_at,
                "stt": transcript.processing_s,
                "text": text_s,
                "inject": done - inject_started,
                "total": done - job.ended_at,
            }
            if config.logging.timings:
                timings_log.info(_timing_line(job, transcript, timings, result))
            self._post(JobFinished(job.id, job.source, result, timings, non_latin))
        finally:
            with self._lock:
                self._current = None
            if session is not None and not requeued:
                session.prev_cut = job.cut
                if text and self._context_chars > 0:
                    tail = (session.tail + " " + text.strip()).strip()
                    session.tail = tail[-self._context_chars :].lstrip()

    def _reinsert(self, job: Job, text: str, token: CancellationToken) -> None:
        """A history text: no engine, no text processing, not added to the history again."""
        if self._stale(job):
            return self._discard(job, "cancelled")
        inject_started = self._clock()
        result = self._injector.inject(text, cancel=token)
        if result.cancelled:
            return self._discard(job, "cancelled")
        log.info("job %d: inserted again from the history (%d chars)", job.id, result.chars)
        timings = {"inject": self._clock() - inject_started}
        self._post(JobFinished(job.id, job.source, result, timings))

    def _session_of(self, job: Job) -> _Session | None:
        """The continuous session of `job`; a new session replaces the previous one."""
        if job.source != "continuous" or job.session_id is None:
            return None
        if self._session is None or self._session.session_id != job.session_id:
            self._session = _Session(job.session_id)
        if self._session.language != job.language:
            # A language switch starts a new paragraph: context in the other language would
            # only mislead the prompt (task 3.7).
            self._session.tail = ""
            self._session.language = job.language
        reset_s = self._context_reset_s
        if reset_s is not None and job.pause_before_s is not None and job.pause_before_s > reset_s:
            log.debug("job %d: %.1f s pause, new paragraph", job.id, job.pause_before_s)
            self._session.tail = ""
        return self._session

    def _ptt_speech(self, audio: NDArray[np.float32], config: Config) -> NDArray[np.float32] | None:
        """05 §5.3: the speech to transcribe, or None for no speech."""
        if self._trimmer is not None and self._trimmer.active:
            return self._trimmer.trim(audio)
        return audio if has_speech_rms(audio, config.ptt.silence_rms_dbfs) else None

    def _transcribe(
        self, job: Job, token: CancellationToken, config: Config, prompt_tail: str | None
    ) -> Transcript:
        """One retry after 1 s for HTTP 5xx and timeouts; raises `_CancelledBeforeRetry` if
        cancelled before the retry. Connection errors and other failures propagate at once."""
        # 06 §6.6: the vocabulary, then the session's context (continuous only).
        prompt = " ".join(p for p in (config.stt.vocabulary_prompt, prompt_tail) if p) or None
        timeout_s = self._request_timeout(job.duration_s, config)
        request = functools.partial(
            self._engine.transcribe,
            job.audio,
            sample_rate=SAMPLE_RATE,
            language=job.language,
            prompt=prompt,
            timeout_s=timeout_s,
        )
        try:
            transcript = request()
        except (EngineHttpError, EngineTimeoutError) as e:
            if isinstance(e, EngineHttpError) and e.status < 500:
                raise
            log.warning("job %d: %s; retrying in %.0f s", job.id, e, RETRY_DELAY_S)
            if token.wait(RETRY_DELAY_S) or self._stale(job):
                raise _CancelledBeforeRetry from e
            transcript = request()
        if transcript.audio_duration_s > 0:
            self._rtf.append(transcript.processing_s / transcript.audio_duration_s)
        return transcript

    def _request_timeout(self, duration_s: float, config: Config) -> float:
        cap = config.stt.request_timeout_max_s
        if not self._rtf:
            return cap  # no history yet: the first request after start may be slow
        rtf = sum(self._rtf) / len(self._rtf)
        return min(cap, max(MIN_REQUEST_TIMEOUT_S, RTF_TIMEOUT_FACTOR * duration_s * rtf))

    def _requeue(self, queued: _Queued, error: EngineConnectionError) -> None:
        """04 §4.4: report the failure, pause, and retry this job first after READY."""
        log.error("job %d: engine unreachable, pausing the queue: %s", queued.job.id, error)
        with self._cond:
            stale = queued.job.generation != self._generation
            if not stale:
                self._queue.appendleft(queued)
            self._current = None  # queued again, not in flight (cancel_all reports it drained)
            if self._paused_since is None:
                self._paused_since = self._clock()
        if stale:
            self._discard(queued.job, "cancelled")
        self._report_connection_failure()

    def _stale(self, job: Job) -> bool:
        with self._lock:
            return job.generation != self._generation

    def _discard(self, job: Job, reason: DiscardReason) -> None:
        log.debug("job %d discarded: %s", job.id, reason)
        self._post(JobDiscarded(job.id, job.source, reason))


def _timing_line(
    job: Job, transcript: Transcript, t: dict[str, float], result: InjectResult
) -> str:
    """12 §12.1 job timing line; never contains text."""
    rtf = transcript.processing_s / job.duration_s if job.duration_s > 0 else 0.0
    if result.left_in_clipboard:
        outcome = "clipboard"
    elif not result.ok:
        outcome = "failed"
    else:
        outcome = "injected"
    seq = f" seq={job.seq}" if job.seq is not None else ""
    return (
        f"job={job.id} src={job.source}{seq} cut={job.cut} audio={t['audio']:.2f}s "
        f"queued={t['queued']:.2f}s "
        f"stt={t['stt']:.2f}s rtf={rtf:.2f} text={t['text'] * 1000:.0f}ms "
        f"inject={t['inject'] * 1000:.0f}ms total={t['total']:.2f}s chars={result.chars} "
        f"backend={result.backend} result={outcome}"
    )
