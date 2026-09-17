# 15. Implementation plan

The sequence follows the preliminary design principle: **first establish whether the model is fast enough, then implement PTT, and continuous mode last.** Each stage ends with acceptance criteria. Do not begin a stage until the previous one meets its criteria.

## Stage 0 — Environment and measurement (before v0.1)

**Goal:** a working `whisper-server`, a recording corpus, and initial measurements. No daemon or hotkeys.

| # | Task | Result |
|---|---|---|
| 0.1 | Repository skeleton: `pyproject.toml` (src layout), `ruff`, `mypy`, `pytest`, `requirements.lock`, `git init` | `pytest` passes on an empty suite |
| 0.2 | `scripts/install.sh` steps 1–4 (apt, build whisper.cpp v1.9.4 + `.whisper-tag`, venv) and `secret` generation | `bin/whisper-server --help` |
| 0.3 | `local_stt/models.py` + `local-stt models pull/list/verify` + `scripts/models.sha256` (base-q5_1, small-q5_1, small-q8_0, small, medium-q5_0, large-v3-turbo-q5_0, silero-vad) | models on disk, checksums valid |
| 0.4 | `audio/capture.py` (minimum: 16 kHz `pipewire` PCM, frames, `PIPEWIRE_NODE`) + `audio/wav.py` | `needs_audio` test; confirmation that `sounddevice` with `PIPEWIRE_NODE` reaches the selected node (already verified for `arecord`, [05](05-audio-and-vad.md) §5.2) |
| 0.5 | `stt/whisper_server.py` + `local-stt transcribe FILE.wav` | Polish text from a file |
| 0.6 | `bench/corpus.py` (`record-corpus`, including `--long`) + `bench/prompts_pl.txt` | corpus A recorded |
| 0.7 | `bench/wer.py`, `bench/runner.py` (stages 1–2 from [13](13-benchmark.md) §13.4; temporary server), `bench/report.py` | preliminary `docs/benchmark-results.md` |

**Acceptance:**

- stage 1–2 report for every model,
- provisional `stt.model`, `threads`, and `dynamic_audio_ctx` selected under the rule in §13.5; the report explicitly states that N2 remains unconfirmed until the full injector measurement in v0.1 (the soak test comes in v0.2).

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
| 1.10 | `ipc.py` + CLI commands marked v0.1 in [10](10-cli-ipc-status.md) §10.1 (including grouped `reload` and server restart) | 10, 04 §4.6 |
| 1.11 | `feedback.py`—sounds according to the matrix + error notifications (`notify-send -p/-r`) | 10 §10.6 |
| 1.12 | `doctor.py` | 10 §10.5 |
| 1.13 | `app.py`—composition, start/stop, signals, `threading.excepthook` | 02, 12 |
| 1.14 | `systemd/*.service`, `install.sh` steps 5–9, `uninstall.sh` | 11 |
| 1.15 | `audio/file_source.py` (`FileAudioSource`, required for E2E) + tests: 14.2 unit tests for the items above, `needs_whisper`, `needs_x11`, PTT E2E | 14 |

**v0.1 acceptance:**

- the complete 14.4 (v0.1) checklist,
- N1 (RAM), N2 (PTT latency—measure `total` from `timings` logs for at least 20 dictations per tested configuration, including the full injector time; selection approved under 13 §13.5), N5, N8, and N9.

Preliminary-design requirements covered by v0.1:

- Ubuntu, microphone, whisper.cpp, benchmark-selected model, Polish,
- PTT, post-recording transcription, insertion into the active window, global hotkey,
- logging, configuration, systemd user service.

## v0.2 — Continuous dictation

| # | Task | Document |
|---|---|---|
| 2.1 | `audio/vad.py` (Silero ONNX) | 05 §5.4 |
| 2.2 | `audio/segmenter.py` with hysteresis and cutting at `max_segment_s` | 05 §5.5 |
| 2.3 | Controller: CONTINUOUS state, toggle with flush, cancel, backlog, reconnect, `EngineStateChanged(DOWN)`, deferred reload | 04 §4.3, §4.6 |
| 2.4 | Pipeline: per-session prompt context; TextProcessor step 5 (continuity) | 04 §4.4, 08 §8.2 |
| 2.5 | Hotkey continuous toggle; CLI `toggle`, `status --watch` (IPC `subscribe`) | 07, 10 |
| 2.6 | VAD trimming for PTT (replaces the RMS gate when `vad.enabled`) | 05 §5.3 |
| 2.7 | Audio error handling: reconnect ×3, overflow, digital silence | 05 §5.6 |
| 2.8 | systemd `STATUS=`; `all` notifications | 11, 10 |
| 2.9 | `bench --soak` (using v0.1's `FileAudioSource`) | 13 §13.4 stage 3 |
| 2.10 | Tests: segmenter, continuous controller, continuous E2E | 14 |

**v0.2 acceptance:**

- the 14.4 (v0.2) checklist,
- a 10-minute battery-powered soak test satisfies N3; incorrect-segmentation report (13 §13.3),
- N4 (CPU during silence) measured with `pidstat -p <pid> 1 60`,
- final model defaults recorded in `docs/benchmark-results.md` and `config.example.toml`.

Preliminary-design requirements covered by v0.2: continuous dictation, VAD, automatic segmentation, automatic insertion of successive segments, daemon status, and audio error handling.

## v0.3 — Quality and control

The preliminary design scope (“partial transcription, result stabilization, improved context management, model switching, CPU/latency benchmark”) becomes the following ADR-010-compatible tasks:

| # | Task | Notes |
|---|---|---|
| 3.1 | **Model switching:** changing `stt.model` + `local-stt reload` already works in v0.1 (server restart, [04](04-state-machine.md) §4.6). v0.3 adds only `local-stt models list --bench`, a model list with the latest benchmark results (WER, p90 latency, RAM) for an informed choice. No runtime override—the config is the single source of truth (ADR-008) | 06 §6.5, 13 |
| 3.2 | **Partial preview, without insertion:** in continuous mode, once an utterance lasts > 4 s, send the accumulated audio with `dynamic_audio_ctx` to the engine every 2 s, **but only when the final-job queue is empty and the worker is idle**. The result goes only to `local-stt status --watch --preview`, when `continuous.preview = true` and at least one explicit preview subscriber exists. Never send it to a window or notification; ordinary `status`, `status --watch`, and `job` events still contain no text. Add the new `continuous.preview = false` key, CLI flag, and preview subscription to [09](09-configuration.md) and [10](10-cli-ipc-status.md) in v0.3 | ADR-010 |
| 3.3 | **Segment-boundary stabilization:** `max_length` cuts with 1 s of audio overlap + removal of duplicate words at the join (longest common word suffix/prefix ≥ 2) | 05 §5.5 |
| 3.4 | **Context:** tune `continuous_context` (tail length; reset after `min_silence` > 5 s = new paragraph) using the `long/` corpus | 06 §6.6 |
| 3.5 | `stt.continuous_model` (second server)—**only if** required by the rule in 13 §13.5 | ADR-016 |
| 3.6 | Full benchmark report (`bench report`) with thermals; update `docs/benchmark-results.md` | 13 |

**v0.3 acceptance:**

- the 14.4 (v0.3) checklist,
- continuous-mode WER on the `long/` corpus is no worse than in v0.2, and the number of duplicate boundary words is zero,
- preview increases mean final-segment latency by no more than 10%.

## After v0.3 — backlog (no commitments)

Ordered by user value:

1. `text.replacements` with ready-made “commands” (new line, period, comma)—the mechanism already exists.
2. History of the last N transcripts **in RAM only** + `local-stt last` (insert again)—useful for E12.
3. Tray / indicator (separate IPC client process, AppIndicator).
4. “Transcribe, do not paste” mode (`injection.backend = "clipboard-only"`).
5. Per-application profiles (`WM_CLASS` → backend, `append_space`, prompt).
6. Polish + English (`language = "auto"` restricted to {pl, en}).
7. Local LLM for punctuation/correction (a separate process like whisper-server).
8. Wayland (ADR-011).
