# 09. Configuration

## 9.1 Format and location

- Format: **TOML**, parsed with `tomllib` from the Python 3.12 standard library. No dependency is required, and the syntax is unambiguous (in YAML, `no`/`on` may be interpreted as booleans). The initial design allowed the format to be changed. Rationale: [03](03-decisions.md), ADR-008.
- File: `$XDG_CONFIG_HOME/local-stt/config.toml` (default: `~/.config/local-stt/config.toml`). The path can be overridden with the `--config PATH` flag or the `LOCAL_STT_CONFIG` variable.
- If the file is absent, default values are used. `install.sh` copies the fully commented `config.example.toml` (repository root; every value in it is the default). A path given explicitly with `--config` or `LOCAL_STT_CONFIG` must exist.
- Validation: dataclasses plus custom validators in `local_stt/config.py`. Every error has the form `<section>.<key>: <problem> (got <value>)`, for example `vad.end_threshold: must be < start_threshold (got 0.6)`. An unknown key is an **error**, not a warning, so typos never pass silently.
- Changes take effect after `local-stt reload`, according to the groups in [04](04-state-machine.md) §4.6. Keys marked ⟳ cause a restart of the engine server. The daemon waits until the mode is IDLE and the queue is empty, or the engine is DOWN. Under whisper-server it first generates a new `whisper-server.env`.

## 9.2 Complete file with default values

```toml
# ~/.config/local-stt/config.toml

[stt]
engine = "parakeet"                  # ⟳ "parakeet" (default since v0.4) or "whisper-server"; 9.2a
port = 8178                          # ⟳
model = "small-q8_0"                 # ⟳ whisper-server only: ggml-<model>.bin in models_dir (stage-0 benchmark)
models_dir = "~/.local/share/local-stt/models"   # ⟳
languages = ["pl", "en"]             # first = startup; the language hotkey cycles them; sent with every request
threads = 4                          # ⟳ inference threads of the engine server
beam_size = -1                       # ⟳ -1 = greedy
vocabulary_prompt = ""               # e.g. "Kubernetes, PipeWire, Gdańsk."
continuous_context = true            # append the end of the previous segment to the prompt
audio_ctx = 1000                     # ⟳ 0 = full 30 s window, or fixed frames; 06 §6.7, set from the benchmark
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
language_toggle = "Ctrl+Control_R"   # next of stt.languages; left Ctrl first; "" = no hotkey

[text]
append_space = true
commands = false                     # spoken dwukropek, średnik, myślnik, trzy kropki, nowa linia (08 §8.2)
hallucination_patterns = [
  'napisy (stworzone|wykonane) przez społeczność amara\.org',
  '(zdjęcia|tłumaczenie) i napisy stworzone przez społeczność amara\.org',
  '^\s*dzięk(i|uję) za (uwagę|obejrzenie|oglądanie)[.!]?\s*$',
  '^\s*(za)?subskrybuj[^.]*[.!]?\s*$',
  # English, for "en" in stt.languages
  'subtitles by the amara\.org community',
  '^\s*thank(s| you)( very much)? for watching[.!]?\s*$',
  '^\s*(please )?(like and )?subscribe[^.]*[.!]?\s*$',
]
# [[text.replacements]]
# pattern = '(?i)\bnowa linia\b'
# replace = "\n"
# regex = true

[injection]
backend = "auto"                     # auto | clipboard | type | clipboard-only (no paste, 08 §8.4)
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

[history]
size = 10                            # texts kept in RAM for `local-stt last`; 0 = off (12 §12.2)

[logging]
level = "INFO"                       # INFO | DEBUG | TRACE
log_text = false                     # true = full transcript text in logs (debugging only)
timings = true                       # timing line for each job (without content)
```

### 9.2a Keys per engine (v0.4)

`stt.engine` selects the engine server ([06](06-stt-engine.md) §6.9). Some `[stt]` keys apply to one engine only:

| Key | whisper-server | Parakeet |
|---|---|---|
| `port`, `models_dir`, `threads`, `startup_timeout_s`, `request_timeout_max_s` | yes | yes |
| `model` | yes | no: the Parakeet model is fixed (`parakeet-tdt-0.6b-v3-int8`) and has no key |
| `languages` | yes | no: the language switch is rejected, the model detects the language |
| `vocabulary_prompt`, `continuous_context` | yes | no: Parakeet has no prompt |
| `audio_ctx`, `audio_ctx_margin`, `beam_size`, `extra_server_args` | yes | no |
| `no_speech_threshold`, `logprob_threshold` | yes | no: Parakeet sends no confidences (06 §6.10) |

The validator checks all keys for both engines. A key that does not apply is kept, so a switch back to whisper-server uses it again.

## 9.3 Cross-field validation

| Rule | Message |
|---|---|
| `vad.end_threshold < vad.start_threshold` | `vad.end_threshold: must be < start_threshold` |
| `0 < thresholds < 1` | `…: must be in (0, 1)` |
| `vad.split_search_s < vad.max_segment_s` | |
| `vad.max_segment_s ≤ 28` | margin for Whisper's 30 s window |
| `stt.audio_ctx == 0` or `500 ≤ stt.audio_ctx < 1500`; `0 ≤ stt.audio_ctx_margin < stt.audio_ctx` when it is set | `stt.audio_ctx: must be 0 or 500-1499` (the lower bound is conservative: only 750 and 1000 were measured, 06 §6.7) |
| `stt.audio_ctx > 0` and `vad.max_segment_s * 50 + stt.audio_ctx_margin > stt.audio_ctx` | warning `vad.max_segment_s: continuous segments longer than X s use the full window` |
| whisper-server: the `models_dir/ggml-<model>.bin` file exists | `stt.model: file not found: … (run: local-stt models pull small-q8_0)` |
| Parakeet: the `models_dir/parakeet-tdt-0.6b-v3-int8/` directory exists (`doctor` also checks the checksum of each file) | `stt.engine: parakeet model not found: … (run: local-stt models pull parakeet-tdt-0.6b-v3-int8)` |
| when `vad.enabled`: the `models_dir/<vad.model>` file exists | `vad.model: file not found: … (run: local-stt models pull silero-vad)` |
| `len(stt.vocabulary_prompt) ≤ 300` | vocabulary character limit, not token count; the engine may truncate the prompt ([06](06-stt-engine.md) §6.6) |
| `stt.languages` names at least one language; each is a language code (`[a-z]{2,3}` or `auto`), none repeated (task 3.7) | `stt.languages[1]: must be a language code such as "pl"`, `stt.languages: must not repeat a language` |
| the removed key `stt.language` is not used | `stt.language: replaced by stt.languages, the first is the startup language: languages = ["pl", "en"]` |
| `hotkeys.push_to_talk != hotkeys.continuous_toggle` | |
| `hotkeys.language_toggle` is `""` (no hotkey) or differs from the PTT and continuous shortcuts | `hotkeys.language_toggle: must differ from push_to_talk and continuous_toggle` |
| the hotkey does not use `ISO_Level3_Shift`, `Alt_R`, `Super_L` (conflict), or `Control_L`, `Shift_L` (used by XTest during paste, [08](08-text-injection.md) §8.5) | [07](07-hotkeys-x11.md) §7.2 |
| `hotkeys.ptt_cancel_key` is a single unmodified keysym, distinct from the PTT, continuous and language keysyms | |
| `stt.extra_server_args` does not contain `--host`, `--port`, `--request-path`, `--inference-path`, `-m`/`--model`, `-l`/`--language`, `-t`/`--threads`, `-bs`/`--beam-size`, `--public`, `--convert` (also as `--flag=value`) | `stt.extra_server_args: flag X is managed by local-stt` |
| `stt.extra_server_args` items and the model path (`stt.models_dir`) contain no whitespace, quotes, backslashes, or `$`: systemd splits the unbraced `$LOCAL_STT_WHISPER_ARGS` on whitespace (9.4) | `stt.models_dir: must not contain whitespace, quotes, backslashes or $` |
| `stt.extra_server_args` does not contain `-pr`/`--print-realtime` or `-debug`/`--debug-mode`: `--print-realtime` prints transcribed text to the server's stdout, which ends up in journald (12 §12.1; verified in `examples/server/server.cpp`, v1.9.4); `--debug-mode` enables whisper.cpp debug output derived from the audio | `stt.extra_server_args: flag X would log transcribed content` |
| `continuous` requires `vad.enabled` | warning `vad.enabled=false: continuous dictation unavailable`; `toggle` returns `vad_disabled` |
| `history.size` is 0..100 (task 5.2) | `history.size: must be 0..100 (got 500)` |
| regexes in `text.*` compile | `text.hallucination_patterns[2]: invalid regex: …` |
| the `replace` template of a regex `text.replacements` rule is valid for its pattern (group references exist, no bad escapes); `re.sub` parses the template even without a match, so a bad one would fail on every transcript | `text.replacements[0].replace: invalid replacement template: …` |

Notes on the implementation (`local_stt/config.py`):

- Hotkeys are checked for syntax ([07](07-hotkeys-x11.md) §7.2) and for a known keysym name (python-xlib keysym tables, no X connection). Whether the keysym has a keycode in the current keyboard map is checked when grabbing (`hotkeys/x11.py`).
- The two model-file rules are checked separately (`check_model_files`) by the daemon and `doctor`, not by every CLI command: `local-stt models pull` and `transcribe --model` must work before the configured model is downloaded.
- Besides the rules above, single values are range-checked (port, threads, timeouts, enums such as `injection.backend` and `logging.level`); TOML types are strict (`port = "8178"` is an error, an integer is accepted where a float is expected).

## 9.4 Files generated for `whisper-server`

The Parakeet server needs no generated file: it reads `config.toml` and `secret` itself ([06](06-stt-engine.md) §6.10).

`install.sh` creates `~/.config/local-stt/secret` once (permissions 0600): 32 hex characters from `secrets.token_hex(16)`. The value is used as the server's `--request-path` ([06](06-stt-engine.md) §6.4).

From the `[stt]` section and the secret, the daemon (as well as `install.sh` and `local-stt reload`) generates `~/.config/local-stt/whisper-server.env` (0600):

```sh
# generated by local-stt — do not edit manually
LOCAL_STT_WHISPER_ARGS="--host 127.0.0.1 --port 8178 --request-path /3f9c…e1 -m ~/.local/share/local-stt/models/ggml-small-q8_0.bin -l pl -t 4 -bs -1 -sns"
```

The `local-stt-whisper.service` unit loads it through `EnvironmentFile=` ([11](11-daemon-systemd-installation.md) §11.4). The config remains the single source of truth, and the host is always `127.0.0.1` and is not configurable.
