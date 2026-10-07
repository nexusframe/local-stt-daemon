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

**v0.4 acceptance** (thresholds approved by the user 2026-10-07):

- corpus A WER with the default engine ≤ 7.4 % (the `small-q8_0` result),
- N2: p90 `total` from ≥ 20 PTT dictations of 4–10 s, target ≤ 2.0 s (v0.1: 3.70 s),
- N3: soak RTF ≤ 0.5 on AC `performance`, and a `power-saver` run recorded (it fails with Whisper),
- `stt.engine = "whisper-server"` still passes the v0.2 soak (no regression of the alternative).

## Backlog (no commitments)

Ordered by user value:

1. `text.replacements` with ready-made “commands” (new line, period, comma)—the mechanism already exists.
2. History of the last N transcripts **in RAM only** + `local-stt last` (insert again)—useful for E12.
3. Tray / indicator (separate IPC client process, AppIndicator).
4. “Transcribe, do not paste” mode (`injection.backend = "clipboard-only"`).
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
