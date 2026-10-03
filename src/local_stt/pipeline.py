"""Job queue and the single PipelineWorker thread (docs/04-state-machine.md §4.4-4.5).

v0.1 scope: PTT RMS gate, generations, one retry for 5xx/timeouts, pause on DOWN with a
startup_timeout_s limit, timing line. Per-session prompt context and VAD trimming arrive in
v0.2 (tasks 2.4, 2.6).
"""

import collections
import functools
import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import numpy as np
from numpy.typing import NDArray

from local_stt.cancellation import CancellationToken
from local_stt.config import Config
from local_stt.events import Event, JobDiscarded, JobFailed, JobFinished, JobStarted
from local_stt.interfaces import (
    CancelResult,
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

SAMPLE_RATE = 16000
RMS_WINDOW_S = 0.1  # 05 §5.3: 1 s of speech in 20 s of silence must still pass
RETRY_DELAY_S = 1.0  # 04 §4.4: one retry for HTTP 5xx and timeouts
MIN_REQUEST_TIMEOUT_S = 10.0  # 06: max(10 s, 4 * audio * RTF), capped
RTF_HISTORY = 10
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


@dataclass
class _Queued:
    job: Job
    enqueued_at: float


class _CancelledBeforeRetry(Exception):
    pass


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
        clock: Callable[[], float] = time.monotonic,
    ):
        self._engine = engine
        self._processor = processor
        self._injector = injector
        self._post = post
        self._report_connection_failure = report_connection_failure
        self._config = config
        self._clock = clock
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
        started = self._clock()
        try:
            self._post(JobStarted(job.id))
            config = self._config
            if job.source == "ptt" and not has_speech_rms(job.audio, config.ptt.silence_rms_dbfs):
                return self._discard(job, "no_speech")
            if self._stale(job):
                return self._discard(job, "cancelled")

            try:
                transcript = self._transcribe(job, token, config)
            except EngineConnectionError as e:
                return self._requeue(queued, e)
            except _CancelledBeforeRetry:
                return self._discard(job, "cancelled")
            except EngineError as e:
                log.error("job %d: transcription failed: %s", job.id, e)
                return self._post(JobFailed(job.id, job.source, job.duration_s, str(e)))
            if self._stale(job):
                return self._discard(job, "cancelled")

            text_started = self._clock()
            ctx = TextContext(job.source, job.session_id, job.seq, job.cut, None, None)
            text = self._processor.process(transcript, ctx)
            text_s = self._clock() - text_started
            if text is None:
                return self._discard(job, "filtered")
            if config.logging.log_text:
                log.debug('text job=%d: "%s"', job.id, text)
            if self._stale(job):
                return self._discard(job, "cancelled")

            inject_started = self._clock()
            result = self._injector.inject(text, cancel=token)
            done = self._clock()
            if result.cancelled:
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
            self._post(JobFinished(job.id, job.source, result, timings))
        finally:
            with self._lock:
                self._current = None

    def _transcribe(self, job: Job, token: CancellationToken, config: Config) -> Transcript:
        """One retry after 1 s for HTTP 5xx and timeouts; raises `_CancelledBeforeRetry` if
        cancelled before the retry. Connection errors and other failures propagate at once."""
        prompt = config.stt.vocabulary_prompt or None  # PTT: vocabulary only (06 §6.6)
        timeout_s = self._request_timeout(job.duration_s, config)
        request = functools.partial(
            self._engine.transcribe,
            job.audio,
            sample_rate=SAMPLE_RATE,
            language=config.stt.language,
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
        f"job={job.id} src={job.source}{seq} audio={t['audio']:.2f}s queued={t['queued']:.2f}s "
        f"stt={t['stt']:.2f}s rtf={rtf:.2f} text={t['text'] * 1000:.0f}ms "
        f"inject={t['inject'] * 1000:.0f}ms total={t['total']:.2f}s chars={result.chars} "
        f"backend={result.backend} result={outcome}"
    )
