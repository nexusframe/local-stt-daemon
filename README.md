# local-stt-daemon

Offline Polish speech-to-text dictation for Ubuntu 24.04, GNOME on X11. Speak, and the text appears in the focused application. Recognition runs on the CPU with [whisper.cpp](https://github.com/ggml-org/whisper.cpp) and [Silero VAD](https://github.com/snakers4/silero-vad), with no cloud service and no network access.

- **Push-to-talk:** hold **right Ctrl**, speak, release. The text is pasted into the active window.
- **Continuous dictation:** **Shift + right Ctrl** turns it on and off. The VAD splits speech into segments, which are inserted as they are transcribed.
- **Esc** while holding right Ctrl, or `local-stt cancel`, discards the recording.

Text is inserted through the clipboard with a simulated paste. The previous clipboard contents are restored afterwards, and `xdotool type` is the fallback when the clipboard cannot be restored. Nothing you dictate is written to the logs.

## Status

Pre-release (version 0.0.1). v0.1 (push-to-talk) and v0.2 (continuous dictation) have passed their acceptance checklists on the reference machine (Intel i5-8365U, no GPU): [v0.1](docs/acceptance-v0.1.md), [v0.2](docs/acceptance-v0.2.md). It has not been tested on other hardware or distributions. Wayland is not supported: global hotkeys and input simulation rely on X11.

With the default model (`small-q8_0`), the 90th-percentile delay from releasing the key to the inserted text was 3.7 s on the reference machine, and the word error rate on the author's voice was 7.4 % ([benchmark results](docs/benchmark-results.md)).

## Requirements

- Ubuntu 24.04 with a GNOME **X11** session ("Ubuntu on Xorg").
- PipeWire (the Ubuntu default) and a microphone.
- Python 3.12, about 1 GB of RAM for the model, and a few minutes of CPU time to build whisper.cpp.

## Installation

```bash
git clone https://github.com/nexusframe/local-stt-daemon.git
cd local-stt-daemon
scripts/install.sh
```

The installer is idempotent. It installs the apt packages (the only step that uses `sudo`), builds whisper.cpp, creates a virtualenv under `~/.local/share/local-stt`, downloads and verifies the model, writes `~/.config/local-stt/config.toml`, enables two systemd user services, and finishes with `local-stt doctor`. Run `scripts/install.sh --help` for options such as `--model`.

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

Settings such as the model, hotkeys, sounds, notifications and VAD thresholds are described in [docs/09-configuration.md](docs/09-configuration.md).

## Documentation

The specification in [`docs/`](docs/README.md) is the source of truth: architecture, state machine, audio and VAD, the STT engine, hotkeys, text injection, configuration, privacy, benchmarks, tests and the implementation plan.

## License

No license has been chosen yet, so all rights are reserved. Third-party data in the repository keeps its own license: the test recordings in `tests/fixtures/` come from FLEURS (CC BY 4.0, see [tests/fixtures/README.md](tests/fixtures/README.md)), and `src/local_stt/bench/long_pl.txt` is under CC BY-SA 4.0 (see `long_pl.ATTRIBUTION.md`).
