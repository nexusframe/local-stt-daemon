# local-stt — documentation

A local, offline **Polish speech-to-text** daemon for Ubuntu 24.04 (GNOME on X11), optimized for a CPU-only system without a GPU (Intel i5-8365U).

- **Push-to-talk:** hold **right Ctrl**, speak, then release it; the text appears in the active window.
- **Continuous:** **Shift + right Ctrl** enables continuous dictation. VAD divides speech into segments, which are inserted as they become available.

Everything runs locally: whisper.cpp (`whisper-server` on `127.0.0.1`) plus a Python daemon.

## Table of contents

| # | Document | Contents |
|---|---|---|
| 01 | [Scope and requirements](01-scope-and-requirements.md) | verified target environment, operating modes, F/N requirements with measurable targets, out of scope |
| 02 | [Architecture](02-architecture.md) | processes, threads, repository structure, PTT/continuous sequences, types, extension points |
| 03 | [Decision log (ADR)](03-decisions.md) | 17 decisions with alternatives, objections, and responses |
| 04 | [State machine](04-state-machine.md) | `(mode, pipeline, engine)` state, events, complete transition table, job queue, reload |
| 05 | [Audio and VAD](05-audio-and-vad.md) | capture (sounddevice/PipeWire), PTT recorder, Silero VAD (ONNX), hysteresis segmenter |
| 06 | [STT engine](06-stt-engine.md) | whisper.cpp v1.9.4: build, models, server flags, HTTP contract, `audio_ctx`, prompt, hallucination filtering |
| 07 | [X11 hotkeys](07-hotkeys-x11.md) | XGrabKey, default keys and GNOME conflicts, press/release, autorepeat |
| 08 | [Text processing and injection](08-text-injection.md) | TextProcessor, clipboard + XTest with confirmation and restoration, xdotool fallback |
| 09 | [Configuration](09-configuration.md) | complete `config.toml` with defaults, validation |
| 10 | [CLI, IPC, and status](10-cli-ipc-status.md) | `local-stt` commands, socket protocol, `status`, `doctor`, sounds, notifications |
| 11 | [Daemon, systemd, and installation](11-daemon-systemd-installation.md) | file layout, dependencies, `install.sh`, both systemd units, operations |
| 12 | [Logging, privacy, and error handling](12-logging-privacy-errors.md) | what is logged, privacy guarantees, E1–E16 error matrix |
| 13 | [Benchmarking and model selection](13-benchmark.md) | corpus, metrics (WER/RTF/latency/thermals), matrix, model-selection rule |
| 14 | [Test strategy](14-tests.md) | test pyramid, required unit tests, Xvfb/E2E, acceptance checklist |
| 15 | [Implementation plan](15-implementation-plan.md) | stage 0 (measurement) → v0.1 PTT → v0.2 continuous → v0.3 quality; acceptance criteria |

Suggested reading order:

- **implementation:** 01 → 02 → 04 → 15, then the document relevant to the current task,
- **“why this design?”:** 03.

## Key decisions at a glance

| Area | Decision |
|---|---|
| Processes | `local-stt-whisper.service` (whisper.cpp `whisper-server`, loopback) + `local-stt.service` (Python 3.12) |
| Model | start with `small-q5_1`; the benchmark makes the final selection (candidates through `medium-q5_0` and `large-v3-turbo-q5_0`); `base` rejected for Polish (~31% WER) |
| Hotkeys | XGrabKey: PTT = hold right Ctrl, continuous = Shift + right Ctrl, Esc while holding PTT = cancel |
| Audio | sounddevice, 16 kHz mono, 512-sample frames; microphone open only while recording |
| VAD | Silero v6.2.1 (ONNX, onnxruntime), 0.50/0.35 hysteresis, 700 ms silence, 15 s maximum |
| Continuous | recording is never paused; queue + one worker; only final segments are inserted |
| Insertion | custom CLIPBOARD owner + XTest `Ctrl+V` / `Ctrl+Shift+V` (terminals), paste confirmation and clipboard restoration; `xdotool type` fallback |
| Config | TOML at `~/.config/local-stt/config.toml` |
| Control | Unix socket + JSON Lines; `local-stt status/toggle/cancel/reload/doctor/bench` CLI |
| Status | start/stop/cancel/error sounds, error notifications, `status`, systemd `STATUS=` |

## Differences from the preliminary design

The preliminary design is archived in [`archive/preliminary-design.md`](archive/preliminary-design.md) (Polish, superseded; kept for context only).

| Preliminary design | This specification | Reason |
|---|---|---|
| `Super+Space` / `Super+Shift+Space` hotkeys | right Ctrl / Shift + right Ctrl | both are occupied in Ubuntu 24.04 (input-source switching); conflict with Mutter's overlay key (ADR-007) |
| `LISTENING → TRANSCRIBING → INJECT → LISTENING` | `(mode, pipeline, engine)` state; capture continues during transcription | the preliminary diagram lost speech (ADR-004) |
| `base` as the primary option | `base` for tests only | Polish WER ~31% (ADR-003) |
| `threads: 8` | `threads: 4` for the benchmark | HT does not accelerate the encoder (whisper.cpp#89 data) |
| YAML config | TOML | standard library, unambiguous types (ADR-008) |
| “X11 / clipboard / ydotool” insertion | clipboard with confirmation + `xdotool type` fallback | Polish characters are unreliable in xdotool 2016 (ADR-009) |
| v0.3 `partial transcript` inserted into the application | preview only in explicitly enabled `status --watch` | ADR-010 |

## Conventions

- Versions verified as of **2026-09-17**: whisper.cpp v1.9.4, Silero VAD v6.2.1, python-xlib 0.33, xdotool 3.20160805.1 (Ubuntu), PipeWire 1.0.5, systemd 255.
- Claims marked “unconfirmed” / “hypothesis” must be verified during implementation. Everything else was checked on the reference machine or in the projects' source code.
