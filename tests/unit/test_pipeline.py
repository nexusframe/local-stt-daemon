import dataclasses
import logging
import queue
import threading
from collections.abc import Callable, Iterator
from typing import Any

import numpy as np
import pytest
from numpy.typing import NDArray

from local_stt import pipeline as pipeline_mod
from local_stt.cancellation import CancellationToken, Cancelled
from local_stt.config import Config
from local_stt.events import Event, JobDiscarded, JobFailed, JobFinished, JobStarted
from local_stt.interfaces import (
    Cut,
    InjectResult,
    Job,
    TextContext,
    Transcript,
    TranscriptSegment,
)
from local_stt.pipeline import CONTEXT_RESET_S, PipelineWorker, has_speech_rms
from local_stt.stt.whisper_server import (
    EngineConnectionError,
    EngineHttpError,
    EngineResponseError,
    EngineTimeoutError,
)

SR = 16000
WAIT_S = 5.0


def speech(seconds: float = 1.0, amplitude: float = 0.1) -> NDArray[np.float32]:
    t = np.arange(int(seconds * SR)) / SR
    return (amplitude * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def silence(seconds: float) -> NDArray[np.float32]:
    return np.zeros(int(seconds * SR), dtype=np.float32)


def transcript(
    text: str = " Ala ma kota.", audio_s: float = 1.0, proc_s: float = 0.5
) -> Transcript:
    segment = TranscriptSegment(text, 0.0, audio_s, 0.01, -0.2)
    return Transcript(text, [segment], audio_s, proc_s, "whisper.cpp", "small-q8_0")


class FakeEngine:
    """Returns queued outcomes (a Transcript or an exception) in order; may block on a gate."""

    name = "fake"

    def __init__(self) -> None:
        self.outcomes: list[Transcript | Exception] = []
        self.calls: list[dict[str, Any]] = []
        self.gate: threading.Event | None = None
        self.entered = threading.Event()

    def health(self) -> Any:
        raise NotImplementedError

    def transcribe(
        self,
        audio: NDArray[np.float32],
        *,
        sample_rate: int,
        language: str,
        prompt: str | None,
        timeout_s: float,
    ) -> Transcript:
        self.calls.append(
            {"samples": len(audio), "language": language, "prompt": prompt, "timeout_s": timeout_s}
        )
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(WAIT_S)
        outcome = self.outcomes.pop(0) if self.outcomes else transcript(audio_s=len(audio) / SR)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeProcessor:
    def __init__(self) -> None:
        self.contexts: list[TextContext] = []
        self.result: Callable[[Transcript], str | None] = lambda t: t.text.strip() + " "
        self.hook: Callable[[], None] = lambda: None

    def process(self, transcript: Transcript, ctx: TextContext) -> str | None:
        self.contexts.append(ctx)
        self.hook()
        return self.result(transcript)


class RecordingInjector:
    """Records texts; optionally waits for cancellation like a real injector waiting for
    modifiers, or holds an input operation open until released."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.wait_for_cancel = False
        self.hold_operation: threading.Event | None = None
        self.in_operation = threading.Event()
        self.waiting = threading.Event()

    def inject(self, text: str, *, cancel: CancellationToken) -> InjectResult:
        self.waiting.set()
        if self.wait_for_cancel and cancel.wait(WAIT_S):
            return InjectResult(False, "clipboard", 0, "gedit", False, None, cancelled=True)
        try:
            with cancel.operation():
                self.in_operation.set()
                if self.hold_operation is not None:
                    self.hold_operation.wait(WAIT_S)
                self.texts.append(text)
        except Cancelled:
            return InjectResult(False, "clipboard", 0, "gedit", False, None, cancelled=True)
        return InjectResult(True, "clipboard", len(text), "gedit", False, None)


class FakeTrimmer:
    """VadTrimmer stand-in: keeps the middle half, or reports no speech."""

    def __init__(self, active: bool = True) -> None:
        self.active = active
        self.speech = True
        self.calls: list[int] = []

    def trim(self, audio: NDArray[np.float32]) -> NDArray[np.float32] | None:
        self.calls.append(len(audio))
        quarter = len(audio) // 4
        return audio[quarter : len(audio) - quarter] if self.speech else None


class Harness:
    def __init__(self, config: Config, trimmer: FakeTrimmer | None = None, **context: Any) -> None:
        self.events: queue.SimpleQueue[Event] = queue.SimpleQueue()
        self.engine = FakeEngine()
        self.processor = FakeProcessor()
        self.injector = RecordingInjector()
        self.connection_failures = 0
        self.worker = PipelineWorker(
            engine=self.engine,
            processor=self.processor,
            injector=self.injector,
            post=self.events.put,
            report_connection_failure=self._report,
            config=config,
            trimmer=trimmer,
            **context,
        )
        self._next_id = 0

    def _report(self) -> None:
        self.connection_failures += 1

    def job(
        self,
        audio: NDArray[np.float32] | None = None,
        *,
        ended_at: float = 0.0,
        language: str = "pl",
    ) -> Job:
        self._next_id += 1
        return Job(
            id=self._next_id,
            source="ptt",
            audio=speech() if audio is None else audio,
            ended_at=ended_at,
            generation=self.worker.generation,
            session_id=None,
            seq=None,
            cut="release",
            language=language,
        )

    def submit(self, audio: NDArray[np.float32] | None = None) -> Job:
        job = self.job(audio)
        self.worker.submit(job)
        return job

    def next_event(self) -> Event:
        return self.events.get(timeout=WAIT_S)

    def outcome(self) -> Event:
        """Next event that is not JobStarted."""
        while isinstance(event := self.next_event(), JobStarted):
            pass
        return event

    def no_more_events(self) -> bool:
        try:
            self.events.get(timeout=0.1)
        except queue.Empty:
            return True
        return False


def config(**sections: dict[str, Any]) -> Config:
    base = Config()
    return dataclasses.replace(
        base,
        **{
            name: dataclasses.replace(getattr(base, name), **values)
            for name, values in sections.items()
        },
    )


@pytest.fixture
def h() -> Iterator[Harness]:
    harness = Harness(config())
    harness.worker.start()
    yield harness
    if harness.engine.gate is not None:
        harness.engine.gate.set()
    harness.worker.stop()


@pytest.fixture(autouse=True)
def fast_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline_mod, "RETRY_DELAY_S", 0.01)


# --- RMS gate (05 §5.3) ------------------------------------------------------------------


def test_rms_gate_detects_short_speech_in_long_silence() -> None:
    audio = np.concatenate([silence(10), speech(1.0, amplitude=0.01), silence(10)])
    assert has_speech_rms(audio, -50)


def test_rms_gate_rejects_silence_noise_and_empty_audio() -> None:
    rng = np.random.default_rng(0)
    noise = (rng.standard_normal(SR * 5) * 0.001).astype(np.float32)  # about -60 dBFS
    assert not has_speech_rms(noise, -50)
    assert not has_speech_rms(silence(3), -50)
    assert not has_speech_rms(np.zeros(0, dtype=np.float32), -50)


def test_rms_gate_checks_last_short_window() -> None:
    audio = np.concatenate([silence(1.0), speech(0.05)])  # a 50 ms trailing window
    assert has_speech_rms(audio, -50)


# --- happy path ---------------------------------------------------------------------------


def test_job_is_transcribed_processed_and_injected(
    h: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="local_stt.timings")
    job = h.submit()
    assert h.next_event() == JobStarted(job.id)
    event = h.next_event()
    assert isinstance(event, JobFinished)
    assert (event.job_id, event.source, event.result.ok) == (job.id, "ptt", True)
    assert set(event.timings) == {"audio", "queued", "stt", "text", "inject", "total"}
    assert h.injector.texts == ["Ala ma kota. "]
    assert h.engine.calls == [{"samples": SR, "language": "pl", "prompt": None, "timeout_s": 120.0}]
    assert h.processor.contexts == [TextContext("ptt", None, None, "release", None, None)]
    (line,) = [r.getMessage() for r in caplog.records if r.name == "local_stt.timings"]
    assert line.startswith(f"job={job.id} src=ptt cut=release audio=1.00s queued=")
    assert "stt=0.50s rtf=0.50" in line
    assert line.endswith("chars=13 backend=clipboard result=injected")
    assert "Ala" not in line


def test_jobs_run_in_submit_order(h: Harness) -> None:
    h.engine.outcomes = [transcript(" jeden"), transcript(" dwa"), transcript(" trzy")]
    jobs = [h.submit() for _ in range(3)]
    finished = [h.outcome() for _ in jobs]
    assert [e.job_id for e in finished] == [j.id for j in jobs]  # type: ignore[union-attr]
    assert h.injector.texts == ["jeden ", "dwa ", "trzy "]


def test_vocabulary_prompt_and_the_job_language_are_sent() -> None:
    # The job's language, not the startup one: the hotkey may have switched it (task 3.7).
    h = Harness(config(stt={"vocabulary_prompt": "PipeWire, Gdańsk."}))
    h.worker.start()
    try:
        h.worker.submit(h.job(language="en"))
        assert isinstance(h.outcome(), JobFinished)
    finally:
        h.worker.stop()
    assert h.engine.calls[0]["prompt"] == "PipeWire, Gdańsk."
    assert h.engine.calls[0]["language"] == "en"


def test_log_text_and_disabled_timings(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    h = Harness(config(logging={"log_text": True, "timings": False}))
    h.worker.start()
    try:
        job = h.submit()
        assert isinstance(h.outcome(), JobFinished)
    finally:
        h.worker.stop()
    messages = [r.getMessage() for r in caplog.records]
    assert f'text job={job.id}: "Ala ma kota. "' in messages
    assert not [r for r in caplog.records if r.name == "local_stt.timings"]


def test_text_is_not_logged_by_default(h: Harness, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    h.submit()
    assert isinstance(h.outcome(), JobFinished)
    assert not any("Ala" in r.getMessage() for r in caplog.records)


# --- discards -----------------------------------------------------------------------------


def test_silent_ptt_job_is_discarded_without_request(h: Harness) -> None:
    job = h.submit(silence(2))
    assert h.outcome() == JobDiscarded(job.id, "ptt", "no_speech")
    assert h.engine.calls == []


# --- VAD trimming (05 §5.3, task 2.6 brought forward) -------------------------------------


@pytest.fixture
def trimmed() -> Iterator[tuple[Harness, FakeTrimmer]]:
    trimmer = FakeTrimmer()
    harness = Harness(config(), trimmer)
    harness.worker.start()
    yield harness, trimmer
    harness.worker.stop()


def test_vad_trimmed_audio_is_transcribed(
    trimmed: tuple[Harness, FakeTrimmer], caplog: pytest.LogCaptureFixture
) -> None:
    h, trimmer = trimmed
    caplog.set_level(logging.INFO, logger="local_stt.timings")
    h.submit(speech(4.0))
    assert isinstance(h.outcome(), JobFinished)
    assert trimmer.calls == [4 * SR]
    assert [c["samples"] for c in h.engine.calls] == [2 * SR]
    assert "audio=2.00s" in caplog.text  # the duration actually transcribed


def test_vad_without_speech_discards_without_request(trimmed: tuple[Harness, FakeTrimmer]) -> None:
    h, trimmer = trimmed
    trimmer.speech = False
    job = h.submit(speech(2.0))  # loud enough for the RMS gate: VAD decides alone
    assert h.outcome() == JobDiscarded(job.id, "ptt", "no_speech")
    assert h.engine.calls == []


def test_inactive_vad_falls_back_to_the_rms_gate() -> None:
    trimmer = FakeTrimmer(active=False)
    h = Harness(config(), trimmer)
    h.worker.start()
    try:
        silent = h.submit(silence(2))
        assert h.outcome() == JobDiscarded(silent.id, "ptt", "no_speech")
        h.submit(speech(2.0))
        assert isinstance(h.outcome(), JobFinished)
    finally:
        h.worker.stop()
    assert trimmer.calls == []
    assert [c["samples"] for c in h.engine.calls] == [2 * SR]  # not trimmed


def test_requeued_job_is_trimmed_again_from_the_original(
    trimmed: tuple[Harness, FakeTrimmer],
) -> None:
    h, trimmer = trimmed
    h.engine.outcomes = [EngineConnectionError("refused")]
    h.submit(speech(4.0))
    assert isinstance(h.next_event(), JobStarted)
    assert h.no_more_events()
    h.worker.resume()
    assert isinstance(h.outcome(), JobFinished)
    assert trimmer.calls == [4 * SR, 4 * SR]
    assert [c["samples"] for c in h.engine.calls] == [2 * SR, 2 * SR]


def test_empty_processor_result_is_filtered(h: Harness) -> None:
    h.processor.result = lambda t: None
    job = h.submit()
    assert h.outcome() == JobDiscarded(job.id, "ptt", "filtered")
    assert h.injector.texts == []


# --- errors and retries (04 §4.4) ---------------------------------------------------------


@pytest.mark.parametrize(
    "error", [EngineHttpError(500, "boom"), EngineTimeoutError("timed out")], ids=["5xx", "timeout"]
)
def test_one_retry_then_success(h: Harness, error: Exception) -> None:
    h.engine.outcomes = [error]
    h.submit()
    assert isinstance(h.outcome(), JobFinished)
    assert len(h.engine.calls) == 2


def test_second_5xx_fails_the_job(h: Harness) -> None:
    h.engine.outcomes = [EngineHttpError(503, "busy"), EngineHttpError(500, "boom")]
    job = h.submit()
    event = h.outcome()
    assert isinstance(event, JobFailed)
    assert (event.job_id, event.audio_s) == (job.id, 1.0)
    assert "500" in event.error
    assert len(h.engine.calls) == 2


@pytest.mark.parametrize(
    "error", [EngineHttpError(400, "bad request"), EngineResponseError("invalid verbose_json")]
)
def test_4xx_and_invalid_response_fail_without_retry(h: Harness, error: Exception) -> None:
    h.engine.outcomes = [error]
    h.submit()
    assert isinstance(h.outcome(), JobFailed)
    assert len(h.engine.calls) == 1


def test_cancel_during_retry_delay(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline_mod, "RETRY_DELAY_S", WAIT_S)
    h.engine.outcomes = [EngineHttpError(500, "boom")]
    job = h.submit()
    assert h.next_event() == JobStarted(job.id)
    h.engine.entered.wait(WAIT_S)
    result = h.worker.cancel_all()
    assert result.in_flight_cancelled
    assert h.next_event() == JobDiscarded(job.id, "ptt", "cancelled")
    assert len(h.engine.calls) == 1


def test_request_timeout_follows_recent_rtf(h: Harness) -> None:
    h.engine.outcomes = [transcript(audio_s=1.0, proc_s=0.5), transcript(audio_s=10.0, proc_s=5.0)]
    h.submit(speech(1.0))
    h.submit(speech(10.0))
    h.submit(speech(20.0))
    h.submit(speech(200.0))
    for _ in range(4):
        assert isinstance(h.outcome(), JobFinished)
    timeouts = [c["timeout_s"] for c in h.engine.calls]
    # no history -> cap; then max(10, 4 * audio * mean RTF 0.5), capped at 120 s
    assert timeouts[0] == 120.0
    assert timeouts[1] == 20.0
    assert timeouts[2] == pytest.approx(40.0)
    assert timeouts[3] == 120.0


# --- generations (04 §4.4) ----------------------------------------------------------------


def test_cancel_drains_queue_and_discards_job_in_transcription(h: Harness) -> None:
    h.engine.gate = threading.Event()
    first = h.submit()
    assert h.next_event() == JobStarted(first.id)
    h.engine.entered.wait(WAIT_S)
    queued = [h.submit(), h.submit()]
    result = h.worker.cancel_all()
    assert result.drained_job_ids == tuple(j.id for j in queued)
    assert (result.in_flight_cancelled, result.injection_in_flight) == (True, False)
    h.engine.gate.set()
    assert h.next_event() == JobDiscarded(first.id, "ptt", "cancelled")
    assert h.no_more_events()
    assert h.injector.texts == []


def test_cancel_during_text_processing(h: Harness) -> None:
    def cancel() -> None:
        h.worker.cancel_all()

    h.processor.hook = cancel
    job = h.submit()
    assert h.outcome() == JobDiscarded(job.id, "ptt", "cancelled")
    assert h.injector.texts == []


def test_cancel_while_injector_waits(h: Harness) -> None:
    h.injector.wait_for_cancel = True
    job = h.submit()
    assert h.next_event() == JobStarted(job.id)
    h.injector.waiting.wait(WAIT_S)  # past the last generation checkpoint
    result = h.worker.cancel_all()
    assert (result.in_flight_cancelled, result.injection_in_flight) == (True, False)
    assert h.next_event() == JobDiscarded(job.id, "ptt", "cancelled")


def test_started_input_operation_finishes_and_is_reported(h: Harness) -> None:
    h.injector.hold_operation = threading.Event()
    job = h.submit()
    h.injector.in_operation.wait(WAIT_S)
    result = h.worker.cancel_all()
    assert result.injection_in_flight
    h.injector.hold_operation.set()
    event = h.outcome()
    assert isinstance(event, JobFinished) and event.job_id == job.id
    assert h.injector.texts == ["Ala ma kota. "]


def test_new_generation_runs_after_cancel(h: Harness) -> None:
    h.worker.cancel_all()
    job = h.job()  # created with the new generation
    h.worker.submit(job)
    assert isinstance(h.outcome(), JobFinished)


def test_stale_job_submitted_after_cancel_is_discarded(h: Harness) -> None:
    old = h.job()
    h.worker.cancel_all()
    h.worker.submit(old)
    assert h.outcome() == JobDiscarded(old.id, "ptt", "cancelled")
    assert h.engine.calls == []


# --- pause on DOWN (04 §4.5) --------------------------------------------------------------


def test_connection_error_pauses_and_retries_job_after_resume(h: Harness) -> None:
    h.engine.outcomes = [EngineConnectionError("refused")]
    job = h.submit()
    later = h.submit()
    assert h.next_event() == JobStarted(job.id)
    assert h.no_more_events()
    assert h.worker.paused
    assert h.connection_failures == 1
    h.worker.resume()
    assert h.next_event() == JobStarted(job.id)  # the failed job runs first
    assert isinstance(h.next_event(), JobFinished)
    assert h.next_event() == JobStarted(later.id)
    assert isinstance(h.next_event(), JobFinished)


def test_connection_error_after_cancel_discards_job_and_still_pauses(h: Harness) -> None:
    h.engine.gate = threading.Event()
    h.engine.outcomes = [EngineConnectionError("refused")]
    job = h.submit()
    h.engine.entered.wait(WAIT_S)
    h.worker.cancel_all()
    h.engine.gate.set()
    assert h.outcome() == JobDiscarded(job.id, "ptt", "cancelled")
    assert h.worker.paused
    assert h.connection_failures == 1


def test_cancel_while_paused_drains_requeued_job(h: Harness) -> None:
    h.engine.outcomes = [EngineConnectionError("refused")]
    job = h.submit()
    assert h.next_event() == JobStarted(job.id)
    assert h.no_more_events()
    result = h.worker.cancel_all()
    assert result.drained_job_ids == (job.id,)
    assert not result.in_flight_cancelled


def test_paused_queue_fails_after_startup_timeout() -> None:
    h = Harness(config(stt={"startup_timeout_s": 0.2}))
    h.worker.pause()
    h.worker.start()
    try:
        jobs = [h.submit(), h.submit()]
        events = [h.next_event(), h.next_event()]
    finally:
        h.worker.stop()
    assert all(isinstance(e, JobFailed) for e in events)
    assert [e.job_id for e in events] == [j.id for j in jobs]  # type: ignore[union-attr]
    assert h.engine.calls == []


def test_resume_before_startup_timeout_keeps_jobs() -> None:
    h = Harness(config(stt={"startup_timeout_s": 60}))
    h.worker.pause()
    h.worker.start()
    try:
        h.submit()
        assert h.no_more_events()
        h.worker.resume()
        assert isinstance(h.outcome(), JobFinished)
    finally:
        h.worker.stop()


def test_live_reload_shortens_pause_timeout() -> None:
    h = Harness(config(stt={"startup_timeout_s": 60}))
    h.worker.pause()
    h.worker.start()
    try:
        h.submit()
        assert h.no_more_events()
        h.worker.update_config(config(stt={"startup_timeout_s": 0.0}))
        assert isinstance(h.next_event(), JobFailed)
    finally:
        h.worker.stop()


@pytest.mark.parametrize(
    ("result", "outcome"),
    [
        (InjectResult(True, "clipboard", 5, None, True, None), "clipboard"),
        (InjectResult(False, "type", 0, "xterm", False, "xdotool failed"), "failed"),
    ],
)
def test_timing_line_outcome(result: InjectResult, outcome: str) -> None:
    job = Job(7, "continuous", speech(2.0), 0.0, 0, 1, 4, "silence", "pl")
    t = {"audio": 2.0, "queued": 0.1, "stt": 1.0, "text": 0.002, "inject": 0.2, "total": 1.5}
    line = pipeline_mod._timing_line(job, transcript(audio_s=2.0, proc_s=1.0), t, result)
    assert line == (
        "job=7 src=continuous seq=4 cut=silence audio=2.00s queued=0.10s stt=1.00s rtf=0.50 "
        "text=2ms "
        f"inject=200ms total=1.50s chars={result.chars} backend={result.backend} "
        f"result={outcome}"
    )


# --- CancellationToken (08 §8.3) ----------------------------------------------------------


def test_token_operation_refused_after_cancel() -> None:
    lock = threading.Lock()
    token = CancellationToken(lock)
    with token.operation():
        assert token.in_operation
        with lock:
            assert token.cancel_locked() is True
    assert not token.in_operation
    assert token.cancelled
    assert token.wait(0)
    with pytest.raises(Cancelled), token.operation():
        pass


# --- continuous session context (task 2.4; 06 §6.6, 08 §8.1) -------------------------------


def continuous_job(
    h: Harness, seq: int, *, session_id: int = 1, cut: Cut = "silence", language: str = "pl"
) -> Job:
    return dataclasses.replace(
        h.job(language=language), source="continuous", session_id=session_id, seq=seq, cut=cut
    )


def run_continuous(h: Harness, *jobs: Job) -> None:
    for job in jobs:
        h.worker.submit(job)
        assert isinstance(h.outcome(), JobFinished | JobDiscarded | JobFailed)


def test_session_text_goes_into_the_prompt(h: Harness) -> None:
    h.worker.update_config(config(stt={"vocabulary_prompt": "PipeWire."}))
    h.engine.outcomes = [transcript(" Ala ma kota."), transcript(" Kot ma Alę.")]
    run_continuous(h, continuous_job(h, 1), continuous_job(h, 2), continuous_job(h, 3))
    assert [c["prompt"] for c in h.engine.calls] == [
        "PipeWire.",
        "PipeWire. Ala ma kota.",
        "PipeWire. Ala ma kota. Kot ma Alę.",
    ]
    assert [c.prompt_tail for c in h.processor.contexts] == [
        None,
        "Ala ma kota.",
        "Ala ma kota. Kot ma Alę.",
    ]


def test_language_switch_drops_the_session_context(h: Harness) -> None:
    # task 3.7: Polish context would only mislead an English segment, and the other way round.
    h.engine.outcomes = [transcript(" Ala ma kota."), transcript(" A cat."), transcript(" Dog.")]
    run_continuous(
        h,
        continuous_job(h, 1),
        continuous_job(h, 2, language="en"),
        continuous_job(h, 3, language="en"),
        continuous_job(h, 4),
    )
    assert [c["language"] for c in h.engine.calls] == ["pl", "en", "en", "pl"]
    assert [c["prompt"] for c in h.engine.calls] == [None, None, "A cat.", None]


def test_long_pause_keeps_the_context_by_default(h: Harness) -> None:
    # task 3.4: the new-paragraph reset raised WER, so it is off (CONTEXT_RESET_S = None).
    assert CONTEXT_RESET_S is None
    h.engine.outcomes = [transcript(" Ala ma kota."), transcript(" Kot.")]
    run_continuous(h, continuous_job(h, 1), pause(continuous_job(h, 2), 60.0))
    assert [c["prompt"] for c in h.engine.calls] == [None, "Ala ma kota."]


def test_pause_above_context_reset_s_starts_a_new_paragraph() -> None:
    h = Harness(config(), context_reset_s=5.0)
    h.worker.start()
    h.engine.outcomes = [transcript(" Ala ma kota."), transcript(" Kot."), transcript(" Pies.")]
    jobs = [
        continuous_job(h, 1),
        pause(continuous_job(h, 2), 5.0),
        pause(continuous_job(h, 3), 5.1),
    ]
    run_continuous(h, *jobs)
    assert [c["prompt"] for c in h.engine.calls] == [None, "Ala ma kota.", None]
    h.worker.stop()


def pause(job: Job, seconds: float) -> Job:
    return dataclasses.replace(job, pause_before_s=seconds)


def test_context_policy_is_a_constructor_parameter() -> None:
    # bench --context compares policies through these parameters.
    h = Harness(config(), context_chars=5, context_reset_s=None)
    h.worker.start()
    h.engine.outcomes = [transcript(" Ala ma kota."), transcript(" Kot.")]
    jobs = [
        continuous_job(h, 1),
        pause(continuous_job(h, 2), 60.0),
        continuous_job(h, 3),
    ]
    run_continuous(h, *jobs)
    assert [c["prompt"] for c in h.engine.calls] == [None, "kota.", "Kot."]
    h.worker.stop()


def test_context_keeps_the_last_200_characters(h: Harness) -> None:
    h.engine.outcomes = [transcript(" " + "a" * 150 + "."), transcript(" " + "b" * 150 + ".")]
    run_continuous(h, continuous_job(h, 1), continuous_job(h, 2), continuous_job(h, 3))
    tail = h.processor.contexts[2].prompt_tail
    assert tail is not None and len(tail) == 200 and tail.endswith("b.")


def test_prev_cut_follows_the_session(h: Harness) -> None:
    run_continuous(
        h,
        continuous_job(h, 1, cut="max_length"),
        continuous_job(h, 2),
        continuous_job(h, 1, session_id=2),  # a new session forgets the old one
    )
    assert [(c.cut, c.prev_cut) for c in h.processor.contexts] == [
        ("max_length", None),
        ("silence", "max_length"),
        ("silence", None),
    ]
    assert h.processor.contexts[2].prompt_tail is None


def test_ptt_and_disabled_context_send_only_the_vocabulary(h: Harness) -> None:
    h.worker.update_config(
        config(stt={"vocabulary_prompt": "Gdańsk.", "continuous_context": False})
    )
    run_continuous(h, continuous_job(h, 1), continuous_job(h, 2))
    h.submit()
    assert isinstance(h.outcome(), JobFinished)
    assert [c["prompt"] for c in h.engine.calls] == ["Gdańsk."] * 3
    assert h.processor.contexts[1].prompt_tail is None
    assert h.processor.contexts[1].prev_cut == "silence"  # continuity does not need context


def test_filtered_text_is_not_context_but_its_cut_counts(h: Harness) -> None:
    h.processor.result = lambda t: None
    run_continuous(h, continuous_job(h, 1, cut="max_length"))
    h.processor.result = lambda t: t.text.strip() + " "
    run_continuous(h, continuous_job(h, 2))
    assert h.processor.contexts[1].prompt_tail is None
    assert h.processor.contexts[1].prev_cut == "max_length"


def test_requeued_job_keeps_its_own_context(h: Harness) -> None:
    h.engine.outcomes = [transcript(" Ala ma kota."), EngineConnectionError("refused")]
    run_continuous(h, continuous_job(h, 1, cut="max_length"))
    h.worker.submit(continuous_job(h, 2))
    assert isinstance(h.next_event(), JobStarted)
    assert h.no_more_events()  # requeued and paused
    h.worker.resume()
    assert isinstance(h.outcome(), JobFinished)
    assert [c["prompt"] for c in h.engine.calls] == [None, "Ala ma kota.", "Ala ma kota."]
    (ctx,) = h.processor.contexts[1:]
    assert ctx.prev_cut == "max_length"
