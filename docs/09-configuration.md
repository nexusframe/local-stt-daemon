# 09. Configuration

## 9.1 Format and location

- Format: **TOML**, parsed with `tomllib` from the Python 3.12 standard library. No dependency is required, and the syntax is unambiguous (in YAML, `no`/`on` may be interpreted as booleans). The initial design allowed the format to be changed. Rationale: [03](03-decisions.md), ADR-008.
- File: `$XDG_CONFIG_HOME/local-stt/config.toml` (default: `~/.config/local-stt/config.toml`). The path can be overridden with the `--config PATH` flag or the `LOCAL_STT_CONFIG` variable.
- If the file is absent, default values are used. `install.sh` copies the fully commented `config.example.toml`.
- Validation: dataclasses plus custom validators in `local_stt/config.py`. Every error has the form `<section>.<key>: <problem> (got <value>)`, for example `vad.end_threshold: must be < start_threshold (got 0.6)`. An unknown key is an **error**, not a warning, so typos never pass silently.
- Changes take effect after `local-stt reload`, according to the groups in [04](04-state-machine.md) §4.6. Keys marked ⟳ cause the daemon to generate a new `whisper-server.env` and restart `local-stt-whisper.service` when the mode is IDLE and the queue is empty (or the engine is DOWN).

## 9.2 Complete file with default values

```toml
# ~/.config/local-stt/config.toml

[stt]
engine = "whisper-server"            # only implementation in v0.1–v0.3
port = 8178                          # ⟳
model = "small-q5_1"                 # ⟳ ggml-<model>.bin filename in models_dir
models_dir = "~/.local/share/local-stt/models"   # ⟳
language = "pl"                      # ⟳
threads = 4                          # ⟳ -t for whisper-server
beam_size = -1                       # ⟳ -1 = greedy
vocabulary_prompt = ""               # e.g. "Kubernetes, PipeWire, Gdańsk."
continuous_context = true            # append the end of the previous segment to the prompt
audio_ctx = 0                        # ⟳ 0 = full 30 s window, or fixed frames (e.g. 1000); 06 §6.7, set from the benchmark
audio_ctx_margin = 128               # ⟳ recordings needing more than audio_ctx - margin frames use the full window
no_speech_threshold = 0.6
logprob_threshold = -1.0
startup_timeout_s = 60
request_timeout_max_s = 120
extra_server_args = []               # ⟳ e.g. ["-nf"]; flags managed by local-stt are rejected (9.3)

[audio]
device = "default"                   # "default" or a PipeWire node name from `local-stt devices`

[ptt]
min_duration_ms = 300
max_duration_s = 120
silence_rms_dbfs = -50.0

[continuous]
max_backlog_s = 60                   # total queued audio after which the mode is disabled

[vad]
enabled = true                       # v0.2: false → PTT with an RMS gate instead of VAD; continuous refuses (vad_disabled)
model = "silero_vad.onnx"            # in models_dir
start_threshold = 0.50
end_threshold = 0.35
min_speech_ms = 250
min_silence_ms = 700
speech_pad_ms = 300
max_segment_s = 15
split_search_s = 3

[hotkeys]
enabled = true
push_to_talk = "Control_R"
continuous_toggle = "Shift+Control_R"
ptt_cancel_key = "Escape"            # single keysym; active only while PTT is held

[text]
append_space = true
hallucination_patterns = [
  'napisy (stworzone|wykonane) przez społeczność amara\.org',
  '(zdjęcia|tłumaczenie) i napisy stworzone przez społeczność amara\.org',
  '^\s*dzięk(i|uję) za (uwagę|obejrzenie|oglądanie)[.!]?\s*$',
  '^\s*(za)?subskrybuj[^.]*[.!]?\s*$',
]
# [[text.replacements]]
# pattern = '(?i)\bnowa linia\b'
# replace = "\n"
# regex = true

[injection]
backend = "auto"                     # auto | clipboard | type
restore_clipboard = true
modifier_wait_ms = 1000
paste_timeout_ms = 1000
type_delay_ms = 12
type_window_classes = ["xterm", "URxvt"]
terminal_window_classes = [
  "gnome-terminal-server", "kitty", "alacritty", "konsole", "tilix",
  "org.wezfurlong.wezterm", "terminator", "xfce4-terminal", "guake", "tabby", "ghostty",
]
[injection.paste_shortcut_overrides]
# "emacs" = "Ctrl+Y"

[feedback]
sounds = true                        # start/stop/cancel/error sounds
sound_volume = 0.4                   # 0.0–1.0 (generated WAV scaling)
notifications = "errors"             # none | errors | all
                                     # all also includes “Dictation enabled/disabled”, “Engine ready” (10 §10.6)

[logging]
level = "INFO"                       # INFO | DEBUG | TRACE
log_text = false                     # true = full transcript text in logs (debugging only)
timings = true                       # timing line for each job (without content)
```

## 9.3 Cross-field validation

| Rule | Message |
|---|---|
| `vad.end_threshold < vad.start_threshold` | `vad.end_threshold: must be < start_threshold` |
| `0 < thresholds < 1` | `…: must be in (0, 1)` |
| `vad.split_search_s < vad.max_segment_s` | |
| `vad.max_segment_s ≤ 28` | margin for Whisper's 30 s window |
| `stt.audio_ctx == 0` or `500 ≤ stt.audio_ctx < 1500`; `0 ≤ stt.audio_ctx_margin < stt.audio_ctx` when it is set | `stt.audio_ctx: must be 0 or 500–1499` (the lower bound is conservative: only 750 and 1000 were measured, 06 §6.7) |
| `stt.audio_ctx > 0` and `vad.max_segment_s * 50 + stt.audio_ctx_margin > stt.audio_ctx` | warning `vad.max_segment_s: continuous segments longer than X s use the full window` |
| the `models_dir/ggml-<model>.bin` file exists | `stt.model: file not found: … (run: local-stt models pull small-q5_1)` |
| when `vad.enabled`: the `models_dir/<vad.model>` file exists | `vad.model: file not found: … (run: local-stt models pull silero-vad)` |
| `len(stt.vocabulary_prompt) ≤ 300` | vocabulary character limit, not token count; the engine may truncate the prompt ([06](06-stt-engine.md) §6.6) |
| `hotkeys.push_to_talk != hotkeys.continuous_toggle` | |
| the hotkey does not use `ISO_Level3_Shift`, `Alt_R`, `Super_L` (conflict), or `Control_L`, `Shift_L` (used by XTest during paste, [08](08-text-injection.md) §8.5) | [07](07-hotkeys-x11.md) §7.2 |
| `hotkeys.ptt_cancel_key` is a single unmodified keysym, distinct from the PTT and continuous keysyms | |
| `stt.extra_server_args` does not contain `--host`, `--port`, `--request-path`, `--inference-path`, `-m`, `-l`, `-t`, `-bs`, `--public`, `--convert` | `stt.extra_server_args: flag X is managed by local-stt` |
| `stt.extra_server_args` does not contain `-pr`/`--print-realtime` or `-debug`/`--debug-mode`: `--print-realtime` prints transcribed text to the server's stdout, which ends up in journald (12 §12.1; verified in `examples/server/server.cpp`, v1.9.4); `--debug-mode` enables whisper.cpp debug output derived from the audio | `stt.extra_server_args: flag X would log transcribed content` |
| `continuous` requires `vad.enabled` | warning `vad.enabled=false: continuous dictation unavailable`; `toggle` returns `vad_disabled` |
| regexes in `text.*` compile | `text.hallucination_patterns[2]: invalid regex: …` |

## 9.4 Files generated for `whisper-server`

`install.sh` creates `~/.config/local-stt/secret` once (permissions 0600): 32 hex characters from `secrets.token_hex(16)`. The value is used as the server's `--request-path` ([06](06-stt-engine.md) §6.4).

From the `[stt]` section and the secret, the daemon (as well as `install.sh` and `local-stt reload`) generates `~/.config/local-stt/whisper-server.env` (0600):

```sh
# generated by local-stt — do not edit manually
LOCAL_STT_WHISPER_ARGS="--host 127.0.0.1 --port 8178 --request-path /3f9c…e1 -m /home/leto/.local/share/local-stt/models/ggml-small-q5_1.bin -l pl -t 4 -bs -1 -sns"
```

The `local-stt-whisper.service` unit loads it through `EnvironmentFile=` ([11](11-daemon-systemd-installation.md) §11.4). The config remains the single source of truth, and the host is always `127.0.0.1` and is not configurable.
