# 04. State machine and event flow

## 4.1 Why not the preliminary design diagram

The diagram in the [preliminary design](archive/preliminary-design.md) (§10) has two flaws:

1. **Continuous mode loses speech.** The `LISTENING → TRANSCRIBING → INJECT → LISTENING` transition means that nothing is listening during transcription. On an i5-8365U, transcribing 5 s of speech with the `small` model takes several seconds, while the user continues speaking.
2. **The mode and background work are combined into one state.** What the user is doing (holding PTT, dictating) is independent of what the engine is doing (processing the transcription queue).

The daemon state therefore consists of **three independent components**, modified exclusively by the `controller` thread:

```text
DaemonState = (mode, pipeline, engine)

mode     ∈ { IDLE, PTT_RECORDING, CONTINUOUS(speech: bool, reconnecting: bool) }
pipeline = { queued_jobs: int, queued_audio_s: float, busy: bool, paused: bool, generation: int }
engine   ∈ { STARTING, READY, DOWN }
```

The controller also stores `pending_reload: Config | None` (4.6), the identifiers described in 4.2, and the `stopping` flag for PTT and continuous mode. While a recording is being finalized, the mode remains active until the audio consumer confirms completion.

## 4.2 Events

All sources send events to a single `controller.events` queue (`queue.Queue`). The Controller processes them sequentially, so it does not need locks around its own state.

| Event | Source | Data |
|---|---|---|
| `PttPressed` | HotkeyListener / IPC `ptt start` | `at: float` (monotonic, at the source), `reply: Future \| None` |
| `PttReleased` | HotkeyListener / IPC `ptt stop` | `at: float` (monotonic, at the source), `reply` |
| `PttCancelKey` | HotkeyListener (`hotkeys.ptt_cancel_key` while PTT is held) | — |
| `ContinuousToggle` | HotkeyListener / IPC `toggle` | `reply` |
| `CancelRequested` | IPC `cancel` | `reply` |
| `RecordingStarted` | audio consumer (first frame after opening the stream) | `recording_id`, `capture_id` |
| `RecordingLimitReached` | audio consumer (Recorder, PTT) | `recording_id`, `capture_id`, `ended_at` |
| `RecordingFinished` | audio consumer (after the `finish_ptt` command) | `recording_id`, `capture_id`, `operation_id`, `AudioClip`, `cut` |
| `SpeechStarted` / `SpeechEnded` | audio-consumer (Segmenter) | `recording_id`, `capture_id` |
| `SegmentReady` | audio consumer (Segmenter) | `recording_id`, `capture_id`, `AudioSegment`; also `operation_id` during flush |
| `FlushDone` | audio consumer (after the `flush` command) | `recording_id`, `capture_id`, `operation_id`, `purpose ∈ {stop, reconnect}` |
| `AudioError` | AudioCapture / audio consumer | `recording_id`, `capture_id`, `kind ∈ {open_failed, device_lost}`, description |
| `ReconnectTick` | controller timer | `recording_id`, `operation_id`, attempt number |
| `CaptureOpenDue` | controller timer (150 ms after continuous mode starts) | `recording_id` |
| `ServerRestartDone` | helper thread executing `systemctl --user restart` (4.6) | exit code |
| `JobStarted` / `JobFinished` / `JobDiscarded` / `JobFailed` | PipelineWorker | `job_id`, timings, reason (`no_speech`, `filtered`, `cancelled`), or error |
| `EngineStateChanged` | EngineMonitor / PipelineWorker | `READY` / `STARTING` / `DOWN` |
| `ReloadRequested` | IPC `reload`, `SIGHUP` | `reply` |
| `ShutdownRequested` | `SIGTERM`, `SIGINT` | — |
| `X11ConnectionLost` | HotkeyListener / ClipboardOwner | — |

**Recording and operation identity.** The Controller assigns a new, monotonically increasing `recording_id` to every PTT recording and continuous session; in continuous mode, `session_id = recording_id`. Every planned attempt to open a stream receives a new `capture_id`, including reconnect attempts. For delayed continuous startup, the identifier is assigned before the timer, so an empty session can be finalized even before opening. Every finalization or reconnect gets an `operation_id`. Audio commands and frames carry the identifiers assigned when they were created rather than reading them only upon receipt.

Before handling an event, the Controller checks its identifiers against the expected recording, stream, and operation. `cancel` invalidates them immediately. A new recording in the same mode cannot accept events from the previous one. `FlushDone(purpose=reconnect)` does not end a session by itself; only a matching `FlushDone(purpose=stop)` switches to IDLE. If the user requests stop during a reconnect flush, we first receive that operation's result and then request a stop flush (4.3). This rule applies to audio events, not completed pipeline jobs, which may legitimately return after recording has ended.

**IPC responses.** An event with `reply` always receives a response:

- `{"ok": true}` when accepted (for `cancel`, also `injection_in_flight`, 4.4; for reload, the fields from 4.6),
- `{"ok": false, "error": "<code>", "message": "…"}` when rejected or ignored in the current mode.

Error codes:

- `invalid_in_mode` — e.g. `toggle` during PTT, `ptt start` during continuous mode,
- `engine_down`,
- `engine_starting`,
- `vad_disabled`,
- `audio_error`,
- `cancelled` — continuous startup was cancelled before the delayed microphone open completed.

The CLI maps every rejection to exit code 4.

## 4.3 Transition table — `mode`

Combinations of (state, event) **not present** in the table:

- events with `reply` → reject with `invalid_in_mode`,
- events without `reply` → ignore and log DEBUG `ignored <event> in <mode>`.

Until continuous mode exists (v0.2, task 2.3), `ContinuousToggle` is rejected in every mode with `invalid_in_mode` (“continuous dictation arrives in v0.2”); from the hotkey it also plays the `error` sound.

Sounds are referred to by the names from [10](10-cli-ipc-status.md) §10.6 (the single source of truth for sounds).

### IDLE

| Event | Condition | Actions | New state |
|---|---|---|---|
| `PttPressed` | `engine == READY` | assign `recording_id` and `capture_id`, remember press time; request `audio_consumer.begin_ptt(ids)`, then `capture.open(ids)` (synchronously, 30–150 ms); open failure → handle like `AudioError` in PTT_RECORDING | PTT_RECORDING |
| `PttPressed` | `engine != READY` | `error` sound, “STT engine unavailable” notification, reject with `engine_down`/`engine_starting` | IDLE |
| `ContinuousToggle` | `engine == READY` and `vad.enabled` | `start` sound, new `recording_id` (`session_id`) and `capture_id`; request a Segmenter reset with these identifiers in the audio consumer; schedule `CaptureOpenDue(recording_id)` in 150 ms (the Controller does not block). Send the IPC response only after the open attempt | CONTINUOUS |
| `ContinuousToggle` | `engine != READY` / `!vad.enabled` | `error` sound, reject with `engine_*` / `vad_disabled` | IDLE |
| `CancelRequested` | — | `pipeline.cancel_all()`; `cancel` sound if anything was discarded | IDLE |
| `JobDiscarded(no_speech)` | PTT source | `cancel` sound | IDLE |

### PTT_RECORDING

| Event | Condition | Actions | New state |
|---|---|---|---|
| `RecordingStarted` | not `stopping` | `start` sound; tell the Recorder to mask through the end of the sound + 80 ms (5.3), only if the sound is played | PTT_RECORDING |
| `PttReleased` | time since press ≥ `ptt.min_duration_ms`, not `stopping` | run the *Finish PTT* procedure below with `cut="release"`; IPC confirms command acceptance | PTT_RECORDING(stopping) |
| `PttReleased` | time since press < `ptt.min_duration_ms`, not `stopping` | invalidate the recording, `capture.close()`, request `audio_consumer.discard(ids)`; signal as specified in 10 §10.6, DEBUG `ptt too short` | IDLE |
| `RecordingFinished` | matching identifiers, `stopping` | `pipeline.submit(Job(ptt, clip, cut))`, `stop` sound | IDLE |
| `RecordingLimitReached` | not `stopping` | *Finish PTT* with `cut="max_duration"` + “Recording limit reached” notification | PTT_RECORDING(stopping)* |
| `PttCancelKey` | including while `stopping` | invalidate the recording and finalization, `capture.close()`, request `audio_consumer.discard(ids)`, `cancel` sound; earlier pipeline jobs remain | IDLE |
| `CancelRequested` | including while `stopping` | as for `PttCancelKey`, plus `pipeline.cancel_all()` | IDLE |
| `AudioError` | — | invalidate the recording and finalization, `capture.close()`, request `audio_consumer.discard(ids)`, `error` sound, notification | IDLE |

\* The key may still be held. Another release while `stopping` or in IDLE does not start finalization again. A repeated IPC command receives `invalid_in_mode`.

*Finish PTT*: the Controller records `ended_at` (the release event's `at` field or the time the limit was reached, not the time the event was processed), sets `stopping`, assigns an `operation_id`, closes capture, and only after callbacks have finished requests `audio_consumer.finish_ptt(ids, ended_at, cut)`. The audio consumer drains this stream's frame queue, trims all collected audio (including frames processed earlier) to the `ended_at` boundary, calls `recorder.end()` in its own thread, and emits `RecordingFinished`. The Controller does not touch Recorder buffers or synchronously wait for their processing. Finalization time contributes to job latency; `Job.ended_at` is not the time `RecordingFinished` was received.

The silence gate and VAD trimming do **not** run in the Controller. PipelineWorker performs them (4.4), because trimming a 120 s recording requires several thousand ONNX calls and the Controller must not block.

### CONTINUOUS

| Event | Condition | Actions | New state |
|---|---|---|---|
| `CaptureOpenDue(rid)` | matching `recording_id` and not `stopping` | `capture.open(ids)` with identifiers assigned at startup: success → `ok` response; failure → `AudioError(open_failed)` row | CONTINUOUS |
| `SpeechStarted` / `SpeechEnded` | — | set `speech` | CONTINUOUS |
| `SegmentReady` | — | `pipeline.submit(Job(continuous, segment, session_id, seq, cut))`; if `queued_audio_s > continuous.max_backlog_s` → *Backlog* row | CONTINUOUS |
| `ContinuousToggle` | not `stopping` | *Stop(flush)* row, `stop` sound | → IDLE after `FlushDone` |
| *Backlog* (internal) | not `stopping` | *Stop(flush)* row, `stop` + `error` sounds, “Transcription cannot keep up — dictation stopped” notification; finish processing the queue | → IDLE after `FlushDone` |
| `CancelRequested` | — | invalidate the recording and operations, `capture.close()`, request `audio_consumer.discard(ids)`, `pipeline.cancel_all()`, `cancel` sound; a pending IPC start receives `cancelled` | IDLE |
| `EngineStateChanged(DOWN)` | not `stopping` | *Stop(flush)* row, `stop` + `error` sounds; segments wait in the paused queue (4.5); “STT engine stopped working — dictation stopped” notification | → IDLE after `FlushDone` |
| `AudioError(device_lost)` | not `reconnecting` and not `stopping` | `capture.close()`, new `operation_id`, request `audio_consumer.flush(ids, purpose="reconnect")`, set `reconnecting = true`; wait for confirmation before reopening | CONTINUOUS(reconnecting) |
| `FlushDone(reconnect)` | matching identifiers, `reconnecting`, not `stopping` | schedule `ReconnectTick(recording_id, operation_id, 1)` in 1 s | CONTINUOUS(reconnecting) |
| `FlushDone(reconnect)` | matching identifiers and `stopping` | after receiving reconnect segments, request a new `flush(purpose="stop")` with a new `operation_id`; no open timer | CONTINUOUS(stopping) |
| `ReconnectTick(n)` | matching recording and operation, `reconnecting`, not `stopping` | assign a new `capture_id`; request an audio-consumer buffer/VAD reset (preserve `session_id` and the next `seq`); attempt `capture.open(ids)`: success → `reconnecting = false`, INFO; failure with `n < 3` → retry in 1 s; third failure → *Stop(flush)*, notification, and `stop` + `error` sounds | CONTINUOUS / → IDLE after `FlushDone(stop)` |
| `FlushDone(stop)` | matching identifiers and `stopping` | end the session | IDLE |
| `AudioError(open_failed)` | during mode startup (`CaptureOpenDue`) | `error` sound, notification, reject with `audio_error` | IDLE |
| `JobFailed` | — | notification (4.4); **mode continues** | CONTINUOUS |

*Stop(flush)* works as follows:

1. The Controller calls `capture.close()` (if the stream is open). PortAudio's `stream.stop()` waits for callbacks to finish, so no new frames can enter the queue after it returns.
2. The Controller sets `stopping` (blocking reconnect timers). If a reconnect flush is still in progress, it preserves that flush's identifiers and receives its segments and `FlushDone(reconnect)`; only then does it assign a new `operation_id` and send `audio_consumer.flush(ids, purpose="stop")`. In all other cases it requests the stop flush immediately.
3. The audio-consumer thread processes frames remaining in the queue, calls `segmenter.flush()`, optionally emits `SegmentReady(cut="flush")`, and then emits `FlushDone`.
4. After a matching `SegmentReady`, the Controller submits the job as usual; after a matching `FlushDone(purpose="stop")`, it switches to IDLE. The audio consumer does not emit audio already returned by the reconnect flush a second time; late events from a completed operation are ignored.

Between steps 1 and 4, another `ContinuousToggle` is ignored (IPC: `invalid_in_mode`), and another backlog overflow does not request another flush. Stopping before `CaptureOpenDue` finalizes an empty session and completes the pending IPC start response with `cancelled`. Each finalization operation has exactly one confirmation; after accepting it, the Controller marks the operation complete. A duplicate confirmation does not reopen the stream or trigger a second flush. The audio consumer executes `begin/reset`, `finish/flush`, and `discard` commands in FIFO order; discard applies only to the specified identifiers, so it does not remove frames from a new recording.

### Any state

| Event | Actions |
|---|---|
| `ShutdownRequested` | close capture, `pipeline.cancel_all()`, ungrab, close the IPC socket → exit 0 |
| `X11ConnectionLost` | close capture and the IPC socket, **without** X11 operations → WARNING, exit 0 (the session is ending; systemd does not restart, and `PartOf` stops the unit) |
| `ReloadRequested` | 4.6 |
| `EngineStateChanged(s)` | `engine = s`; `READY` → `pipeline.paused = false`; in CONTINUOUS, `DOWN` also applies the corresponding CONTINUOUS table row |
| `JobStarted` / `JobFinished` | update `pipeline` and status statistics; `JobFinished` with `left_in_clipboard` → notification from [08](08-text-injection.md) §8.5 (sent by the Controller based on `InjectResult`; the injector does not notify by itself) |
| `JobFailed` | update statistics + aggregate notification (4.4), regardless of mode |
| `JobDiscarded` | update statistics; `cancel` sound only in IDLE and only for `no_speech` from PTT (IDLE table) |
| audio events and timers with a stale `recording_id`, `capture_id`, or `operation_id` | ignored before consulting the transition table, even if the new session has the same mode |
| `ServerRestartDone` | 4.6 |

## 4.4 Pipeline (job queue)

```text
Controller ──submit(Job)──► jobs ──► PipelineWorker (1 thread)
                                        │
                                        ├─ [PTT] silence gate / VAD trimming ──► no speech → JobDiscarded(no_speech)
                                        ├─ generation check
                                        ├─ engine.transcribe(audio, prompt)
                                        ├─ generation check
                                        ├─ processor.process(transcript, ctx) ──► None → JobDiscarded(filtered)
                                        ├─ generation check
                                        └─ injector.inject(text, cancel=token) ──► JobFinished / JobDiscarded(cancelled)
```

- **One worker thread.** `whisper-server` processes one request at a time, and a single worker guarantees that text is entered in recording order.
- **Generations instead of interruption.** `cancel_all()` increments `generation` and drains the queue. Each `Job` remembers the generation at creation time, and the worker discards a stale job before every step (`JobDiscarded(cancelled)`). An in-flight HTTP request is not interrupted; its result is discarded.
- **Cancellation in the injector.** The old generation's token is invalidated even while the worker is waiting for PTT, modifiers, or the clipboard. The final check and the start of the input operation are synchronized with `cancel_all()` by a short lock (8 §8.3). After cancellation, no new paste or subsequent `type` chunk starts. An XTest sequence or `type` chunk that has already started may finish; we do not undo text in another application. The `cancel` response contains `injection_in_flight`, indicating such an operation at the moment of cancellation.
- **PTT while the worker is busy** is allowed. The new job is queued.
- **Continuous context.** The worker keeps `last_text[session_id]`. At most the last 200 characters are passed to the engine as part of the `prompt` ([06](06-stt-engine.md) §6.6).
- **Transcription error:**
  - A **connection** error (server does not respond) → the worker emits `EngineStateChanged(DOWN)`, **pauses** (`paused = true`), and puts the job back at the front of the queue (4.5).
  - HTTP 5xx / timeout → one retry after 1 s; then `JobFailed`.
  - HTTP 4xx → immediate `JobFailed`.
  - An invalid response body (HTTP 200 without valid `verbose_json`) → immediate `JobFailed`, like 4xx: the failure is most likely deterministic (user decision 2026-10-03).
  - `JobFailed` → “Could not transcribe segment (N s)” notification. Several failures within 10 s are combined into one “N segments could not be transcribed” notification. Audio is removed from memory.
- An **empty result** (no speech, filtered hallucination) is not an error: `JobDiscarded`, DEBUG log.

## 4.5 Engine (`engine`)

`EngineMonitor` polls `GET <request_path>/health` ([06](06-stt-engine.md) §6.5):

- every 500 ms in `STARTING` and `DOWN`,
- every 10 s in `READY`.

Transitions:

- `200 ok` → `READY`,
- `503 loading model` → `STARTING`,
- connection error → `STARTING` if less than `stt.startup_timeout_s` has elapsed since daemon startup (or a requested server restart); otherwise `DOWN`.

PipelineWorker reports a connection failure through `EngineMonitor.report_connection_failure()` instead of posting `DOWN` itself: the monitor posts `DOWN` and switches to 500 ms polling. Otherwise the monitor, still in READY, would see no change when the server returns and the Controller would stay DOWN. A requested restart (4.6) calls `EngineMonitor.restarted()`, which starts a new startup grace period and polls immediately; a health result started before a restart or reported failure is discarded. Verified 2026-10-03 against a real `whisper-server`: READY 0.1 s after start; after the server was stopped, DOWN at the next READY poll (≤ 10 s).

**Paused queue.** After `EngineStateChanged(DOWN)` (including one reported by the worker), the pipeline has `paused = true`. Jobs wait while systemd restarts the server (`RestartSec=2` + model loading), which takes several seconds.

- `READY` → `paused = false`; the worker resumes with the job that failed.
- If the engine does not return within `stt.startup_timeout_s` of entering DOWN, all queued jobs receive `JobFailed` and we display **one** aggregate notification. The pipeline owns this timer: it starts at `pause()` (or at the worker's own connection failure), `resume()` cancels it, and the Controller's 10 s `JobFailed` aggregation turns the burst into one notification (user decision 2026-10-03).

The daemon does not start the server itself after a failure (`Restart=on-failure` does that). The only exception is an explicitly requested restart during reload (4.6).

## 4.6 Reload

`local-stt reload` or `systemctl --user reload local-stt` (`SIGHUP`):

1. Load and validate the new config. If it is invalid, retain the old one and respond with `{"ok": false, "errors": [...]}`.
2. Compute the section diff. Keys are divided into three groups:

| Group | Keys | When applied |
|---|---|---|
| **live** | `logging.*`, `text.*`, `injection.*`, `feedback.*`, `ptt.*`, `continuous.*`, `stt.vocabulary_prompt`, `stt.continuous_context`, `stt.no_speech_threshold`, `stt.logprob_threshold`, `stt.startup_timeout_s`, `stt.request_timeout_max_s` | immediately |
| **at IDLE** | `audio.*`, `vad.*`, `hotkeys.*` | immediately if `mode == IDLE`; otherwise stored in `pending_reload` and applied on the next transition to IDLE (without interrupting the recording) |
| **server restart** ⟳ | `stt.engine`, `stt.model`, `stt.models_dir`, `stt.language`, `stt.threads`, `stt.beam_size`, `stt.port`, `stt.extra_server_args`, `stt.audio_ctx`, `stt.audio_ctx_margin` (a server only ever sees one fixed `audio_ctx` plus the full window, 06 §6.7) | see below |

3. **Server restart** (⟳):
   - the daemon generates a new `whisper-server.env` ([09](09-configuration.md) §9.4),
   - waits until `mode == IDLE` and the queue is empty (or `engine == DOWN`, in which case a restart is needed anyway),
   - pauses the pipeline and runs `systemctl --user restart local-stt-whisper.service` in a helper thread (the Controller does not block), which reports `ServerRestartDone`,
   - switches the client to the new `port`, sets `engine = STARTING`, and resumes the pipeline after `READY`; exit code ≠ 0 or no `READY` within `startup_timeout_s` → error E16 ([12](12-logging-privacy-errors.md)).
   - `stt.models_dir` also affects the VAD model path, which is applied like the “at IDLE” group.

   We do not use `POST /load`; the rationale is in [06](06-stt-engine.md) §6.5.
4. Response: `{"ok": true, "applied": [...], "deferred": [...], "server_restart": true|false}`. Lists contain changed keys (`section.key`). Server keys are `applied` when the restart starts immediately and `deferred` while it waits for IDLE, an empty queue, or a restart already in progress. A restart failure is detected either from the `systemctl` exit code or from `EngineStateChanged(DOWN)` arriving before `READY`.

## 4.7 Externally visible status

`local-stt status` composes one readable state (priority from top to bottom):

| Displayed state | Condition |
|---|---|
| `ERROR: engine down` | `engine == DOWN` |
| `STARTING` | `engine == STARTING` |
| `RECORDING` | `mode == PTT_RECORDING` |
| `LISTENING (reconnecting)` | `mode == CONTINUOUS` and `reconnecting` |
| `LISTENING (speech)` / `LISTENING` | `mode == CONTINUOUS` |
| `TRANSCRIBING (n queued)` | `mode == IDLE` and (`busy` or `queued_jobs > 0`) |
| `IDLE` | otherwise |

In `LISTENING` mode, `, transcribing n` is appended to the status. The JSON format is described in [10](10-cli-ipc-status.md) §10.4.
