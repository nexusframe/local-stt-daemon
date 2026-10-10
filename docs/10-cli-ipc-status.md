# 10. CLI, IPC, and status

## 10.1 `local-stt` commands

One entry point (`[project.scripts] local-stt = "local_stt.cli:main"`), with subcommands implemented through `argparse`. **This is the only complete command list** — other documents refer here.

| Command | Version | Action | Requires a running daemon |
|---|---|---|---|
| `local-stt daemon [--config P] [--log-level L]` | v0.1 | runs the daemon in the foreground (as systemd starts it) | — |
| `local-stt engine-server [--config P] [--port N] [--threads N] [--model-dir D]` | v0.4 (tasks 4.2, 4.5) | runs the Parakeet engine server in the foreground, as `local-stt-engine.service` starts it ([06](06-stt-engine.md) §6.10). The options override `stt.port`, `stt.threads` and the model directory for a temporary server. The environment variable `LOCAL_STT_REQUEST_PATH` replaces the request path from `secret` | — |
| `local-stt status [--json] [--watch [--preview]]` | v0.1 / `--watch` v0.2 / `--preview` backlog (15, item 9) | daemon status (10.4) | yes (otherwise: `daemon not running`, code 3) |
| `local-stt ptt start\|stop` | v0.1 | equivalent to pressing/releasing the PTT key | yes |
| `local-stt toggle` | v0.2 | enables/disables continuous mode | yes |
| `local-stt language [toggle\|CODE]` | v0.3 (task 3.7) | without an argument prints the active language (`en (languages: pl, en)`, read from `status`); `toggle` moves to the next of `stt.languages` like the hotkey; `CODE` selects a language from the list (other codes → `bad_language`, code 4). The daemon never writes the config, so a restart returns to the first language of the list. Under Parakeet, `toggle` and `CODE` are rejected with `language_unsupported` (code 4), and the active language shows as `auto` (task 4.3) | yes |
| `local-stt last [N]` | v0.5 (task 5.2) | inserts the N-th newest text of the history again into the active window (default 1 = the newest), through the same injector and queue as a dictation. Rejected with `busy` (code 4) while PTT records or continuous mode is on, and with `no_history` (code 4) when there is no text number N. N < 1 → usage error (code 2). The command prints nothing on success | yes |
| `local-stt history` | v0.5 (task 5.2) | prints the history texts, newest first, one per line with the number for `last`: `1  Ala ma kota.`. A line break in a text shows as `⏎`. An empty history prints `history is empty` | yes |
| `local-stt cancel` | v0.1 | cancels recording/continuous mode and pending jobs; an injection operation already underway may finish (08 §8.3) | yes |
| `local-stt reload` | v0.1 | reloads the config; reports applied and deferred changes and whether the server will restart ([04](04-state-machine.md) §4.6) | yes |
| `local-stt doctor` | v0.1 | environment diagnostics (10.5) | no |
| `local-stt devices` | v0.1 | lists PipeWire sources (`pactl -f json list sources`, excluding `.monitor`): node name for `audio.device` + description; marks the default | no |
| `local-stt models list\|pull NAME\|verify` | stage 0 | models in `models_dir`, HF download, SHA256 verification (the only command that uses the Internet). `parakeet-tdt-0.6b-v3-int8` is a directory of four files; `list` shows its total size ([06](06-stt-engine.md) §6.3, task 4.5) | no |
| `local-stt models list --bench` | v0.3 | lists the STT models (the Whisper models and, since v0.4, Parakeet) with their latest benchmark results from `~/.local/share/local-stt/bench/`: WER, p90 `text_ready` (medium group), peak server RSS. Per model: the newest run with whole-corpus (stage 2) results, else the newest stage-1 run (marked `stage 1`); in that run, the configuration matching `stt.threads`, `stt.audio_ctx` and `stt.beam_size`, else the closest one (`audio_ctx` first, then beam, then threads; marked `!`). Each row shows the run date and corpus (A = the user's recordings, B = the public interim corpus, 13 §13.2), because results from different corpora or stages are not comparable. `*` marks the model of the selected engine: `stt.model` under whisper-server, `parakeet-tdt-0.6b-v3-int8` under Parakeet (user decisions 2026-10-04; task 4.5). Parakeet has no `audio_ctx` or beam, so only its thread count can differ from the config | no |
| `local-stt transcribe FILE.wav [--model M]` | stage 0 | one-shot file transcription (test without microphone or hotkeys). Without `--model`, it uses the running server of `stt.engine`. With `--model`, it uses a **temporary** server on a random free loopback port (like `bench`, [13](13-benchmark.md) §13.4), so the service model does not change. `--model parakeet-tdt-0.6b-v3-int8` starts a temporary Parakeet server; any other name starts a temporary whisper-server (task 4.5) | without `--model`: the engine server, not the daemon |
| `local-stt record-corpus DIR [--long]` | stage 0 | records the benchmark corpus ([13](13-benchmark.md) §13.2) | no |
| `local-stt bench [--quick] [--dataset DIR] …` | stage 0 | model matrix on temporary servers, Whisper models and Parakeet ([13](13-benchmark.md) §13.4) | no (both engine services must be stopped) |
| `local-stt bench --soak …` | v0.2 | 10-minute continuous-mode test through the real Segmenter; default model: the `stt.engine` model (Parakeet since task 4.9); `--audio-ctx` with Parakeet → code 2 | no |
| `local-stt bench --context [--long F.wav] [--reference F.txt] [--context-chars 0,100,200,300] [--context-reset off]` | v0.3 | continuous-mode context policies on one long recording ([13](13-benchmark.md) §13.4); whisper-server only, because Parakeet has no prompt | no |
| `local-stt bench report DIR` | stage 0 | Markdown results report | no |

Exit codes: `0` OK, `1` general error, `2` usage error, `3` daemon not running, `4` rejected by the daemon (for example `toggle` when the engine is DOWN), `78` configuration error.

## 10.2 IPC — control socket

- Path: `$XDG_RUNTIME_DIR/local-stt/control.sock` (`/run/user/1000/…`, tmpfs, private to the user).
- The directory has `0700` permissions and the socket `0600`. The daemon also checks `SO_PEERCRED`: the client's `uid` must equal `os.getuid()`, otherwise it closes the connection.
- Protocol: **JSON Lines** (one request = one UTF-8 line terminated by `\n`; one response = one line).
- Server: the `ipc-server` thread (`socketserver.ThreadingUnixStreamServer`). State-changing commands become Controller events. The response waits for their processing through a `concurrent.futures.Future` with a 5 s timeout.
- Stale socket: at startup, if the file exists and `connect()` fails, the file is removed. If `connect()` succeeds, the daemon is already running: ERROR `another instance is running`, exit code 1.
- Several requests may be sent over one connection; a connection that sends nothing for 60 s is closed. A connection from another uid is closed without a response.
- Errors produced by the IPC layer itself, before the Controller sees the request (task 1.10): `bad_request` (invalid JSON or UTF-8, not an object, `ptt` without `start`/`stop`), `unknown_command`, `too_long` (line > 64 KiB; the connection is then closed), `timeout` (no Controller response within 5 s). The CLI maps them, like Controller rejections, to exit code 4; `reload` with an invalid config (`{"ok": false, "errors": [...]}`) exits with code 78.

### Requests and responses

```json
→ {"cmd": "status"}
← {"ok": true, "status": { ...see 10.4... }}

→ {"cmd": "ptt", "action": "start"}
← {"ok": true}

→ {"cmd": "toggle"}
← {"ok": false, "error": "engine_down", "message": "STT engine unavailable"}

→ {"cmd": "language"}                    // toggle; {"cmd": "language", "set": "en"} selects
← {"ok": true, "language": {"active": "en", "languages": ["pl", "en"]}}

→ {"cmd": "last", "n": 1}                 // task 5.2; "n" is optional, default 1
← {"ok": true, "chars": 14}

→ {"cmd": "history"}
← {"ok": true, "texts": ["Ala ma kota. ", "Dzień dobry. "]}   // newest first

→ {"cmd": "reload"}
← {"ok": true, "applied": ["vad.min_silence_ms"], "deferred": [], "server_restart": true}

→ {"cmd": "subscribe"}
← {"event": "state", "status": {...}}        // stream – one line on every state change
← {"event": "language", "language": {"active": "en", "languages": ["pl", "en"]}}   // after every switch
← {"event": "job", "job_id": 17, "source": "continuous", "audio_s": 4.1, "processing_s": 1.9, "chars": 62, "result": "injected"}
← {"event": "speech_start", "utt": 41, "session_id": 3, "t": 81234.512, "t_start": 81234.262}
← {"event": "speech_end", "utt": 41, "session_id": 3, "t_start": 81234.262, "t_end": 81236.940}

→ {"cmd": "subscribe", "transcripts": true}
← ... // the same stream, and also:
← {"event": "transcript", "utt": 41, "session_id": 3, "job_ids": [17], "final": true, "text": "Jaka jest pogoda?", "language": "auto", "t_start": 81234.262, "t_end": 81236.940, "t_ready": 81237.610, "audio_s": 2.68, "stt_s": 0.61}
← {"event": "utterance_dropped", "utt": 42, "session_id": 3, "reason": "filtered"}
```

`job`, `state` and speech events **do not contain text**. Only the `history` response (task 5.2) and the `transcript` events of a subscription with `"transcripts": true` (task 6.2, ADR-019) contain text (12 §12.2).

*Implementation (task 2.5).* `subscribe` turns the connection into a stream; the client sends nothing more and closes the connection to end it. The first line is the current state (or, if the Controller does not answer, a `timeout` error and the connection closes). A `state` line follows every change of the displayed state (04 §4.7), with the full status document. A `job` line follows every finished job: `audio_s`, `processing_s` (`null` when the engine was not reached), `chars` (0 when nothing was entered), `source` ∈ `ptt`, `continuous`, `history` (a `last` insert, task 5.2; it does not count in the `stats` of 10.4), and `result` ∈ `injected`, `clipboard` (left in the clipboard), `failed`, `no_speech`, `filtered`, `cancelled`. *Speech events (task 6.1, ADR-019).* In continuous mode, a `speech_start` line follows when the VAD confirms speech, and a `speech_end` line follows when the utterance ends. They contain no text. `utt` is the utterance number. The daemon gives it at `speech_start`, and it increases across sessions. `session_id` is the continuous session. All times are `time.monotonic()` of the daemon (Linux `CLOCK_MONOTONIC`), so another process on the same machine can compare them with its own monotonic clock. The times come from the audio frames, not from the moment of processing:

- `t_start`: the start of the first speech frame of the utterance (also when the utterance was split at `max_segment_s`),
- `t`: the end of the frame that confirmed speech, about `min_speech_ms` after `t_start`,
- `t_end`: the end of the last speech frame (`p ≥ end_threshold`), not the end of the silence that ended the utterance. The line comes `min_silence_ms` after `t_end`, or at once on a stop. `t_end` is `null` when the session ends before the end of speech (cancel, an error), so that no client waits for a `speech_end` that never comes.

PTT recordings make no speech events.

*Transcripts (task 6.2, ADR-019).* `subscribe` with `"transcripts": true` adds the text events `transcript` (and `transcript_retracted`, task 6.4). A subscription without it never gets them; a value that is not a boolean gives `bad_request`. Every utterance of continuous mode gives exactly one of two events after its `speech_end`: one `transcript`, or one `utterance_dropped` when no text came from it. This is true also in ordinary dictation, where the text is injected as well. PTT jobs give no transcript. A `max_length` split makes several jobs for one utterance. Their texts are joined with one space into one `transcript`, in the order of the jobs (user decision 2026-10-10). Fields of `transcript`:

- `job_ids`: the jobs of the utterance, also the ones without text,
- `final`: `true` (task 6.4 adds speculative `false` texts),
- `text`: the processed text (08 §8.1, `text.commands` included) without leading and trailing spaces,
- `language`: `auto` under Parakeet; under whisper-server the active language when speech started,
- `t_start`, `t_end`: as in `speech_end`; `t_end` is `null` when the session ended before the end of speech,
- `t_ready`: the monotonic time when the text of the last part was ready, before injection,
- `audio_s`, `stt_s`: the sums over the parts with text.

`utterance_dropped` has `reason` ∈ `no_speech` (the segmenter found less than `min_speech_ms` of speech, or no part reported a text), `filtered`, `cancelled`, `failed`: the reason of the last part without text. Queued jobs that a cancel drains count as `cancelled`. A subscriber that falls 256 messages behind is disconnected so that it cannot hold daemon memory; the daemon closes all streams at shutdown.

Response to `cancel`: `{"ok": true, "injection_in_flight": false}`, or `true` in the second field if injection began before cancellation. With `true`, the CLI prints “Remaining jobs cancelled; injection already in progress may finish.” This applies to a single paste sequence or the current `type` chunk, never subsequent chunks or jobs.

Backlog (former task 3.2, not in v0.3; [15](15-implementation-plan.md) item 9): `status --watch --preview` would require `continuous.preview=true` and explicitly subscribes to a separate `preview` stream containing partial text. Regular `subscribe`, `status --watch`, and `status --json` do not receive preview content; having no subscribers disables those STT requests. Preview text remains in daemon RAM and the subscriber's terminal; users may redirect CLI output to a file themselves. It is never sent to logs or notifications.

## 10.3 Signals

| Signal | Action |
|---|---|
| `SIGTERM`, `SIGINT` | `ShutdownRequested` — clean shutdown (ungrab, stream close, socket removal), code 0 |
| `SIGHUP` | `ReloadRequested` (`systemctl --user reload local-stt`) |
| `SIGUSR1` | dumps internal state (queues, threads, counters) to the INFO log — hang diagnostics |

## 10.4 Status

### `local-stt status`

```text
local-stt 0.1.0 — IDLE
  engine     READY   whisper.cpp small-q8_0 @127.0.0.1:8178 (t=4)
  hotkeys    OK      PTT=Control_R  continuous=Shift+Control_R  language=Ctrl+Control_R
  language   pl (languages: pl, en)
  audio      default (closed)
  pipeline   0 queued, last: 3.8 s audio → 1.6 s (RTF 0.42) 2 min ago
  uptime     2 h 14 min
```

### `local-stt status --json`

```json
{
  "version": "0.1.0",
  "state": "LISTENING",
  "mode": "CONTINUOUS",
  "speech": true,
  "reconnecting": false,
  "engine": {"state": "READY", "name": "whisper.cpp", "model": "small-q8_0", "port": 8178},
  "hotkeys": {"state": "OK", "problems": []},
  "language": {"active": "pl", "languages": ["pl", "en"]},
  "audio": {"device": "default", "open": true, "overflows": 0},
  "pipeline": {"queued": 1, "queued_audio_s": 3.4, "busy": true, "generation": 5},
  "stats": {"jobs_ok": 42, "jobs_failed": 0, "jobs_filtered": 3, "rtf_avg_10": 0.44, "latency_avg_10_s": 1.7},
  "uptime_s": 8040
}
```

`state` is calculated according to the priorities in [04](04-state-machine.md) §4.7.

*Implementation (task 1.10).* Fields beyond the example above: `engine.threads` (for the `(t=4)` text), `hotkeys.push_to_talk` / `hotkeys.continuous_toggle` (the shortcuts in effect) and `hotkeys.state = "disabled"` with `hotkeys.enabled = false`; each problem is `{"hotkey", "value", "reason"}` ([07](07-hotkeys-x11.md) §7.6); `pipeline.paused`; `pipeline.last = {"audio_s", "stt_s", "ago_s"}` or `null` (the text form's `last:`). Statistics are counted by the Controller since startup: `jobs_ok` = successful `JobFinished`; `jobs_failed` = `JobFailed` plus `JobFinished` with `ok = false`; `jobs_filtered` = `JobDiscarded` with `no_speech` or `filtered` (cancelled jobs are not counted); `jobs_non_latin` = `JobFinished` whose text has letters outside the Latin script, injected unchanged and also counted in `jobs_ok` or `jobs_failed` (task 4.4, [06](06-stt-engine.md) §6.8); `rtf_avg_10` (`stt / audio`) and `latency_avg_10_s` (`total`) average the last 10 successful jobs and are `null` before the first. `audio.overflows` counts input overflows since startup (task 2.7, 05 §5.6). The text form shows no `stats` line, as in the example.

*Language (task 3.7).* `language` is the active language and `stt.languages`, the first being the startup language; `hotkeys.language_toggle` is the shortcut in effect (`""` = none). `status --watch` appends ` [EN]` to the state while the active language differs from the startup language, and rewrites the line on every `language` event.

*Parakeet (tasks 4.3–4.5).* The examples above show whisper-server. Under Parakeet, `engine.name` is `parakeet` and `engine.model` is `parakeet-tdt-0.6b-v3-int8`. The active language is `auto`, and `status --watch` does not append it. Live output 2026-10-08:

```text
local-stt 0.5.0 — IDLE
  engine     READY   parakeet parakeet-tdt-0.6b-v3-int8 @127.0.0.1:8178 (t=4)
  hotkeys    OK      PTT=Control_R  continuous=Shift+Control_R  language=Ctrl+Control_R
  language   auto (languages: pl, en)
```

A rejected language switch returns `{"ok": false, "error": "language_unsupported", "message": "Parakeet detects the language itself; stt.languages is for whisper-server"}`; the CLI prints `rejected: <message>` and exits with code 4.

`status --watch` subscribes to events and rewrites one terminal line. It can be used in a status bar (for example, the GNOME “Executor” extension or a future tray; see [15](15-implementation-plan.md)). *Implementation (task 2.5):* the line is the displayed state (`state`); when stdout is not a terminal, every change is printed as a new line instead, which suits status bars that read lines. `status --watch --json` prints the raw stream, `job` events included. Ctrl+C exits with 0; if the daemon stops, `daemon stopped` and exit code 3.

## 10.5 `local-stt doctor`

Checks and prints `OK` / `WARN` / `FAIL` with a suggested fix:

| Test | FAIL/WARN when | Suggestion |
|---|---|---|
| session | type from `loginctl show-session <Display> -p Type` ≠ `x11` ([07](07-hotkeys-x11.md) §7.5) | “Select the Ubuntu on Xorg session on the login screen” |
| `DISPLAY` in `systemctl --user show-environment` | missing | `dbus-update-activation-environment --systemd DISPLAY XAUTHORITY` |
| config | validation error | validator message |
| STT model | missing file / bad checksum; under Parakeet, any of the model directory's files | `local-stt models pull …` |
| VAD model | missing/bad checksum | `local-stt models pull silero-vad` |
| `whisper-server` binary | missing / fails to run (`--help`) / `.whisper-tag` ≠ tag from `install.sh` | `scripts/install.sh --rebuild-whisper` |
| `secret`, `whisper-server.env` | missing / permissions other than 0600 | `scripts/install.sh` |
| engine service: `local-stt-engine` (Parakeet) or `local-stt-whisper`, as `stt.engine` selects | inactive | `systemctl --user status <unit>` |
| `GET /health` (with the prefix from `secret`) | no response / `loading model` longer than `stt.startup_timeout_s` | `journalctl --user -u <unit>` |
| port | listening on more than loopback (`ss -ltn`) | privacy FAIL |
| hotkeys | daemon running → `hotkeys` state over IPC (`degraded` = FAIL with problem list); daemon not running → test grab on a separate connection (`BadAccess` = FAIL) | identifies the conflicting GNOME shortcut (`gsettings list-recursively` + matching) |
| microphone | open for 1 s → RMS | WARN when < -60 dBFS: “check mute/input level in sound settings” |
| `xdotool` | missing | WARN: `type` backend unavailable |
| `pw-play` / `paplay` | both missing | WARN: no sounds |
| `notify-send` | missing | WARN: no notifications |
| CPU governor / power | `powersave` on battery | INFO: latency impact |

*Implementation (task 1.12, `doctor.py`; user decisions 2026-10-03).* `local-stt doctor [--config P]` prints one line per check as it finishes, in table order, with `OK`, `INFO`, `WARN`, `FAIL` or `SKIP`, and ends with `N FAIL, N WARN, N OK`. Exit code `1` if any check FAILs, otherwise `0` (WARN and INFO do not fail). With an invalid config, the config line is FAIL with every validator message, and the checks that need it (STT/VAD model, `/health`, port, hotkeys, microphone) are `SKIP config invalid`; the others still run. Details:

- The expected whisper.cpp tag is `WHISPER_TAG` in `stt/whisper_server.py`; a unit test keeps it equal to `DEFAULT_WHISPER_TAG` in `install.sh` (the installed venv need not have the repo).
- STT/VAD model: missing file or SHA256 mismatch against `models.sha256` is FAIL; a model file without a pinned checksum is OK with a note. The VAD model is SKIP with `vad.enabled = false`. `local_stt.models` is imported only inside this check (12 §12.2).
- `secret` must also contain 32 lowercase hex characters.
- Engine (task 4.5): the service, `/health` and STT model checks follow `stt.engine`. With an invalid config, the service check uses the default engine (Parakeet). The `whisper-server` binary and `whisper-server.env` checks run under both engines, because a reload can switch to whisper-server. Live 2026-10-08 under Parakeet: 17 OK.
- `/health` is SKIP while the engine service is not active. A server still loading the model (HTTP 503) is polled until `stt.startup_timeout_s` has passed since the unit became active (`ActiveEnterTimestampMonotonic`); a refused connection or other HTTP status (e.g. a wrong request path) is FAIL.
- Port: nothing listening is OK; any listener on `stt.port` whose address is not loopback (`0.0.0.0`, `[::]`, `*`) is FAIL.
- Hotkeys: a running daemon's `degraded` is FAIL with its problem list; `disabled` is INFO; an IPC error other than “not running” is FAIL. **GNOME shortcuts do not cause `BadAccess`** (tested on the reference machine: a core grab of `Super+space` succeeds although `switch-input-source = ['<Super>space', …]`), so the GNOME lookup runs for every grabbed shortcut, not only on FAIL: `gsettings list-recursively` plus custom keybindings (their relocatable schema is not listed there), GTK accelerators (`<Primary>`/`<Control>` → `Ctrl`, `<Mod1>` → `Alt`, `<Mod4>` → `Super`) compared with the hotkey's modifiers and keysym (case-insensitive). A match makes an otherwise OK result WARN “GNOME may take the keys”. Which client then receives the key was not established (an ad-hoc XTest test received no events even for an unbound key, so it was inconclusive).
- Microphone: 1 s through `AudioCapture`; FAIL when it cannot be opened or delivers no frames; WARN below -60 dBFS (with the echo-cancel note from [05](05-audio-and-vad.md) §5.7) or when a configured `audio.device` is not the source actually used.
- Power: on battery = a `Battery` supply that is `Discharging`, or `Mains`/`USB` supplies (excluding `scope = Device`) that exist and are all offline; no supplies = AC.
- `session_type()` (loginctl, `XDG_SESSION_TYPE` fallback) is also meant for the daemon's startup check (task 1.13).

## 10.6 User feedback

### Sounds (`feedback.sounds`)

At startup, the daemon generates six short WAV files (sine waves with 5 ms fade-in/out, volume `sound_volume`) in `$XDG_RUNTIME_DIR/local-stt/sounds/`:

| Sound | Pattern | Duration |
|---|---|---|
| `start` | 2 ascending tones, 660→880 Hz | 130 ms |
| `stop` | 2 descending tones, 880→660 Hz | 130 ms |
| `cancel` | 1 tone at 440 Hz | 120 ms |
| `error` | 3× 330 Hz with pauses | 250 ms |
| `language` | 1 tone at 880 Hz: the startup language (first of `stt.languages`) is now active (task 3.7) | 80 ms |
| `language_alt` | 2× 880 Hz with a pause: any other language is now active; the notification names it | 180 ms |

**Event → sound matrix** (the single source of truth; [04](04-state-machine.md) and [05](05-audio-and-vad.md) refer to it):

| Situation | Sound |
|---|---|
| PTT: first microphone frame (`RecordingStarted`); samples through the end of the sound + 80 ms are discarded | `start` |
| PTT released, recording accepted into the queue | `stop` |
| PTT shorter than `ptt.min_duration_ms` (press→release time) | no `stop` and no transcription; `start` may already have sounded after the first frame |
| PTT cancelled (cancel key, `local-stt cancel`) | `cancel` |
| PTT recording with no speech (`JobDiscarded(no_speech)`) | `cancel` |
| Continuous mode enabled (sound **before** opening the microphone) | `start` |
| Language switched (hotkey, `local-stt language`) while the microphone is closed | `language` / `language_alt`; no sound while recording or in continuous mode, where the tone would be recorded (notification only) |
| Continuous mode disabled (toggle, backlog, engine DOWN, microphone after 3 attempts) | `stop`; additionally `error` for backlog/DOWN/microphone |
| `local-stt cancel` discarding anything | `cancel` |
| Rejection: engine unavailable, microphone open error, audio error during PTT | `error` |
| Text injected | none (the result is visible in the window) |
| `JobFailed`, unconfirmed paste | no sound, notification only |

Playback: `subprocess.Popen(["pw-play", path])` (fallback `paplay`), without waiting for completion. Sounds never overlap: a sound requested while another is still playing starts 70 ms after it ends (a timer thread), and `play()` returns the time until the queued sound ends, so the start-sound masking window covers the delay. Added in the v0.2 acceptance (2026-10-04): after a PTT without speech the `cancel` sound came ~70 ms after `stop` and was inaudible under it (checked by ear: overlapped vs. a 70 ms gap); the same applied to `stop` + `error` when the backlog limit is reached or the microphone is lost. The separate process does not interfere with the PortAudio input stream. When continuous mode starts, the microphone opens only after the `start` sound—the `CaptureOpenDue` event occurs 150 ms later ([04](04-state-machine.md) §4.3).

### Notifications (`feedback.notifications`)

`notify-send -a local-stt -i audio-input-microphone -p [-r <id>] [-e] "<title>" "<body>"` (libnotify-bin 0.8.3 on Ubuntu 24.04):

- `-p` prints the notification ID, which the daemon remembers. The next invocation with `-r <id>` **replaces** the preceding notification, so they do not accumulate. GNOME Shell ignores the `x-canonical-private-synchronous` hint and `-t`, so we do not use them.
- `-e` (transient) is used for informational notifications (`all` level), so they do not remain in the notification center. Errors are not transient.

| `errors` level (default) | Additionally with `all` |
|---|---|
“Language: XX” after a language switch (key `language`, task 3.7; under Parakeet “Language: automatic (Parakeet)”, task 4.3), engine unavailable, microphone error, “microphone appears muted,” “recording limit reached,” transcription failed (aggregated), “paste failed—text is in the clipboard,” “no active field—text is in the clipboard,” “transcription cannot keep up—dictation stopped,” hotkey conflict at startup, engine restart failed after reload | “Dictation enabled/disabled,” “Engine ready: <model>” |

Notifications **never contain transcribed text**.

*Implementation (task 2.8).* “Dictation enabled” is shown once the microphone has opened (not when the toggle is accepted, so a failed start shows only the error), and “Dictation disabled” when the session ends: at the confirmed stop flush, or at `cancel` of a session whose microphone was open; not at daemon shutdown. Both use the notification key `dictation`, separate from the `continuous` key of the error notifications (backlog, engine DOWN), so the informational one never replaces an error on screen. `STATUS=` (11 §11.5) carries the same displayed state as `status` and `status --watch`, e.g. `LISTENING (speech), transcribing 1`.

*Implementation (task 1.11, `feedback.py`).* `play()` and `notify()` are called from the controller thread and never wait (user decision 2026-10-03): a sound is a `pw-play` process (`paplay` if `pw-play` is missing; finished processes are reaped on the next `play()`), and a notification goes through a queue to the `feedback` thread, which runs `notify-send -p` and remembers the returned ID per notification key (`engine`, `audio`, `limit`, `clipboard`, `job_failed`, …). The title and body follow `--`, so text starting with `-` is not taken as an option. WAV files are 48 kHz mono s16; the `error` pattern is three 50 ms tones separated by 50 ms pauses. A `sound_volume` reload rewrites the files atomically. Without a player, with `sounds = false`, or when the files cannot be written, `play()` returns `None`, so there is no start-sound masking window ([05](05-audio-and-vad.md) §5.2).

**Tested on the reference machine (2026-10-03):** all four sounds play through `pw-play` at `sound_volume = 0.4` (the user confirmed sound and volume); `Popen` returns in 0.5–3 ms; `notify-send -p` prints the ID, and `-r <id>` returns the same ID and replaces the notification (only the second entry remained in the GNOME notification centre); a `-e` notification is shown and does not stay in the notification centre. Not measured: the delay between starting `pw-play` and the sound actually reaching the microphone, which the start-sound masking window (05 §5.2) assumes is covered by the 80 ms margin.
