# local-stt

Offline Polish speech-to-text dictation for Ubuntu 24.04, GNOME on X11. Speak, and the text appears in the focused application. Recognition runs on the CPU with [Parakeet TDT 0.6B v3](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3) through [onnx-asr](https://github.com/istupakov/onnx-asr) (the default since v0.4) or with [whisper.cpp](https://github.com/ggml-org/whisper.cpp), and with [Silero VAD](https://github.com/snakers4/silero-vad). It uses no cloud service and no network access.

- **Push-to-talk:** hold **right Ctrl**, speak, release. The text is pasted into the active window.
- **Continuous dictation:** **Shift + right Ctrl** turns it on and off. The VAD splits speech into segments, which are inserted as they are transcribed.
- **Esc** while holding right Ctrl, or `local-stt cancel`, discards the recording.

Text is inserted through the clipboard with a simulated paste. The previous clipboard contents are restored afterwards, and `xdotool type` is the fallback when the clipboard cannot be restored. Nothing you dictate is written to the logs.

## Status

Version 0.5.0. Five milestones have passed their acceptance checklists on the reference machine (Intel i5-8365U, no GPU): [v0.1](docs/acceptance-v0.1.md) (push-to-talk), [v0.2](docs/acceptance-v0.2.md) (continuous dictation), [v0.3](docs/acceptance-v0.3.md) (language switch for Whisper), [v0.4](docs/acceptance-v0.4.md) (the Parakeet engine) and [v0.5](docs/acceptance-v0.5.md) (spoken commands, history, clipboard-only mode). It has not been tested on other hardware or distributions. Wayland is not supported: global hotkeys and input simulation rely on X11.

Parakeet is the default engine since v0.4. On the reference machine, the 90th-percentile delay from key release to inserted text is 0.99 s (21 dictations of 4–10 s). With Whisper `small-q8_0`, the default until v0.4, it was 3.7 s. On the author's voice, the word error rate is 5.6 % with Parakeet and 7.4 % with Whisper ([benchmark results](docs/benchmark-results.md)). Continuous dictation keeps up on the `performance` and `power-saver` profiles.

Parakeet has no language setting: it detects the language itself. A sentence that mixes Polish and English can come out in Cyrillic. To use Whisper instead, set `engine = "whisper-server"` in the config and run `local-stt reload`.

## Requirements

- Ubuntu 24.04 with a GNOME **X11** session ("Ubuntu on Xorg").
- PipeWire (the Ubuntu default) and a microphone.
- Python 3.12.
- About 1.3 GB of RAM for the Parakeet engine, up to 2.4 GB during a 2-minute dictation (0.5 GB with Whisper `small-q8_0`).
- A few minutes of CPU time to build whisper.cpp.

## Installation

```bash
git clone https://github.com/nexusframe/local-stt.git
cd local-stt
scripts/install.sh
```

The installer is idempotent. It does these steps:

1. Installs the apt packages (the only step that uses `sudo`).
2. Builds whisper.cpp.
3. Creates a virtualenv under `~/.local/share/local-stt`.
4. Downloads and verifies the models: Parakeet, Whisper `small-q8_0` and Silero VAD (about 0.9 GB).
5. Writes `~/.config/local-stt/config.toml`.
6. Installs the systemd user services. The daemon starts the engine service that the config selects.
7. Runs `local-stt doctor`.

Run `scripts/install.sh --help` for options such as `--model` (the Whisper model).

To remove it: `scripts/uninstall.sh`. This keeps the models and config; add `--purge` to delete them too.

## Usage

```bash
local-stt status            # state of the daemon, engine, hotkeys and audio
local-stt status --watch    # follow state changes live
local-stt toggle            # continuous dictation on/off, like Shift + right Ctrl
local-stt cancel            # discard the recording and pending text
local-stt doctor            # environment diagnostics
local-stt reload            # apply changes to config.toml
```

Settings such as the engine, the model, hotkeys, sounds, notifications and VAD thresholds are described in [docs/09-configuration.md](docs/09-configuration.md).

## Documentation

The specification in [`docs/`](docs/README.md) is the source of truth: architecture, state machine, audio and VAD, the STT engines, hotkeys, text injection, configuration, privacy, benchmarks, tests and the implementation plan.

## License

No license has been chosen yet, so all rights are reserved. Third-party data in the repository keeps its own license: the test recordings in `tests/fixtures/` come from FLEURS (CC BY 4.0, see [tests/fixtures/README.md](tests/fixtures/README.md)), and `src/local_stt/bench/long_pl.txt` is under CC BY-SA 4.0 (see `long_pl.ATTRIBUTION.md`).
