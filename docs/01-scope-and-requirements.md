# 01. Scope and requirements

## 1.1 What we are building

`local-stt` is a background program (daemon) for Ubuntu. It converts Polish speech to text **entirely offline** and types that text into the currently active window.

It consists of two processes:

| Process | Language | Role |
|---|---|---|
| `local-stt-whisper.service` | C++ (the ready-made `whisper-server` from whisper.cpp) | keeps the Whisper model in memory and transcribes audio sent over `127.0.0.1` |
| `local-stt.service` | Python 3.12 (our code) | hotkeys, microphone, VAD, state machine, text post-processing, text injection, CLI/status |

There is also the `local-stt` CLI tool (full list: [10](10-cli-ipc-status.md) §10.1).

## 1.2 Target environment (verified on the reference machine)

| Component | Value |
|---|---|
| OS | Ubuntu 24.04.5 LTS (noble) |
| Session | **GNOME on Xorg (X11)**, `XDG_SESSION_TYPE=x11`, WM: mutter |
| CPU | Intel Core i5-8365U, 4 cores / 8 threads, AVX2 |
| RAM | 16 GB (in practice, about 4–5 GB free during normal use) |
| GPU | no dedicated GPU |
| Audio | PipeWire 1.0.5 with `pipewire-pulse` and `pipewire-alsa`; microphone: built-in ALC3254 |
| Keyboard layouts | GNOME input sources: `pl` (X11 `setxkbmap -query`: `pl,us`); right Alt = AltGr (`ISO_Level3_Shift`) — **required for Polish characters** |
| Shortcuts used by GNOME | `Super+Space`, `Shift+Super+Space` (switch input source), `Super_L` (overlay/Activities), `Control_L` (locate-pointer, disabled) |
| Python | 3.12.3 (system) |
| systemd | 255, `graphical-session.target` active in the user session |
| Available tools | `xdotool 3.20160805.1`, `notify-send`, `pw-record`, `gcc`, `git` |
| Missing tools | `cmake`, `python3-venv`, `libportaudio2` (installed by `scripts/install.sh`) |

Wayland **is not a target** in versions v0.1–v0.3. The architecture does not preclude it: hotkeys and text injection are behind interfaces, so Wayland support can be added later as separate backends (see [03-decisions.md](03-decisions.md), ADR-011).

## 1.3 Operating modes

### Push-to-talk (PTT)

1. The user **presses and holds** the PTT key (right `Ctrl` by default).
2. The daemon opens the microphone stream and records.
3. The user releases the key. The recording is placed in the transcription queue.
4. The text is processed and typed into the window that is active **at injection time**.

Pressing `Esc` while holding PTT cancels the recording. Taps shorter than `ptt.min_duration_ms` (press→release time) are discarded. They may produce a brief start sound if the first frame has already arrived, but do not trigger transcription or a stop sound.

### Continuous dictation

1. `Shift + right Ctrl` enables listening. The daemon plays the start sound.
2. The microphone remains open, and VAD (Silero) splits the audio into utterances.
3. Each completed utterance is placed in the transcription queue. **Recording does not stop during transcription.**
4. Results are injected in recording order.
5. Pressing `Shift + right Ctrl` again ends the mode. Any ongoing utterance is finalized and transcribed as well.
6. `local-stt cancel` ends the mode and cancels all pending jobs and the result of the current transcription, including PTT jobs. No subsequent paste or `type` chunk is started. An injection operation that has already begun may finish and is not undone; the CLI reports it through `injection_in_flight` ([08](08-text-injection.md) §8.3). Esc cancels only the current PTT recording.

## 1.4 Functional requirements → where they are described

| ID | Requirement | Version | Document |
|---|---|---|---|
| F1 | entirely offline operation | v0.1 | 12, 11 |
| F2 | no GPU requirement | v0.1 | 06 |
| F3 | Polish language | v0.1 | 06, 13 |
| F4 | push-to-talk | v0.1 | 04, 07 |
| F5 | continuous dictation | v0.2 | 04, 05 |
| F6 | global hotkeys | v0.1 | 07 |
| F7 | automatic speech detection (VAD) | v0.2 | 05 |
| F8 | injection into the active window | v0.1 | 08 |
| F9 | daemon operation | v0.1 | 11 |
| F10 | session autostart | v0.1 | 11 |
| F11 | clear status | v0.1 (CLI, sounds, error notifications), v0.2 (`status --watch`, systemd `STATUS=`, `all` notifications) | 10 |
| F12 | manual listening stop | v0.2 | 04, 10 |

## 1.5 Non-functional requirements — measurable targets

| ID | Target | How it is measured |
|---|---|---|
| N1 | RAM: Python daemon ≤ 150 MB RSS; `whisper-server` with the selected model ≤ 1 GB RSS | `local-stt bench`, `ps -o rss` |
| N2 | PTT: p90 latency from key release to injector completion, including clipboard handling (`total` in logs), ≤ 5 s for 4–10 s utterances (default model). Raised from 2.5 s on 2026-09-17 after stage-0 measurements (13 §13.5) | `bench` measures the stage up to ready text; acceptance: p90 `total` from at least 20 complete dictations with successful injection, including audio finalization and clipboard handling (13 §13.5) |
| N3 | Continuous: average RTF ≤ 0.5 in a 10-minute test; the queue does not grow monotonically | `bench --soak` |
| N4 | Continuous in silence: daemon CPU ≤ 5% of one core | `pidstat` |
| N5 | no audio or text byte leaves the host; `whisper-server` listens only on `127.0.0.1` | `ss -ltnp`, `doctor` |
| N6 | model replacement without rebuilding: change `stt.model` + `local-stt reload` | [06](06-stt-engine.md) |
| N7 | STT engine replacement without changes outside the `stt/` module | `SttEngine` interface |
| N8 | all configuration in `~/.config/local-stt/config.toml` | [09](09-configuration.md) |
| N9 | restart within ≤ 5 s after a process failure (systemd) | manual `kill -9` test |

## 1.6 Out of scope (intentionally)

- Wayland, KDE, and other distributions (they may work, but are not tested).
- GPU, CUDA, OpenVINO.
- Partial “live” transcription that corrects already injected text (an explicitly enabled preview in `status --watch`, never injected or sent in a notification, was planned for v0.3 as task 3.2 and moved to the backlog after a measurement — see [03](03-decisions.md) ADR-010 and [15](15-implementation-plan.md) backlog item 9).
- Voice commands, GUI, tray, and LLM post-processing — only as extension points ([15](15-implementation-plan.md), “Backlog” section).
- `PAUSE`/`RESUME` actions from the preliminary design (§9): deferred. Continuous mode is toggled with a single shortcut and queue state is preserved, so a separate pause adds nothing. We will revisit this if preserving prompt context between sessions becomes necessary.
- Configurable `sample_rate`/`channels` (preliminary design §5.1): rejected; see [05](05-audio-and-vad.md) §5.2.
- Running as root or as any system service (except apt packages during installation).
