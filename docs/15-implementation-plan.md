# 15. Implementation plan

The sequence follows the preliminary design principle: **first establish whether the model is fast enough, then implement PTT, and continuous mode last.** Each stage ends with acceptance criteria. Do not begin a stage until the previous one meets its criteria.

## Stage 0 — Environment and measurement (before v0.1)

**Goal:** a working `whisper-server`, a recording corpus, and initial measurements. No daemon or hotkeys.

| # | Task | Result |
|---|---|---|
| 0.1 | Repository skeleton: `pyproject.toml` (src layout), `ruff`, `mypy`, `pytest` (including the mypy override for `Xlib.*`, [14](14-tests.md)), `requirements.lock`; the git repository already exists | `pytest` passes on an empty suite |
| 0.2 | `scripts/install.sh` steps 1–4 (apt, build whisper.cpp v1.9.4 + `.whisper-tag`, venv) and `secret` generation | `bin/whisper-server --help` |
| 0.3 | `local_stt/models.py` + `local-stt models pull/list/verify` + `src/local_stt/models.sha256` (base-q5_1, small-q5_1, small-q8_0, small, medium-q5_0, large-v3-turbo-q5_0, silero-vad) | models on disk, checksums valid |
| 0.4 | `audio/capture.py` (minimum: 16 kHz `pipewire` PCM, frames, `PIPEWIRE_NODE`) + `audio/wav.py` | `needs_audio` test; confirmation that `sounddevice` with `PIPEWIRE_NODE` reaches the selected node (already verified for `arecord`, [05](05-audio-and-vad.md) §5.2) |
| 0.5 | `stt/whisper_server.py` + `local-stt transcribe FILE.wav` | Polish text from a file |
| 0.6 | `bench/corpus.py` (`record-corpus`, including `--long`) + `bench/prompts_pl.txt` and `bench/long_pl.txt` (package data) | corpus A recorded |
| 0.7 | `bench/wer.py`, `bench/runner.py` (stages 1–2 from [13](13-benchmark.md) §13.4; temporary server), `bench/report.py` | preliminary `docs/benchmark-results.md` |

**Acceptance:**

- stage 1–2 report for every model,
- provisional `stt.model`, `threads`, and `audio_ctx` selected under the rule in §13.5; the report explicitly states that N2 remains unconfirmed until the full injector measurement in v0.1 (the soak test comes in v0.2).

## v0.1 — MVP: PTT

| # | Task | Document |
|---|---|---|
| 1.1 | `config.py` with full validation + `config.example.toml` + `whisper-server.env` generation | 09 |
| 1.2 | `logging_setup.py` (TRACE, journald format), `sdnotify.py` | 12, 11 |
| 1.3 | `events.py`, `interfaces.py`, `controller.py`—IDLE / PTT_RECORDING states, `engine`, “Any state” rows | 04 |
| 1.4 | `engine_monitor.py` | 04 §4.5 |
| 1.5 | `audio/recorder.py` with a limit and start-sound masking | 05 §5.2–5.3 |
| 1.6 | `pipeline.py` (worker, RMS gate, generations, retry, pause on DOWN, timings) | 04 §4.4–4.5 |
| 1.7 | `text/filters.py`, `text/processor.py` (without step 5—continuous-mode continuity) | 08 §8.2, 06 §6.8 |
| 1.8 | `audio/consumer.py`; `inject/x11util.py`, `inject/clipboard.py`, `inject/xdotool.py`, `inject/auto.py` | 05, 08 |
| 1.9 | `hotkeys/spec.py`, `hotkeys/x11.py` (PTT + Esc) | 07 |
| 1.10 | `ipc.py` + CLI commands marked v0.1 in [10](10-cli-ipc-status.md) §10.1 (including grouped `reload` and server restart; also `devices`, which no task listed — user decision 2026-10-03; `daemon` comes with `app.py` in 1.13, `doctor` in 1.12) | 10, 04 §4.6 |
| 1.11 | `feedback.py`—sounds according to the matrix + error notifications (`notify-send -p/-r`) | 10 §10.6 |
| 1.12 | `doctor.py` | 10 §10.5 |
| 1.13 | `app.py`—composition, start/stop, signals, `threading.excepthook` | 02, 12 |
| 1.14 | `systemd/*.service`, `install.sh` steps 5–9, `uninstall.sh` | 11 |
| 1.15 | `audio/file_source.py` (`FileAudioSource`, required for E2E) + tests: 14.2 unit tests for the items above, `needs_whisper`, `needs_x11`, PTT E2E | 14 |

**v0.1 acceptance:** completed 2026-10-04, results in [acceptance-v0.1.md](acceptance-v0.1.md).

- the complete 14.4 (v0.1) checklist,
- N1 (RAM), N2 (PTT latency—measure `total` from `timings` logs for at least 20 dictations per tested configuration, including the full injector time; selection approved under 13 §13.5), N5, N8, and N9.

Preliminary-design requirements covered by v0.1:

- Ubuntu, microphone, whisper.cpp, benchmark-selected model, Polish,
- PTT, post-recording transcription, insertion into the active window, global hotkey,
- logging, configuration, systemd user service.

## v0.2 — Continuous dictation

| # | Task | Document |
|---|---|---|
| 2.1 | `audio/vad.py` (Silero ONNX) — **done in v0.1** (brought forward during the v0.1 acceptance, 2026-10-03) | 05 §5.4 |
| 2.2 | `audio/segmenter.py` with hysteresis and cutting at `max_segment_s` | 05 §5.5 |
| 2.3 | Controller: CONTINUOUS state, toggle with flush, cancel, backlog, reconnect, `EngineStateChanged(DOWN)`, deferred reload | 04 §4.3, §4.6 |
| 2.4 | Pipeline: per-session prompt context; TextProcessor step 5 (continuity) | 04 §4.4, 08 §8.2 |
| 2.5 | Hotkey continuous toggle; CLI `toggle`, `status --watch` (IPC `subscribe`) | 07, 10 |
| 2.6 | VAD trimming for PTT (replaces the RMS gate when `vad.enabled`) — **done in v0.1** together with 2.1: the RMS gate could not reject room noise ([acceptance](acceptance-v0.1.md#finding-the-rms-gate-cannot-reject-silence)) | 05 §5.3 |
| 2.7 | Audio error handling: reconnect ×3, overflow, digital silence | 05 §5.6 |
| 2.8 | systemd `STATUS=`; `all` notifications | 11, 10 |
| 2.9 | `bench --soak` (using v0.1's `FileAudioSource`) | 13 §13.4 stage 3 |
| 2.10 | Tests: segmenter, continuous controller, continuous E2E | 14 |

**v0.2 acceptance:**

- the 14.4 (v0.2) checklist,
- a 10-minute battery-powered soak test satisfies N3; incorrect-segmentation report (13 §13.3),
- N4 (CPU during silence) measured with `pidstat -p <pid> 1 60`,
- final model defaults recorded in `docs/benchmark-results.md` and `config.example.toml`.

Status: checklist completed 2026-10-04, results in [acceptance-v0.2.md](acceptance-v0.2.md); open: the incorrect-segmentation report, and N3 holds only on AC with the `performance` profile.

Preliminary-design requirements covered by v0.2: continuous dictation, VAD, automatic segmentation, automatic insertion of successive segments, daemon status, and audio error handling.

### v0.2 follow-ups — external code review (2026-10-04)

A static review of `a23c111` reported four defects; each was confirmed by reading the code (not reproduced at runtime). Targeted fixes only: E14 stays as it is, and the pipeline gets no catch-all for unexpected exceptions (user decision 2026-10-04). Each fix comes with a test that reproduces the defect.

| # | Task | Severity |
|---|---|---|
| R1 | **Replacement template not validated:** `config.py` checks only the regex pattern, not `replace`. A pattern `(a)` with `replace = "\2"` passes validation, and `re.sub` then raises `re.error` on **every** text, even without a match (checked in Python). The pipeline thread crashes, E14 ends the process, and after the systemd restart the first dictation crashes it again. Fix: the validator runs `re.sub(pattern, replace, "")`, which parses the template without a match (Python 3.12 raises `IndexError`, not `re.error`, for an unknown group name), so a bad reload is rejected and the previous config is kept | high |
| R2 | **Truncated HTTP response kills the daemon:** `WhisperServerEngine._request` maps only `TimeoutError` and `OSError`; `http.client.IncompleteRead` (and other `http.client.HTTPException`s that are not `OSError`) escapes `EngineError` handling in the pipeline, and E14 ends the process, losing queued audio. Fix: `IncompleteRead` → `EngineConnectionError` (requeue, as for a dropped connection), other `HTTPException`s → `EngineResponseError`; `health()` uses the same request and no longer crashes the monitor thread either. Test: a server that declares a larger `Content-Length`, sends part of the body and closes | medium |
| R3 | **Server-group reload does not reach the pipeline:** after a restart, `switch_server` (`app.py`) swaps the engine and the controller updates its `config.stt`, but `pipeline.update_config` is called only for the `live` group. Changing only `stt.language` (the only server key the pipeline reads; its timeouts are live keys) leaves the pipeline on the old value. Fix: the controller passes `use_server` its effective config (the restart's server keys + live keys reloaded in the meantime, so a later live reload is not reverted), and `ComponentReloader.use_server` re-applies the live components, the pipeline among them. Test: the language of the next transcription request, not just the restart call | medium |
| R4 | **Microphone stream not always closed on errors (`capture.py`):** if `stream.start()` fails, the created `InputStream` is not closed; if `stop()`/`abort()` raises, `close()` is skipped although `self._stream` is already cleared. Fix: attempt `close()` in `finally` on both paths. Test: an error raised after the stream was created | low |

Status: R1–R4 fixed 2026-10-04; each new test fails on the old code and passes with the fix (full suite: 802 passed before the last test-only lint fix, ruff and mypy clean). Live check 2026-10-04 after `install.sh --no-apt` (`doctor` 17 OK): R1 — a `(a)` → `\2` rule makes `local-stt reload` exit 78 with the validator message, same daemon PID; R3 — after a reload of only `stt.language` pl → en, Polish PTT speech came out in English (job 1), then restored to pl. R2 and R4 cannot be provoked safely on the live daemon and are covered by unit tests only. Spec updated in [04](04-state-machine.md) §4.6, [06](06-stt-engine.md) §6.5, [09](09-configuration.md).

## v0.3 — Quality and control

The preliminary design scope (“partial transcription, result stabilization, improved context management, model switching, CPU/latency benchmark”) becomes the following ADR-010-compatible tasks:

| # | Task | Notes |
|---|---|---|
| 3.1 | **Model switching:** changing `stt.model` + `local-stt reload` already works in v0.1 (server restart, [04](04-state-machine.md) §4.6). v0.3 adds only `local-stt models list --bench`, a model list with the latest benchmark results (WER, p90 latency, RAM) for an informed choice. No runtime override—the config is the single source of truth (ADR-008) | 06 §6.5, 13 |
| 3.2 | **Moved to the backlog** (item 9, user decision 2026-10-06). ~~**Partial preview, without insertion:**~~ in continuous mode, once an utterance lasts > 4 s, send the accumulated audio to the engine every 2 s (same `audio_ctx` policy as final jobs, 06 §6.7), **but only when the final-job queue is empty and the worker is idle**. The result goes only to `local-stt status --watch --preview`, when `continuous.preview = true` and at least one explicit preview subscriber exists. Never send it to a window or notification; ordinary `status`, `status --watch`, and `job` events still contain no text. Add the new `continuous.preview = false` key, CLI flag, and preview subscription to [09](09-configuration.md) and [10](10-cli-ipc-status.md) in v0.3 | ADR-010 |
| 3.3 | ~~**Segment-boundary stabilization:**~~ **rejected after measurement**, see the status below. `max_length` cuts with 1 s of audio overlap + removal of duplicate words at the join (longest common word suffix/prefix ≥ 2) | 05 §5.5 |
| 3.4 | **Context:** tune `continuous_context` (tail length; reset after `min_silence` > 5 s = new paragraph) using the `long/` corpus | 06 §6.6 |
| 3.5 | **Not needed** (closed 2026-10-06, status below). `stt.continuous_model` (second server)—**only if** required by the rule in 13 §13.5 | ADR-016 |
| 3.6 | **Closed, moved to v0.4** (task 4.7, user decision 2026-10-07). ~~Full benchmark report (`bench report`) with thermals; update `docs/benchmark-results.md`~~ | 13 |
| 3.7 | **Language switch hotkey (cycles `stt.languages`)**, design below (user decisions 2026-10-05) | 04, 06, 07, 09, 10 |

Status: 3.1 done 2026-10-04 (`models list --bench`, details in [10](10-cli-ipc-status.md) §10.1); checked on the stored runs — the values match [benchmark-results](benchmark-results.md).

Status: 3.3 implemented and measured 2026-10-06, then **rejected** (user decision 2026-10-06); the code was not kept. Variant tested: the remainder of a `max_length` split started with the last 1 s of the emitted part (clipped for tiny limits, not counted as speech for rule 7), and TextProcessor dropped the longest prefix of ≥ 2 words equal to a suffix of the session's text (case and punctuation ignored). Measurement: one pass of corpus A `long/001.wav` (497 s) through the soak chain (`small-q8_0`, 4 threads, `audio_ctx` 1000, `continuous_context` on), 59 segments, 9 `max_length` cuts, identical segmentation in both runs; WER against `long/001.txt`: **16.6 % (128/771) without the overlap, 17.6 % (136/771) with it**. The removal never fired: with the session text in the prompt, Whisper continued it and skipped the repeated audio instead of transcribing it again. 5 joins came out the same (cuts in silence), 3 got worse (a loop "…która poznała Bronisław…", "obra Zob | z o burczej" instead of "obrazob | burczej", "…ma… …Mendeliewa" instead of "mendelejeva"), 1 changed slightly. Small sample (one recording, 9 joins), but the direction is clear. Untested alternative, not pursued: no prompt tail for the segment after a `max_length` cut, so the overlap is transcribed and then removed.

Status: 3.4 done 2026-10-06 — **no change to the production policy** (user decision 2026-10-06): the tail stays 200 characters and the new-paragraph reset is implemented but off. New: `AudioSegment`/`Job.pause_before_s` (05 §5.5), `PipelineWorker(context_chars, context_reset_s)` with `CONTEXT_RESET_S = None`, and `bench --context` (13 §13.4). Measured with `bench --context` (`small-q8_0`, 4 threads, `audio_ctx` 1000, AC, `performance`, a fresh server per policy; results in `~/.local/share/local-stt/bench/context-2026-10-06/`):

| Recording | Policy | WER (errors / 771) | raw WER (errors / 766) |
|---|---|---|---|
| `long/001` (497 s, 59 segments, longest pause 3.4 s) | no context | 16.86 % (130) | 28.20 % (216) |
| | 100 characters | 16.34 % (126) | 28.33 % (217) |
| | **200 characters (default)** | 16.60 % (128) | 28.59 % (219) |
| | 300 characters | 16.21 % (125) | 28.46 % (218) |
| `long/001` + pauses (628 s, 57 segments) | 200, **no reset** | **15.56 % (120)** | **27.81 % (213)** |
| | 200, reset after > 5 s | 16.47 % (127) | 28.33 % (217) |

Decision rule (agreed before the runs): change the default only for > 1 point of WER, or a clear raw-WER gain without a WER loss. The tail length moved WER by at most 0.65 points, even with no context at all, so 200 stays. The reset lost 7 words: after the 60 s pause it hallucinated a clause ("…z rodziny, który w tym roku wchodzili z rodziną"), and a paragraph start lost "W Szczółkach"; the other differences went both ways. The pause recording is synthetic: room noise from the recording's longest natural pause (360.4–362.4 s, tiled with 50 ms crossfades) inserted at the 7 paragraph boundaries for pauses of 6, 30, 4, 60, 12, 8 and 20 s (re-segmented: 6.1, 30.1, 4.0, 60.0, 13.6, 8.0, 20.5 s; the 60 s of noise produced no false segment); script `make_pauses.py` in the results directory. Limits: one text (one biography, so the previous paragraph is relevant context), synthetic pauses, small differences. Found while building the pause recording: `pause_before_s` after a `max_length` split whose rest was dropped counted from an older segment (88 s instead of ~1 s); fixed before the measurement, with a test.

Status: 3.5 closed 2026-10-06 as **not needed**. The 13 §13.5 rule asks for a second server only when the default model fails the soak test **and** a faster production model passes it with a WER gap > 3 pp. On AC with the `performance` profile `small-q8_0` passes (RTF 0.33, queue slope −0.115 s/min, [acceptance-v0.2](acceptance-v0.2.md)), so the rule is not triggered. It fails on `power-saver` (RTF 1.99, the stand-in for the missing battery), but no production model is fast enough to change that: `base-q5_1` is excluded from production, and the runner-up `small-q5_1` is slower than `small-q8_0` ([benchmark-results](benchmark-results.md), stage 2, t=4, `audio_ctx` 1000: p90 `text_ready` 5.07 vs 3.67 s, RTF 0.84 vs 0.60). This is a deduction from those numbers, not a `power-saver` soak of `small-q5_1`. N3 holds on AC `performance` only, as accepted for v0.2 (user decision 2026-10-04).

Status: 3.6 closed 2026-10-07 without a new run (user decision 2026-10-07). The whisper.cpp matrix on corpus A is already in [benchmark-results](benchmark-results.md) (2026-10-03, same corpus, matrix and whisper.cpp v1.9.4), and the soaks are in [acceptance-v0.2](acceptance-v0.2.md) (2026-10-04). v0.4 replaces the default engine (ADR-018), so a fresh report on Whisper would describe the outgoing default; the full report with thermals moves to task 4.7.

**Task 3.7 design — language switch hotkey** (user decisions 2026-10-05). Basis: whisper-server honours the per-request `language` and `stt.language` is already a live reload key (06 §6.5).

- **State:** the daemon holds the active language in memory; the first of `stt.languages` is the startup language. The daemon never writes `config.toml` (ADR-008), so a restart returns to it. A `reload` that changes `stt.languages` resets the active language to its first entry; any other reload keeps it.
- **Languages:** one list `stt.languages = ["pl", "en"]` (first = startup, codes as before, no repeats, live reload key) replaces `stt.language`; the switch moves to the next entry and wraps around. User decision 2026-10-05, after a first implementation with `stt.alt_language`: a single second language was a design mistake, because adding a third later would need a config migration. An old config with `stt.language` is rejected with a hint (`stt.language: replaced by stt.languages, …`), not aliased.
- **Key:** new `hotkeys.language_toggle = "Ctrl+Control_R"` (`""` = no hotkey): left Ctrl first, then right Ctrl; right Ctrl first starts PTT. The first design used `Alt+Control_R`; live in VS Code the bare Alt activated the menu bar and the editor lost focus, so the default became `Ctrl+Control_R`, which kept focus in VS Code (user decision 2026-10-05; details in [07](07-hotkeys-x11.md) §7.1).
- **CLI/IPC:** `local-stt language [toggle|<code>]`, where `<code>` must be in `stt.languages`; without an argument it prints the active language. Works without hotkeys (07 §7.7).
- **When it applies:** a job records the language active when its recording **started** (`Job.language`; continuous: when its segment arrived), so jobs already recorded or queued keep theirs; a switch while PTT is held applies to the next recording. In continuous mode a switch ends the session context (like a new paragraph): the prompt tail of the other language is dropped.
- **Feedback:** sound `language` (one 880 Hz tone: the startup language) or `language_alt` (two: any other, user decision 2026-10-05; the notification names it), queued like the other sounds (10 §10.6) and played **only while the microphone is closed** (added during implementation: in continuous mode the tone would be recorded); a notification “Language: EN” shown for `notifications = "errors"` and `"all"` (not for `none`), replacing the previous one.
- **Status:** `status` text line `language   en (languages: pl, en)`; `--json` field `"language": {"active": "en", "languages": ["pl", "en"]}`; a `language` event in the `subscribe` stream so `status --watch` can show it.
- **English hallucination filters:** add common Whisper English hallucinations (e.g. “Thank you for watching.”, “Thanks for watching!”, “Subtitles by the Amara.org community”) to the default `text.hallucination_patterns`; they are matched only as whole-utterance patterns, like the Polish ones. `stt.vocabulary_prompt` stays shared (Polish vocabulary in English mode is a known limitation, not addressed here).
- **Tests:** controller (toggle, job keeps its language, reload reset rules, continuous context reset), hotkey integration (Alt-first order), IPC/CLI, filters; live: switch during PTT and continuous mode.

Status: 3.7 implemented 2026-10-05; specs updated in [04](04-state-machine.md) §4.6, [06](06-stt-engine.md) §6.6, [07](07-hotkeys-x11.md) §7.1–7.2, [09](09-configuration.md), [10](10-cli-ipc-status.md) §10.1–10.6. Live test 2026-10-05 on the reinstalled daemon: CLI `language` show/toggle/reject (`de` → code 4)/select; switch, sounds and notification during PTT and continuous mode (no sound while recording); `Alt+Control_R` lost focus in VS Code, `Ctrl+Control_R` did not.

**v0.3 acceptance:**

- the 14.4 (v0.3) checklist,
- continuous-mode WER on the `long/` corpus is no worse than in v0.2 (the duplicate-boundary-word criterion was dropped with task 3.3, the preview-latency one with task 3.2).

Status: checklist completed 2026-10-07, results in [acceptance-v0.3.md](acceptance-v0.3.md); `long/001` WER 16.60 %, equal to v0.2.

## v0.4 — Faster engine (Parakeet)

Basis: ADR-018 (user decisions 2026-10-07). Goal: text appears sooner after the end of speech; ADR-010 (inject only final segments) stays.

| # | Task | Notes |
|---|---|---|
| 4.1 | **Library choice:** run Parakeet TDT 0.6B v3 int8 through both `sherpa-onnx` and `onnx-asr` on corpus A (WER, p50/p90, RSS). Take `onnx-asr` if it is within 0.5 pp WER and 10 % latency of `sherpa-onnx` (it also runs Canary, the candidate fallback), otherwise `sherpa-onnx` | measured with sherpa-onnx only so far (ADR-018) |
| 4.2 | **Engine service** `local-stt-engine.service`: a small Python server holding the model, loopback-only with the random request path from the `secret` (as ADR-002), `health` + `inference` mirroring the subset of 06 §6.5 the daemon uses; systemd restart policy (N9) | ADR-002, 06 |
| 4.3 | **`ParakeetEngine`** in `local_stt/stt/` behind `SttEngine` (N7); `stt.engine = "parakeet"` (default) or `"whisper-server"`; switching engines on `reload` stops one service and starts the other. `language` and `prompt` are ignored by Parakeet: document it, and decide whether the language hotkey is disabled or kept for whisper-server only | 06 §6.9, 09 |
| 4.4 | **Text filtering:** add Parakeet's non-speech fillers ("Yeah.", "Mm.", "Mm-mm.") as whole-utterance patterns; count outputs with non-Latin letters (Cyrillic) in the log and `status`, but inject them unchanged — the count is the evidence for backlog item 10 | ADR-017, 06 §6.8 |
| 4.5 | **Install, models, doctor:** `install.sh` downloads the model with a pinned sha256 (`models.sha256`), installs the service; `doctor` checks the service and model; `models list --bench` and `bench` accept the new engine | 06, 11, 13 |
| 4.6 | **Spec updates:** N1 (RAM per engine), N5 (the new listener), 02 architecture, 06 (new engine section), 09 (`stt.engine`, model keys), 13, 14 | 01, 02, 06, 09, 13, 14 |
| 4.7 | **Full benchmark report** (former task 3.6): `bench` and `bench report` for the new default and `small-q8_0` on corpus A, with thermals; update `docs/benchmark-results.md` | 13 |
| 4.8 | **Parakeet memory on long recordings:** examine the engine server RSS for PTT recordings up to `ptt.max_duration_s` (120 s). Then select one fix: the onnxruntime arena off, higher N1 and `MemoryMax`, or long recordings sent in parts | 01, 06, 11 |
| 4.9 | **`bench --soak` for Parakeet:** the soak runs the default engine, so the v0.4 acceptance can measure N3 | 13 |

Status: 4.1 measured 2026-10-07: **`onnx-asr` chosen** by the rule above. Setup: `sherpa-onnx` 1.13.8 with the k2-fsa int8 export, `onnx-asr` 0.12.0 (onnxruntime 1.30.0) with the HF `istupakov/parakeet-tdt-0.6b-v3-onnx` int8 export; 4 threads; corpus A (40 files); one process per library with a warm-up, run order sherpa, onnx-asr, sherpa, onnx-asr, 3 repetitions each (6 per library); daemon stopped; AC, `performance`, no extra cooling (CPU 86–87 °C mean, peaks 98 °C, so latencies are throttled). Results:

| | `sherpa-onnx` | `onnx-asr` | difference |
|---|---|---|---|
| WER (identical in all 6 repetitions) | 5.73 % | 5.57 % | −0.16 pp |
| medium p50 / p90 | 0.93 / 1.15 s | 1.01 / 1.24 s | +8 % / +8 % |
| all files p50 / p90, mean RTF | 0.91 / 2.48 s, 0.125 | 0.98 / 2.59 s, 0.136 | +7 % / +5 %, +9 % |
| RSS after load / peak | 0.84 / 1.23 GB | 1.13 / 1.55 GB | +0.3 GB |
| load | 2.7–2.9 s | 2.4–2.9 s | — |

The two exports differ, so 10 of 40 transcripts differ (in both directions; neither is consistently better). The latency gap is within the rule but not small: the second `onnx-asr` run (medium p50 0.94 s) matched `sherpa-onnx` (0.93 s), the first did not (1.08 s), so throttling moves it by several percent. The rule did not cover memory: `onnx-asr` costs ~0.3 GB more RSS, which counts for N1 (task 4.6) and for the `resident` mode of backlog item 10. Script, raw results and summary: `~/.local/share/local-stt/bench/libchoice-2026-10-07/` (outside the repo). The user confirmed `onnx-asr` on 2026-10-07 knowing the memory cost.

Status: 4.2 implemented 2026-10-07 (user decisions: same package and venv as the daemon; the whisper-server HTTP subset as the protocol). `local-stt engine-server` (`src/local_stt/engine_server.py`) loads the model from `stt.models_dir/parakeet-tdt-0.6b-v3-int8/` with `stt.threads`, then listens on `127.0.0.1:stt.port` under the `secret` request path and sends `READY=1`; one inference at a time, `/health` answers during one; the reply is `verbose_json` with one segment and null confidences (so the no-speech filter, 06 §6.8 rule 1, never applies); `language` and `prompt` are ignored; exit 78 without a secret or model. Unit `systemd/local-stt-engine.service` (`Type=notify`, `MemoryMax=2500M`, `RestartPreventExitStatus=78`); dependency `onnx-asr` 0.12.0 in `requirements.lock`. Tests: 14 unit tests drive the server with the unchanged `WhisperServerEngine` client; 1 `needs_parakeet` integration test with the real model. Live check 2026-10-07 as a transient `Type=notify` user unit on port 8179: READY after 3.2 s (model load 2.9 s), the three fixtures (4.2–4.7 s) transcribed correctly in 0.54–0.72 s, RSS 1.19 GB, clean stop on SIGTERM. Left to later tasks: installing and enabling the unit and the model download (4.5; the model was copied by hand into `~/.local/share/local-stt/models/` for the test); both units are `WantedBy=graphical-session.target` and share `stt.port`, so only the selected engine's unit may be enabled (4.3/4.5); the `engine-server` command and `needs_parakeet` marker in docs 10 and 14 (4.6).

Status: 4.3 implemented 2026-10-07. User decisions 2026-10-07: the language hotkey and `local-stt language` are rejected under Parakeet; the default stays `whisper-server` until 4.5 can download the model; the daemon, not systemd, runs the selected engine's unit. Changes: `stt.engine` accepts `"parakeet"`; `ParakeetEngine` (`stt/parakeet.py`) is `WhisperServerEngine` with Parakeet's name and model that never sends the prompt; registry `ENGINES` + `ENGINE_UNITS`; startup checks the Parakeet model directory instead of `stt.model`. Engine units: at startup the daemon stops the other engine's unit and `start`s the selected one in a helper thread (not waited for, 11 §11.5); a server-group reload stops the other and `restart`s the selected one, and writes `whisper-server.env` only for whisper-server. `local-stt.service` lost `Wants=local-stt-whisper.service`; `install.sh` installs `local-stt-engine.service`, enables only `local-stt.service` and disables both engine units (11 §11.3 step 8 and §11.5 updated, since a test pins the unit to the spec); `uninstall.sh` removes the new unit. Under Parakeet the language switch answers `language_unsupported` (CLI exit 4) with the notification "Language: automatic (Parakeet)"; `status` shows language `auto` (hidden in `status --watch`), engine `parakeet` and its model; log lines and the `reload` message name the selected engine. Tests: 808 unit tests pass (new: Parakeet client contract, registry, model-directory check, language rejection and status, unit switching order, systemctl exit codes), integration tests of both engines pass, ruff and mypy clean. Live check 2026-10-07 after `install.sh --no-apt`: `reload` whisper → parakeet stopped whisper and started the engine unit, READY 8.4 s after the reload (model load 7.3 s on the first, cold read; 5.1 s on the next start; 2.9 s in 4.2); `local-stt language toggle` → exit 4; a daemon restart with `engine = "parakeet"` started Parakeet itself; `reload` back to whisper → READY in 2.0 s, `doctor` 17 OK. Not done here: `doctor` still checks only `local-stt-whisper` and reports FAIL under Parakeet, and `transcribe`/`bench` know only whisper-server (4.5); specs 04 §4.6, 06 §6.9, 09 (`stt.engine`), 10 (`language_unsupported`, `engine-server`) and an 11 section for the new unit (4.6).

Status: 4.4 implemented 2026-10-07. User decisions 2026-10-07: the fillers are built in and apply only to Parakeet (the user's own `text.hallucination_patterns` list replaces the default one, so new default patterns would not reach it, and whisper-server output "Yeah." must stay); every non-Latin script is counted, not only Cyrillic. Measurement first: the filler list in ADR-018 came from sherpa-onnx on "quiet windows" of the long corpus recording, but 19 of the 20 quietest 3 s windows (−28 to −20 dBFS) pass the VAD gate and contain speech (continuous reading has no real pauses), so that evidence was speech, not silence (ADR-018 to be corrected in 4.6); synthetic silence and white noise (−60 to −20 dBFS) give empty output. The user then recorded 8 non-speech takes of 12 s (`~/stt-corpus-nonspeech/`): the VAD gate dropped room tone, breath, loud breath, keyboard, mouse and desk, and chair; the cough gave "Cool.", humming "Hm", "Mm.", "Um", "Mm, mm, um". Rule 5 of 06 §6.8 drops a result made only of fillers (non-words, or one of yeah/cool/so alone); the non-Latin warning and `stats.jobs_non_latin` are in 06 §6.8 and 10. Tests: 841 unit tests pass (fillers kept inside sentences, Polish letters are Latin, processor only for Parakeet, counter, warning without the text unless `logging.log_text`). Live check 2026-10-07 on the reinstalled daemon with Parakeet, the user dictating: humming injected nothing; 1 of 3 takes of "Po code review zrób rebase i force push." came out in Cyrillic, was injected unchanged, logged without its text and counted (`jobs_non_latin` 1 of 11 jobs); latency from key release to injection 0.47–0.82 s (`total`, 11 PTT jobs of 1.5–5.1 s; v0.1 whisper p90 3.70 s). Observed: very short utterances (spelled letters "E T C") were first taken as English ("Eh, good sir. …"), and mixed Polish-English sentences stay weak, as ADR-018 expected. Measurement scripts and output: `~/.local/share/local-stt/bench/fillers-2026-10-07/` (outside the repo).

Status: 4.5 implemented 2026-10-08. User decisions 2026-10-07: Parakeet becomes the default engine (`stt.engine = "parakeet"` in `config.py` and `config.example.toml`; an existing config file is kept), and `install.sh` downloads both the Parakeet model and `--model` (the whisper-server fallback, default `small-q8_0`); `bench`, `transcribe` and `models list --bench` accept Parakeet, `--soak` and `--context` stay Whisper-only. Changes: the model registry handles a directory model: `models.sha256` pins the four files of `istupakov/parakeet-tdt-0.6b-v3-onnx` at revision `8f23f0c0` (LFS sha256 = the local copy from 4.2; the two small files hashed after download), `models pull parakeet-tdt-0.6b-v3-int8` installs the files only after all of them verify, `list`/`verify` report the directory as one model; `install.sh` step 6 pulls it (11 §11.3 step 6 updated). Found in the live download: the Hugging Face CDN dropped one of three 652 MB encoder transfers (curl got 31.7 MB), and `urllib`'s `read(n)` returns EOF silently, so the old code reported a checksum mismatch; downloads now check Content-Length and resume with `Range` (3 attempts, start over if the server ignores Range); this applied to the Whisper models too. `doctor` checks the selected engine's unit, health and model (every pinned file); `local-stt engine-server` takes `--port`, `--threads`, `--model-dir` and the request path from `LOCAL_STT_REQUEST_PATH`, which `TemporaryParakeetServer` (`transcribe --model`, `bench`) uses; `bench` runs Parakeet once per thread count (ctx 0, greedy, no beam or whisper-bench), refuses to run while either engine unit is active, records the onnx-asr version and includes Parakeet in the default `--models`; the report labels Parakeet `t=N` and treats ctx/beam as matching the config. Tests: 867 unit tests pass (new: directory model pull/status/atomicity, resume after a dropped connection, Range ignored, give-up after 3, engine-server overrides and bind failure, temporary server command, bench matrix and run, report matching, transcribe routing, doctor per engine); tests written for whisper-server defaults now pin `engine = "whisper-server"`; `needs_parakeet` integration tests pass (new: temporary server in its own process); ruff and mypy clean. Live check 2026-10-08: a fresh `models pull` into an empty directory (2 min, checksum OK; that transfer did not drop, so resume is covered by unit tests only); `install.sh --no-apt` step 6 "already present" for all three, `doctor` 17 OK under Parakeet (FAIL in 4.3); `transcribe` via the service, `--model parakeet-tdt-0.6b-v3-int8` and `--model small-q8_0` all correct; `models list --bench` marks Parakeet as selected and not measured; `bench --quick` with Parakeet and base-q5_1 (1 repeat, medium group, `powersave`, results in a scratch directory only) ran and rendered (Parakeet WER 5.7 %, p50 1.33 s, peak RSS 1268 MB; not a benchmark result). Left for 4.6: README (still says whisper.cpp and `small-q8_0` as the default), 06 §6.3 (directory model, resume), 10 (`engine-server` options, `transcribe --model`, `doctor` per engine, `models list` output), 13 (Parakeet in the bench matrix).

Status: 4.6 done 2026-10-08. User decisions 2026-10-08: N1 for the Parakeet engine server is ≤ 1.6 GB RSS (the measured peak is 1.55 GB); the 14.4 checklist gets a v0.4 draft; new and changed doc text follows the ASD-STE100 writing rules (now a rule in `CLAUDE.md`). Changes:

- 01: the two engine processes, N1 per engine, N5 and N6 for both engines.
- 02: the diagram, the `engine-unit` and `server-restart` threads, and the repository tree (`engine_server.py`, `stt/parakeet.py`, the new unit; the tree had a `stt/fake.py` that does not exist).
- 03 ADR-018: `onnx-asr` (task 4.1), RAM, and a correction of the filler evidence (task 4.4).
- 04 §4.6: the server restart and the language switch under Parakeet.
- 06: the title, §6.3 (directory model, resume), §6.9 (registry), new §6.10 (Parakeet engine server).
- 09: `stt.engine`, new §9.2a (keys per engine), the Parakeet model-file rule.
- 10: `engine-server`, `language`, `models`, `transcribe` and `bench` rows; Parakeet status; `doctor` per engine.
- 11: file layout, `onnx-asr`, §11.4 with `local-stt-engine.service`, uninstall, operations.
- 13: Parakeet in the matrix, the service check, N1 per engine in the decision rule.
- 14: the `needs_parakeet` marker, the v0.4 unit tests, the v0.4 checklist draft.
- README, `docs/README.md` and `config.example.toml` comments.

Found while writing: PTT recordings can last 120 s (`ptt.max_duration_s`), but Parakeet was tested only up to 24 s (ADR-018 now says so). The README status still names only the v0.1 and v0.2 acceptances and version 0.0.1.

Status: 4.7 done 2026-10-08. User decisions 2026-10-08: the matrix has the two v0.4 engines only (Parakeet and `small-q8_0` @1000, each with 4 and 8 threads); external cooling on and browsers closed; the new results go on top of [benchmark-results](benchmark-results.md), and the 2026-10-03 results stay below. Run `2026-10-07T22-56-03Z`, corpus A, 3 repetitions, AC, `performance`. Then `small-q8_0` t=4 at the full window was added to the same run, because the audio_ctx rule of 13 §13.5 needs it. Results (stage 2): Parakeet t=4 WER 5.6 %, p90 `text_ready` 0.93 s, peak RSS 1585 MB; `small-q8_0` t=4 @1000 WER 7.4 %, p90 2.80 s. The report selects Parakeet t=4, which is the current default. With 8 threads, Parakeet is slower (p90 1.23 s) and is more than N1 (2188 MB). The Parakeet RSS margin to N1 is 3 %. Found while writing the report: `bench report` used the 1 GB whisper-server N1 limit for every engine, so it excluded Parakeet. Fix: an N1 limit for each engine (13 §13.5), `stt.engine` in the provisional default, and a line when no configuration is left; 3 new unit tests. Not done here: N3 for Parakeet, because `bench --soak` is Whisper-only (task 4.5) and the v0.4 acceptance needs a Parakeet soak.

Follow-up measurement 2026-10-08 (the user asked to raise N1 to 1800 MB; scripts and results in `~/.local/share/local-stt/bench/rss-2026-10-08/`, outside the repo). The Parakeet RSS increases with the recording length. A fresh engine server with 4 threads had a peak of 1268, 1380, 1563, 2015 and 2096 MB for 15, 30, 60, 90 and 120 s of `long/001.wav`. The memory stays allocated after the request. In one process, the 16 medium files, then 60 s and 120 s gave a peak of 2529–2540 MB (2 runs). That is more than `MemoryMax=2500M` (2384 MB) of `local-stt-engine.service`. Possible effect, not tested on the unit: systemd stops the engine during a long PTT recording. With `enable_cpu_mem_arena = False` the peak was 1912–1973 MB, and the RSS went back to ~800 MB after each request. All transcripts were identical. The medium p50 was 0.94–0.97 s instead of 0.84–0.94 s, 60 s took 8.1 s instead of 6.6–7.1 s, and 120 s took 24 s instead of 16.4 s. N1 stays 1.6 GB for now (user decision 2026-10-08): task 4.8 examines this and selects the fix.

Status: 4.8 done 2026-10-08. User decisions 2026-10-08: compare the arena variants first, then shrink the arena only after long recordings; N1 for Parakeet 2.2 GB and `MemoryMax=3000M`. Scripts and results: `~/.local/share/local-stt/bench/rss-2026-10-08/` (outside the repo).

- **What happens at the old limit.** On the installed unit (`MemoryMax=2500M`): 16 medium files, then 60 s, then 120 s three times. systemd did not stop the server (no OOM kill, no restart). The cgroup reached its limit 1007 times, the kernel dropped the file cache and moved 576 MB of the server to swap. The unit has no swap limit. All transcripts were complete.
- **Variants** (in-process, medium group, then 60 s and 120 s, 2 runs each, interleaved, daemon stopped; all transcripts identical): the current arena had medium p50 0.82 s, 120 s in 16.0 s, a peak of 2.55 GB, and kept 2.55 GB. The arena off had 0.94 s, 23.9 s, a peak of 1.93–1.98 GB, and kept ~1.1 GB. A shrink after every request had 0.93–0.96 s, 16.5 s, a peak of 2.30–2.33 GB, and kept ~1.2 GB. A shrink only after more than 30 s had 0.83–0.86 s (current arena in the same run: 0.76–0.83 s; short requests use the same code path), 120 s in 14.8 s (+1–3 %), a peak of 2.09 GB, and kept ~1.25 GB.
- **Change:** `engine_server.ShrinkingSession` wraps the encoder session. After an encoder input of more than 3000 feature frames (30 s; 100 frames/s, measured), it runs with `memory.enable_memory_arena_shrinkage = cpu:0`. onnx-asr 0.12 has no run-options parameter, so the server wraps the private `asr._encoder`; without it the server logs a warning and runs without the shrink. N1 for Parakeet is 2.2 GB (01, 13 §13.5, `bench report`) and the unit has `MemoryMax=3000M` (11 §11.4). Specs updated: 01, 03 ADR-018, 06 §6.10, 11, 13, 14, README.
- **Tests:** 872 unit tests pass (new: shrink only above the limit, other attributes pass through, a warning without `asr._encoder`; N1 per engine at 2.2 GB); the `needs_parakeet` test checks the real onnx-asr: `asr._encoder` exists, 29 s does not shrink and 31 s does. ruff and mypy are clean.
- **Live check** after `install.sh --no-apt` (doctor 17 OK): the same sequence as above, then four more 120 s recordings. No swap, no limit events, no restart. The RSS went back to 1.19–1.27 GB after each long recording. 120 s took 16.8–16.9 s. The peak was 2106 MB after the first 120 s recording and 2184 MB from the second on, which is 3 % below N1 (2253 MB).

Status: 4.9 done 2026-10-08 (user request 2026-10-08: N3 of the v0.4 acceptance needs it). `bench --soak` without `--model` uses `stt.engine_model`, thus Parakeet by default; `--model small-q8_0` keeps the Whisper soak. For Parakeet the soak starts a `TemporaryParakeetServer`, sets `stt.engine = "parakeet"` in its config (the filler rule applies, no prompt is sent), and records `audio_ctx = 0` and the engine; `--audio-ctx` with Parakeet gives exit code 2. `--context` stays Whisper-only. Specs: 10, 13 §13.4, 14. Tests: 872 unit tests pass (the CLI defaults for both engines, the `--audio-ctx` rejection; the old "Whisper models only" test is removed); ruff and mypy are clean. Smoke run (120 s, AC, `performance`, a scratch directory, not an N3 result): 18 jobs, all injected, RTF 0.10, VAD 1.7 % of a core, peak RSS 1316 MB, 99 °C. Its verdict was FAIL on the queue slope (+0.34 s/min): the queue held one segment (4.8–12.8 s of audio) for one sample of 1 s and was empty otherwise. Over 2 minutes, these spikes give a random slope. The 10-minute run has a 5-minute tail.

**v0.4 acceptance** (thresholds approved by the user 2026-10-07):

- corpus A WER with the default engine ≤ 7.4 % (the `small-q8_0` result),
- N2: p90 `total` from ≥ 20 PTT dictations of 4–10 s, target ≤ 2.0 s (v0.1: 3.70 s),
- N3: soak RTF ≤ 0.5 on AC `performance`, and a `power-saver` run recorded (it fails with Whisper),
- `stt.engine = "whisper-server"` still passes the v0.2 soak (no regression of the alternative).

Status: v0.4 checklist and criteria completed 2026-10-08, results in [acceptance-v0.4.md](acceptance-v0.4.md). All pass: corpus A WER 5.6 %; N2 p90 `total` 0.99 s (21 dictations of 4–10 s); soak RTF 0.09 on `performance` and 0.31 on `power-saver`; the whisper-server soak RTF 0.32. Findings, decided by the user 2026-10-08: `doctor` in `install.sh` ran before the engine was ready (fixed: step 9 waits for the engine unit); the N1 margin was 1.7 % (N1 for Parakeet is now 2.4 GB); the VAD share on `power-saver` is 4.9 % (no change).

## v0.5 — Commands

Basis: backlog item 1 (user decision 2026-10-08).

| # | Task | Notes |
|---|---|---|
| 5.1 | **Built-in spoken commands:** `text.commands = false` (opt-in) turns spoken signs (`:` `;` `–` `...`) and line breaks into text, as step 4a before `text.replacements` | 08 §8.2, 09 |
| 5.2 | **History in RAM:** `history.size = 10` texts; `local-stt last [N]` inserts one again, `local-stt history` lists them | backlog item 2; 10, 12 §12.2, 09 |
| 5.3 | **Clipboard-only mode:** `injection.backend = "clipboard-only"` puts the text in the clipboard and sends no keys; continuous segments of one session are joined | backlog item 4; 08 §8.4, 09 |

Status: 5.1 implemented 2026-10-09. User decisions 2026-10-08: a built-in set behind one key (not example rules to copy), default `false`, only the commands that Parakeet recognizes, and only the English commands that do not occur in ordinary speech. First test round (2026-10-08, 12 phrases, one take each, `~/stt-corpus-cmd/`, outside the repo): Parakeet wrote each command as words with its own punctuation (`dobry, przecinek. Jak`). It did not recognize “dwukropek” (“dwóch kropek”), “wykrzyknik” (“wygrzytnik”), “nowy akapit” (“I w nowym akapicie”) and “new paragraph” (empty result). The first set was przecinek, kropka, średnik, znak zapytania, question mark, nowa linia and new line. Live test 2026-10-09: the commands worked; “kropka kropka kropka” gave `. . .`; “trzy kropki” came once as “3 kropki”; Parakeet wrote “Krop.” once for “kropka”. The user then decided (2026-10-09): join signs from commands in a row, add “trzy kropki”, and keep the text layer but change the set. Reason: in dictation, pauses give commas and periods, and colons, semicolons and dashes are more useful. Second test round (2026-10-09, 10 phrases, `~/stt-corpus-cmd2/`): “myślnik” 5 of 5, “średnik” 4 of 4 (both rounds), “dwukropek” 2 of 5 (“dwóch kropek” 2 times, “dwuchrotek” once). Final set: dwukropek (+ “dwóch kropek”), średnik, myślnik (` – `), trzy kropki (+ “3 kropki”), nowa linia, new line (08 §8.2 step 4a). A result that ends with a line break gets no trailing space. In the user's terminal, the line break moved the cursor and did not run the command. Live test of the final set 2026-10-09 after `install.sh --no-apt`: one PTT dictation with all five commands gave the expected text. Tests: 903 unit tests pass (new: the Parakeet outputs of both rounds, case rules, line break rules, commands in a row, words that are not commands); ruff and mypy are clean. Open: a command spoken alone comes after the previous trailing space; the `type` backend with a line break is not tested.

Status: 5.2 implemented 2026-10-09. User decisions 2026-10-09: `history.size = 10` by default (0 = off), `local-stt last [N]` and `local-stt history`, every continuous segment is one entry, no own hotkey (a GNOME shortcut can run `local-stt last`). Test seams agreed before the tests: `TranscriptHistory`, `PipelineWorker`, IPC/CLI. Changes: `history.py` (a locked deque, newest first, live resize); the pipeline adds every processed text before injection, so a failed or cancelled paste stays in the history; `PipelineWorker.reinject` queues a job with source `history` that only injects (no engine, no text processing, no new history entry, not counted in `stats`); the controller answers `last` only at IDLE (`busy` otherwise, `no_history` for a missing number) and `history` with the texts; specs 04 §4.6, 09, 10 §10.1–10.2, 12 §12.2. Tests: 930 unit tests pass (new: history order, limit and resize; history after a failed paste; reinject; IPC parsing; controller replies and stats; CLI requests, output and exit codes), 65 integration tests pass; ruff and mypy are clean. Live test 2026-10-09 after `install.sh --no-apt`: `local-stt history` listed four PTT dictations, newest first; `local-stt last 2` pasted the second newest text into the editor.

Status: 5.3 implemented 2026-10-09. User decisions 2026-10-09: the segments of one continuous session are joined in the clipboard; a notification after each PTT recording and once for each continuous session; CLIPBOARD only (PRIMARY does not change); the mode is set only in the config file. Changes: `ClipboardOnlyInjector` (`inject/clipboard.py`) puts the text in CLIPBOARD and sends no keys; `take_text(as_user=True)` makes the text the user's content, so a later paste in another mode restores it; `AutoInjector` selects it before the window rules; the pipeline joins the session text (`_Session.clipboard`, a new start above 64 KiB); `JobFinished.session_id` lets the Controller notify once for each session; specs 04, 08 §8.4, 09. Tests: 941 unit tests pass (new: backend selection, joining, a failed segment, the limit, notifications), 68 integration tests pass (new on Xvfb: no keys and the old content replaced, a later paste restores the dictated text, cancellation keeps the clipboard); ruff is clean, mypy is clean for `src`. Live test 2026-10-09 after `install.sh --no-apt`, with the user's config set to `clipboard-only`: PTT put the text in the clipboard without a paste (inject 1–8 ms), and `Ctrl+V` pasted it; one continuous session of 6 segments gave one notification, and one `Ctrl+V` pasted all segments; `local-stt last 2` put the second newest text in the clipboard with a notification.

**v0.5 acceptance:** the 14.4 (v0.5) checklist (user decision 2026-10-09). v0.5 has no measured criteria.

Status: v0.5 checklist completed 2026-10-10, results in [acceptance-v0.5.md](acceptance-v0.5.md). All six items pass. Findings: `local-stt history` showed a text that is only a line break as an empty line (fixed: the line break is replaced before `strip()`); one paste waited 17.5 s in a continuous session, probably because the PTT key was held (08 §8.5 step 1; no change, user decision 2026-10-10).

## v0.6 — Input for a voice assistant

Status: plan, ADR-019 and acceptance criteria accepted by the user 2026-10-10.

Basis: ADR-019 and user decisions 2026-10-09. Goal: local-stt becomes the speech input of the orchestrator `local-assistant` (ASR → LLM → TTS, separate repository). Dictation (PTT, injection, CLI) must work as before. The new function is separate and is off until a client turns it on.

Facts from the code and from the other projects (read-only, 2026-10-09):

- `subscribe` already exists: JSON lines to every subscriber, disconnect after 256 queued messages, socket `0600` and an `SO_PEERCRED` uid check (10 §10.2, `ipc.py`).
- The audio consumer already makes `SpeechStarted` and `SpeechEnded` in continuous mode. The Controller does not publish them.
- `job` events do not contain text (10 §10.2). Only the `history` response contains text.
- local-tts uses the same style (`cmd`, `event`, JSON lines). Its `cancel` → `cancelled` takes 4 ms (median). Thus the barge-in delay comes almost fully from the VAD confirmation in local-stt.
- The `local-assistant` report `docs/m4-real-llm.md` measured 180–350 ms of LLM prompt processing for a new user utterance. The LLM can start this work only when it has the text.

User decisions 2026-10-09:

1. The orchestrator starts conversation mode with an IPC command. The mode belongs to that connection: when the connection closes, local-stt goes back to dictation.
2. PTT is off during conversation mode.
3. Conversation texts do not go into the RAM history (task 5.2).
4. Sounds and information notifications are off during conversation mode. Error notifications stay.
5. `vad.min_silence_ms` stays shared with dictation (700 ms). No separate, shorter value.
6. Speculative transcription (task 6.4) is part of v0.6.
7. Live partial transcripts (backlog item 9, ADR-010) are not part of v0.6.

Protocol draft (10 §10.2 gets the final text in task 6.1–6.3):

```json
→ {"cmd": "subscribe", "transcripts": true}
→ {"cmd": "conversation", "on": true}
← {"ok": true}
← {"event": "speech_start", "utt": 41, "session_id": 3, "t": 81234.512}
← {"event": "speech_end", "utt": 41, "t_start": 81234.262, "t_end": 81236.940}
← {"event": "transcript", "utt": 41, "job_ids": [17], "final": true, "text": "Jaka jest pogoda?",
   "language": "auto", "t_start": 81234.262, "t_end": 81236.940, "t_ready": 81237.610,
   "audio_s": 2.68, "stt_s": 0.61}
← {"event": "transcript_retracted", "utt": 41}
← {"event": "utterance_dropped", "utt": 41, "reason": "no_speech"}
```

- `utt` is a number that the daemon gives at `speech_start`. All events of one utterance have the same `utt`.
- All times are `time.monotonic()` (`CLOCK_MONOTONIC`). Other processes on the same machine can compare them with their own monotonic clock.
- `speech_start` and `speech_end` contain no text. Every subscriber gets them.
- `transcript` and `transcript_retracted` go only to a subscriber with `"transcripts": true`.
- `utterance_dropped` tells the client that no final text comes for this `utt`. `reason` ∈ `no_speech`, `filtered`, `cancelled`, `failed`. Without this event, a client waits without a limit after `speech_end`.
- `language` is `"auto"` under Parakeet, because Parakeet does not report the language. Under whisper-server it is the active language.

| # | Task | Notes |
|---|---|---|
| 6.1 | **VAD events:** publish `speech_start` and `speech_end` with `utt` and monotonic times in `subscribe`. Add the start of speech to `AudioSegment`. Measure the delay from the start of speech to `speech_start` | hypothesis: about `min_speech_ms` (250 ms) + one frame; 10 §10.2, 04 §4.2, 05 |
| 6.2 | **Transcript stream:** `subscribe` with `"transcripts": true`; events `transcript` (`final: true`) and `utterance_dropped` | ADR-019; 10 §10.2, 12 §12.2 |
| 6.3 | **Conversation mode:** `{"cmd": "conversation", "on": true/false}` starts or stops continuous mode with the subscriber as the text target (`sink = subscriber`): no injection, no history, no sounds, no information notifications, PTT off. The mode ends when the connection that started it closes. `status` shows the sink | 04 §4.3, §4.4, §4.7; 08; 09; 10 |
| 6.4 | **Speculative transcription:** in conversation mode only, after `conversation.speculative_ms` of silence (draft value 250 ms), transcribe the utterance so far and send `transcript` with `final: false`. If speech starts again in the same utterance, send `transcript_retracted`. If the silence reaches `min_silence_ms` and no speech frame came after the speculative cut, the final `transcript` uses the speculative text again (no second engine request) | ADR-019; measure the gain and the extra engine requests |
| 6.5 | **Echo measurement:** play TTS from the laptop speakers during conversation mode and count false `speech_start` events, without and with the PipeWire `module-echo-cancel` (installed, not loaded). The decision about echo comes after this measurement | local-tts plays with `pw-cat --media-role Communication`; the MVP assumes headphones |

Status: 6.1 implemented 2026-10-10. Changes: the segmenter returns `SpeechStart` (start of the first speech frame, end of the confirming frame) and `SpeechEnd` (utterance start, end of the last frame with `p ≥ end_threshold`); `SpeechStarted` and `SpeechEnded` carry these monotonic times; the Controller gives `utt` numbers that increase across sessions and publishes `speech_start` and `speech_end` in `subscribe`; a session that ends before the end of speech (cancel, error) publishes `speech_end` with `t_end = null`. Specs: 04 §4.2 and §4.3, 05 §5.5, 10 §10.2. Tests: 1024 pass (new: speech times in the segmenter, also after a `max_length` split and a flush; times in the consumer events; `utt` numbers, stale events, cancel with an open utterance, numbers across sessions); ruff and mypy are clean. K1 measurement 2026-10-10 (scratch script, outside the repo: corpus A, 40 files joined with 2.5 s of silence, real-time `FileAudioSource`, the daemon's consumer, Silero, Controller and a temporary Parakeet server; AC, `powersave`): 42 `speech_start` events, delay from `t_start` to publication p50 257 ms, p90 257 ms, maximum 257 ms. The VAD confirmation is 256 ms in all events (8 frames of 32 ms, `min_speech_ms = 250`); the consumer and the Controller add 0.6–1.2 ms. Thus K1 depends almost only on `min_speech_ms`. Live check 2026-10-10 after `install.sh --no-apt`, through the real socket and the built-in microphone, 3 utterances in continuous mode: arrival − `t_start` 288, 310 and 320 ms (32–64 ms more than with the file, from PipeWire capture and the socket); `speech_end` came 737–759 ms after `t_end` (`min_silence_ms = 700`); dictation injected the three texts as before (`total` 0.51–0.56 s).

Status: 6.2 implemented 2026-10-10. User decisions 2026-10-10: every continuous utterance gives a transcript, also in ordinary dictation (PTT gives none); one `transcript` for each utterance, also after a `max_length` split (the texts of the parts are joined with one space). Changes: `subscribe` accepts `"transcripts": true` (`bad_request` for a value that is not a boolean), and `Subscribers` sends `transcript` and `transcript_retracted` only to these subscriptions; `JobFinished` carries the text and the time it was ready, out of its `repr`; the Controller collects the jobs of each utterance and publishes one `transcript` (`job_ids`, `final: true`, `text`, `language`, `t_start`, `t_end`, `t_ready`, `audio_s`, `stt_s`) or one `utterance_dropped` (`reason` of the last part without text) when the utterance has ended and all its jobs have reported; queued jobs that a cancel drains count as `cancelled`, so no utterance waits without a limit. Specs: 04 §4.2 and §4.4, 10 §10.2, 12 §12.2. Tests: 1038 pass (new: the text filter in `Subscribers`, `subscribe` with the flag and a bad flag, the text out of the `JobFinished` repr, one transcript, a split utterance with and without text in the second part, a dropped utterance (filtered, failed, no segment), a job before `speech_end`, cancel with pending and with drained jobs, `auto` under Parakeet, no transcript for PTT); ruff and mypy are clean. Live check 2026-10-10 after `install.sh --no-apt`, two subscribers through the real socket, one continuous session with two sentences and a cough: the subscriber with `"transcripts": true` got two `transcript` events with the correct text; the subscriber without the flag got only the speech events; the editor got both texts as before. The VAD did not take the cough as speech, so no `utterance_dropped` came (the unit tests cover it). `t_ready − t_end` was 1.18 s and 1.10 s (700 ms of silence plus the engine): the base value for K3.

Status: 6.3 implemented 2026-10-10. User decisions 2026-10-10: conversation mode is a flag of `subscribe` (`"conversation": true`), not a separate command, because an ordinary request connection closes when it is idle; the continuous hotkey, `toggle` and `cancel` end the mode; the start is accepted only at IDLE (`busy` otherwise). Changes: `ConversationStart` and `ConversationEnd` events (the owner is the connection); the IPC handler starts the mode before the stream and posts `ConversationEnd` when the stream ends; `Job.sink` (`inject` or `subscriber`); the pipeline does not inject or add to the history for `subscriber` and reports `backend = "none"` (`job` result and timing line `sent`); the Controller has no sounds in conversation mode and only one notification, “Conversation mode enabled” at the start (user decision 2026-10-10: no client may open the microphone unseen; it is not an information notification, because the default `notifications = "errors"` hides those — the first live check showed nothing), publishes `conversation` events (`on: true`; `on: false` with `reason` `client`, `user` or `error`) and `"conversation"` in `status`. An idle stream now checks its client every 0.2 s instead of every 1 s (`SUBSCRIBER_POLL_S`), because the first live test took 1.04 s from the disconnect to the end of the session (K6: 1 s). Specs: 04 §4.2 and §4.3, 08 §8.3, 10 §10.2 and §10.4, 12 §12.2. Tests: 1055 pass (new: start, sink, `busy`, engine not ready, end by the owner and not by another connection, hotkey and cancel, a new ordinary session after a conversation, error notifications; IPC ownership, refusal, two owners, a bad flag, a disconnect seen within 0.5 s; pipeline without injection and history, timing line `sent`); ruff and mypy are clean. Live check 2026-10-10 after `install.sh --no-apt`: a conversation client got two `transcript` events (`result=sent`, inject 0 ms) while the editor had the focus and got nothing; no sound or notification; 1.04 s after the client closed the session ended (before the poll change); a PTT dictation 11 s later was injected (`total` 0.53 s). In an earlier try the continuous hotkey ended the mode with `reason: user`, and the client then got the transcript of an ordinary continuous session that it did not own, without stopping it. After the poll change, three disconnects without speech ended the session after 4, 10 and 238 ms.

Status: 6.4 implemented 2026-10-10. Changes: `[conversation] speculative_ms = 250` (0 = off, below `vad.min_silence_ms`); the Segmenter makes speculative cuts for conversation sessions only (05 §5.5 rule 9); the consumer posts `SpeculativeReady` and `SpeculationRetracted`; the Controller submits a speculative job (`Job.speculative`, it does not change the session context), sends its text as `transcript` with `final: false` while the utterance goes on, sends `transcript_retracted` when speech comes back after a sent text, withdraws a retracted job that has not started (`PipelineWorker.withdraw`), and uses the speculative job as the final part when the final segment has `reuses_speculative` (no second engine request). Specs: 04 §4.2 and §4.3, 05 §5.5, 09, 10 §10.2. Tests: 1002 unit tests pass (new: speculative cuts, retraction by a speech frame or a split, reuse after silence and flush, no speculation when off or after a split; config range; consumer events; speculative and final texts, a final that waits for a running speculative job, retraction, withdraw, a job without text, dictation ignores the cuts; pipeline context and withdraw); ruff and mypy are clean. Measurement 2026-10-10 (scratch script outside the repo; corpus A, 40 files joined with 2.5 s of silence, real-time `FileAudioSource`, the daemon's consumer, Silero, Controller and a temporary Parakeet server; AC, `performance`; results in `~/.local/share/local-stt/bench/conversation-2026-10-10/`): the first run showed that retracted speculative jobs held the engine queue (K3 p90 1.94 s), which the withdraw fixed; the second run had an unknown second load on the machine (engine median 6 s for each job) and is not valid; the third run had a load of at most 1.0 and an engine median of 0.52 s. Third run, utterances of 2–10 s: K2 p90 0.68 s (7 texts, threshold 0.9 s: pass); K3 p90 0.825 s, p50 0.76 s (20 utterances, threshold 0.8 s: **fail by 25 ms**; without speculation p90 1.29 s); K4 WER 5.41 % against 5.10 % in dictation (+0.31 pp, threshold +0.5 pp: pass). Engine jobs: 84 in conversation mode against 49 in dictation (offline count with Silero: 70 speculative cuts, 35 of them retracted by pauses inside sentences, 35 reused). K3 is about 256 ms of silence plus the engine time. User decision 2026-10-10: keep 250 ms and leave K3 open until the v0.6 acceptance, when K5 shows the CPU cost. Live check 2026-10-10 after `install.sh --no-apt` (built-in microphone, a conversation client through the real socket): each of two utterances gave a `final: false` text 550 ms after the end of speech, then `speech_end` and the `final: true` text with the same text and the same single job about 200 ms later (no second engine request, inject 0 ms). The pause inside the spoken question was longer than `min_silence_ms`, so the question became two utterances; a client that waits for a whole question must handle this (an orchestrator topic).

Status: 6.5 measured 2026-10-10. Method: 180 s of local-tts speech (Piper `pl_PL-zenski_wg_glos-medium`, 22 assistant answers) played with `pw-cat --media-role Communication` from the built-in laptop speakers. The daemon used the built-in microphone in conversation mode, and nobody spoke. The scripts and the event logs are in `~/.local/share/local-stt/bench/echo-2026-10-10/`. `module-echo-cancel` was loaded with `aec_method=webrtc` only for the runs with echo cancellation. Its `ec_source` and `ec_sink` were the default devices for these runs, and the script restored the defaults after the runs. A `llama-server` of another project was running with nice 5 (load 0.7–2.6).

| Run | Speaker volume | False `speech_start` | Per minute | Final transcripts |
|---|---|---|---|---|
| without echo cancellation | 44 % | 23 | 7.7 | 23 |
| without echo cancellation | 100 % | 22 | 7.3 | 22 |
| `module-echo-cancel` | 44 % | 0 | 0 | 0 |
| `module-echo-cancel` | 100 % | 0 | 0 | 0 |

Without echo cancellation, almost every TTS answer became a transcript with near-correct text (for example “Stoica Austrii to Santera, a nie Sydney…”). Thus an orchestrator on speakers would answer its own speech.

Control run (echo cancellation on, 44 %, 60 s of TTS): the user spoke 5 sentences. Each sentence gave a `speech_start`, and one extra `speech_start` gave `utterance_dropped` (`filtered`). Thus echo cancellation does not block the user's speech, and barge-in works. Text quality depends on overlap with the TTS. Two sentences with 13 % and 35 % TTS overlap, and one sentence after the TTS ended, had correct text. Two sentences with 100 % TTS overlap had wrong English text (“Yeah, well, I think it's”, “Yeah, yeah.”). One control run with one speaker is a small sample.

User decision 2026-10-10: headphones stay the default assumption. On speakers, the orchestrator loads `module-echo-cancel`. A `speech_start` during TTS stops the TTS (barge-in). The orchestrator must treat the transcript of speech that overlaps the TTS with care, because its text can be wrong. local-stt needs no change for this.

**v0.6 acceptance** (criteria accepted by the user 2026-10-10):

| # | Criterion | Threshold | Method |
|---|---|---|---|
| K1 | Barge-in: the delay from the start of speech to the arrival of `speech_start` at the client | p90 ≤ 400 ms | corpus A recordings through `FileAudioSource`; the start of speech comes from an offline VAD run (task 6.1) |
| K2 | Speculative text: `t_ready − t_end` of `transcript` with `final: false`, utterances of 2–10 s | p90 ≤ 0.9 s | the same recordings (task 6.4) |
| K3 | Final text: `t_ready − t_end` of `transcript` with `final: true` when no retraction came | p90 ≤ 0.8 s | as K2 |
| K4 | Quality: corpus A WER in conversation mode compared with dictation | at most +0.5 pp | checks that the reuse of the speculative text drops no words |
| K5 | CPU cost: a 10-minute soak in conversation mode; all engine requests count, also speculative and retracted ones | RTF ≤ 0.5 (N3); record the number of retractions | `bench --soak` in conversation mode (an extension of about 1–2 h) |
| K6 | Isolation: in conversation mode no text goes to the window, the clipboard, the history or a notification. After the client disconnects, dictation works again within 1 s. A client without `"transcripts": true` gets no text | all pass | Xvfb tests and a live test |
| K7 | Regression and privacy: a short v0.5 checklist (PTT, continuous mode, `doctor`, the full test suite); the journal contains no dictated text | all pass | as in the v0.5 acceptance |

Echo (task 6.5) has no threshold. The acceptance records the number of false `speech_start` events for each minute of TTS, without and with `module-echo-cancel`. The user decides after the measurement. Conversation mode assumes headphones until then.

K1–K3 use recordings, because the real start of speech is not known in a live test. A live result can differ by the PipeWire capture delay. This is not measured.

Status: v0.6 accepted 2026-10-10, results in [acceptance-v0.6.md](acceptance-v0.6.md). K1, K2 and K4–K7 pass. K3 fails by 25 ms (p90 0.825 s), and the user accepted this deviation. K5 needed a new mode of the soak test: `bench --soak --conversation` (13 §13.4 stage 3) gives 0.16 s of engine time for each second of speech. K6 has a new Xvfb test (`tests/e2e/test_conversation_e2e.py`).

Open points:

- The config key names: `[conversation]` with `speculative_ms`.
- The `local-assistant` README and CLAUDE.md still use the old name `local-stt-daemon`. That repository must change them.

## Backlog (no commitments)

Ordered by user value:

1. ~~`text.replacements` with ready-made “commands” (new line, period, comma)~~ **moved to v0.5** (task 5.1). Left here: “wykrzyknik” and “nowy akapit” (Parakeet did not recognize them), a command spoken alone, the `type` backend with a line break, and engine hotwords for command words (untested whether onnx-asr supports them).
2. ~~History of the last N transcripts **in RAM only** + `local-stt last` (insert again)—useful for E12.~~ **moved to v0.5** (task 5.2).
3. Tray / indicator (separate IPC client process, AppIndicator).
4. ~~“Transcribe, do not paste” mode (`injection.backend = "clipboard-only"`).~~ **moved to v0.5** (task 5.3).
5. Per-application profiles (`WM_CLASS` → backend, `append_space`, prompt).
6. Polish + English (`language = "auto"` restricted to {pl, en}).
7. Local LLM for punctuation/correction (a separate process like whisper-server).
8. Wayland (ADR-011).
9. Partial preview in `status --watch --preview` (former task 3.2, moved here 2026-10-06 before implementation). Its v0.3 criterion was that the preview raises mean final-segment latency by at most 10 %; a measurement says it cannot on the reference machine. Tested 2026-10-06 on a temporary `whisper-server` (`small-q8_0`, 4 threads, `audio_ctx` 1000, AC, `performance`, 77 °C at the start), fixtures `pl_short.wav` (4.2 s) and the three fixtures joined (13.2 s):
   - a request costs about the same at any length, because the fixed `audio_ctx` gives the encoder a fixed cost: 4.2 s took 2.7–2.9 s (3.5–4.0 s once the CPU was hot), 13.2 s took 3.7–4.2 s. A preview every 2 s therefore runs back to back while the user speaks;
   - closing the connection does not free the server: whisper-server sets an `abort_callback` on client disconnect, yet a 4.2 s request sent right after dropping a 13.2 s one 0.3–3 s into it took 5.2–7.9 s (7 tries), i.e. it waited for the dropped request. Transcripts were unchanged;
   - so a final segment usually waits for a preview in flight: about +1.5 s on average and up to ~3.5 s on a ~3 s latency, roughly +50 %. Continuous mode already runs at RTF 0.54 in the soak (N3 passes only on `performance`), and back-to-back previews would keep the CPU busy for as long as the user speaks.
   Revisit on a faster machine, after a whisper.cpp upgrade (check the abort again), or with a cheaper design (e.g. a second server with a small model, or at most one preview per utterance).
10. **Fallback engine for wrong-script output** (from ADR-018; only if Cyrillic output bothers the user in practice — the v0.4 counter, task 4.4, provides the evidence). When the default engine's text contains non-Latin letters, re-run the same audio through an engine with a forced language (candidate: Canary 1B v2, `pl` forced: corpus A WER 4.3 %, ~1.9 GB RSS, no Cyrillic in the 5 takes of the failing sentence). Configurable memory mode (user decision 2026-10-07): `resident` (both models loaded, ~3 GB), `on_demand` (load the fallback only when needed, ~2.3 s load + ~1 s decode, unload after idle), `off` (single model); plus the existing `stt.engine = "whisper-server"` as the lowest-RAM option (~0.5 GB). `on_demand` when memory is short: check `MemAvailable` before loading and cap the service with systemd `MemoryMax`; if the load is skipped or fails, inject the default engine's text unchanged and notify once.
