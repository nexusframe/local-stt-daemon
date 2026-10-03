# 10. CLI, IPC, and status

## 10.1 `local-stt` commands

One entry point (`[project.scripts] local-stt = "local_stt.cli:main"`), with subcommands implemented through `argparse`. **This is the only complete command list** — other documents refer here.

| Command | Version | Action | Requires a running daemon |
|---|---|---|---|
| `local-stt daemon [--config P] [--log-level L]` | v0.1 | runs the daemon in the foreground (as systemd starts it) | — |
| `local-stt status [--json] [--watch [--preview]]` | v0.1 / `--watch` v0.2 / `--preview` v0.3 | daemon status (10.4) | yes (otherwise: `daemon not running`, code 3) |
| `local-stt ptt start\|stop` | v0.1 | equivalent to pressing/releasing the PTT key | yes |
| `local-stt toggle` | v0.2 | enables/disables continuous mode | yes |
| `local-stt cancel` | v0.1 | cancels recording/continuous mode and pending jobs; an injection operation already underway may finish (08 §8.3) | yes |
| `local-stt reload` | v0.1 | reloads the config; reports applied and deferred changes and whether the server will restart ([04](04-state-machine.md) §4.6) | yes |
| `local-stt doctor` | v0.1 | environment diagnostics (10.5) | no |
| `local-stt devices` | v0.1 | lists PipeWire sources (`pactl -f json list sources`, excluding `.monitor`): node name for `audio.device` + description; marks the default | no |
| `local-stt models list\|pull NAME\|verify` | stage 0 | models in `models_dir`, HF download, SHA256 verification (the only command that uses the Internet) | no |
| `local-stt models list --bench` | v0.3 | lists models with the latest benchmark results | no |
| `local-stt transcribe FILE.wav [--model M]` | stage 0 | one-shot file transcription (test without microphone or hotkeys); without `--model`, uses the running server; with `--model`, uses a **temporary** server on a random free loopback port (like `bench`, [13](13-benchmark.md) §13.4) to avoid changing the service model | without `--model`: yes |
| `local-stt record-corpus DIR [--long]` | stage 0 | records the benchmark corpus ([13](13-benchmark.md) §13.2) | no |
| `local-stt bench [--quick] [--dataset DIR] …` | stage 0 | model matrix on temporary servers ([13](13-benchmark.md) §13.4) | no (the server service should be stopped) |
| `local-stt bench --soak …` | v0.2 | 10-minute continuous-mode test through the real Segmenter | no |
| `local-stt bench report DIR` | stage 0 | Markdown results report | no |

Exit codes: `0` OK, `1` general error, `2` usage error, `3` daemon not running, `4` rejected by the daemon (for example `toggle` when the engine is DOWN), `78` configuration error.

## 10.2 IPC — control socket

- Path: `$XDG_RUNTIME_DIR/local-stt/control.sock` (`/run/user/1000/…`, tmpfs, private to the user).
- The directory has `0700` permissions and the socket `0600`. The daemon also checks `SO_PEERCRED`: the client's `uid` must equal `os.getuid()`, otherwise it closes the connection.
- Protocol: **JSON Lines** (one request = one UTF-8 line terminated by `\n`; one response = one line).
- Server: the `ipc-server` thread (`socketserver.ThreadingUnixStreamServer`). State-changing commands become Controller events. The response waits for their processing through a `concurrent.futures.Future` with a 5 s timeout.
- Stale socket: at startup, if the file exists and `connect()` fails, the file is removed. If `connect()` succeeds, the daemon is already running: ERROR `another instance is running`, exit code 1.
- Several requests may be sent over one connection; a connection that sends nothing for 60 s is closed. A connection from another uid is closed without a response.
- Errors produced by the IPC layer itself, before the Controller sees the request (task 1.10): `bad_request` (invalid JSON or UTF-8, not an object, `ptt` without `start`/`stop`), `unknown_command`, `unsupported` (`subscribe` until v0.2), `too_long` (line > 64 KiB; the connection is then closed), `timeout` (no Controller response within 5 s). The CLI maps them, like Controller rejections, to exit code 4; `reload` with an invalid config (`{"ok": false, "errors": [...]}`) exits with code 78.

### Requests and responses

```json
→ {"cmd": "status"}
← {"ok": true, "status": { ...see 10.4... }}

→ {"cmd": "ptt", "action": "start"}
← {"ok": true}

→ {"cmd": "toggle"}
← {"ok": false, "error": "engine_down", "message": "STT engine unavailable"}

→ {"cmd": "reload"}
← {"ok": true, "applied": ["vad.min_silence_ms"], "deferred": [], "server_restart": true}

→ {"cmd": "subscribe"}
← {"event": "state", "status": {...}}        // stream – one line on every state change
← {"event": "job", "job_id": 17, "source": "continuous", "audio_s": 4.1, "processing_s": 1.9, "chars": 62, "result": "injected"}
```

`job` events **do not contain text**.

Response to `cancel`: `{"ok": true, "injection_in_flight": false}`, or `true` in the second field if injection began before cancellation. With `true`, the CLI prints “Remaining jobs cancelled; injection already in progress may finish.” This applies to a single paste sequence or the current `type` chunk, never subsequent chunks or jobs.

In v0.3, `status --watch --preview` requires `continuous.preview=true` and explicitly subscribes to a separate `preview` stream containing partial text. Regular `subscribe`, `status --watch`, and `status --json` do not receive preview content; having no subscribers disables those STT requests. Preview text remains in daemon RAM and the subscriber's terminal; users may redirect CLI output to a file themselves. It is never sent to logs or notifications.

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
  hotkeys    OK      PTT=Control_R  continuous=Shift+Control_R
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
  "audio": {"device": "default", "open": true, "overflows": 0},
  "pipeline": {"queued": 1, "queued_audio_s": 3.4, "busy": true, "generation": 5},
  "stats": {"jobs_ok": 42, "jobs_failed": 0, "jobs_filtered": 3, "rtf_avg_10": 0.44, "latency_avg_10_s": 1.7},
  "uptime_s": 8040
}
```

`state` is calculated according to the priorities in [04](04-state-machine.md) §4.7.

*Implementation (task 1.10).* Fields beyond the example above: `engine.threads` (for the `(t=4)` text), `hotkeys.push_to_talk` / `hotkeys.continuous_toggle` (the shortcuts in effect) and `hotkeys.state = "disabled"` with `hotkeys.enabled = false`; each problem is `{"hotkey", "value", "reason"}` ([07](07-hotkeys-x11.md) §7.6); `pipeline.paused`; `pipeline.last = {"audio_s", "stt_s", "ago_s"}` or `null` (the text form's `last:`). Statistics are counted by the Controller since startup: `jobs_ok` = successful `JobFinished`; `jobs_failed` = `JobFailed` plus `JobFinished` with `ok = false`; `jobs_filtered` = `JobDiscarded` with `no_speech` or `filtered` (cancelled jobs are not counted); `rtf_avg_10` (`stt / audio`) and `latency_avg_10_s` (`total`) average the last 10 successful jobs and are `null` before the first. `audio.overflows` stays 0 until v0.2 (task 2.7). The text form shows no `stats` line, as in the example.

`status --watch` subscribes to events and rewrites one terminal line. It can be used in a status bar (for example, the GNOME “Executor” extension or a future tray; see [15](15-implementation-plan.md)).

## 10.5 `local-stt doctor`

Checks and prints `OK` / `WARN` / `FAIL` with a suggested fix:

| Test | FAIL/WARN when | Suggestion |
|---|---|---|
| session | type from `loginctl show-session <Display> -p Type` ≠ `x11` ([07](07-hotkeys-x11.md) §7.5) | “Select the Ubuntu on Xorg session on the login screen” |
| `DISPLAY` in `systemctl --user show-environment` | missing | `dbus-update-activation-environment --systemd DISPLAY XAUTHORITY` |
| config | validation error | validator message |
| STT model | missing file / bad checksum | `local-stt models pull …` |
| VAD model | missing/bad checksum | `local-stt models pull silero-vad` |
| `whisper-server` binary | missing / fails to run (`--help`) / `.whisper-tag` ≠ tag from `install.sh` | `scripts/install.sh --rebuild-whisper` |
| `secret`, `whisper-server.env` | missing / permissions other than 0600 | `scripts/install.sh` |
| `local-stt-whisper` service | inactive | `systemctl --user status local-stt-whisper` |
| `GET /health` (with the prefix from `secret`) | no response / `loading model` longer than `stt.startup_timeout_s` | `journalctl --user -u local-stt-whisper` |
| port | listening on more than loopback (`ss -ltn`) | privacy FAIL |
| hotkeys | daemon running → `hotkeys` state over IPC (`degraded` = FAIL with problem list); daemon not running → test grab on a separate connection (`BadAccess` = FAIL) | identifies the conflicting GNOME shortcut (`gsettings list-recursively` + matching) |
| microphone | open for 1 s → RMS | WARN when < -60 dBFS: “check mute/input level in sound settings” |
| `xdotool` | missing | WARN: `type` backend unavailable |
| `pw-play` / `paplay` | both missing | WARN: no sounds |
| `notify-send` | missing | WARN: no notifications |
| CPU governor / power | `powersave` on battery | INFO: latency impact |

## 10.6 User feedback

### Sounds (`feedback.sounds`)

At startup, the daemon generates four short WAV files (sine waves with 5 ms fade-in/out, volume `sound_volume`) in `$XDG_RUNTIME_DIR/local-stt/sounds/`:

| Sound | Pattern | Duration |
|---|---|---|
| `start` | 2 ascending tones, 660→880 Hz | 130 ms |
| `stop` | 2 descending tones, 880→660 Hz | 130 ms |
| `cancel` | 1 tone at 440 Hz | 120 ms |
| `error` | 3× 330 Hz with pauses | 250 ms |

**Event → sound matrix** (the single source of truth; [04](04-state-machine.md) and [05](05-audio-and-vad.md) refer to it):

| Situation | Sound |
|---|---|
| PTT: first microphone frame (`RecordingStarted`); samples through the end of the sound + 80 ms are discarded | `start` |
| PTT released, recording accepted into the queue | `stop` |
| PTT shorter than `ptt.min_duration_ms` (press→release time) | no `stop` and no transcription; `start` may already have sounded after the first frame |
| PTT cancelled (cancel key, `local-stt cancel`) | `cancel` |
| PTT recording with no speech (`JobDiscarded(no_speech)`) | `cancel` |
| Continuous mode enabled (sound **before** opening the microphone) | `start` |
| Continuous mode disabled (toggle, backlog, engine DOWN, microphone after 3 attempts) | `stop`; additionally `error` for backlog/DOWN/microphone |
| `local-stt cancel` discarding anything | `cancel` |
| Rejection: engine unavailable, microphone open error, audio error during PTT | `error` |
| Text injected | none (the result is visible in the window) |
| `JobFailed`, unconfirmed paste | no sound, notification only |

Playback: `subprocess.Popen(["pw-play", path])` (fallback `paplay`), without waiting for completion. The separate process does not interfere with the PortAudio input stream. When continuous mode starts, the microphone opens only after the `start` sound—the `CaptureOpenDue` event occurs 150 ms later ([04](04-state-machine.md) §4.3).

### Notifications (`feedback.notifications`)

`notify-send -a local-stt -i audio-input-microphone -p [-r <id>] [-e] "<title>" "<body>"` (libnotify-bin 0.8.3 on Ubuntu 24.04):

- `-p` prints the notification ID, which the daemon remembers. The next invocation with `-r <id>` **replaces** the preceding notification, so they do not accumulate. GNOME Shell ignores the `x-canonical-private-synchronous` hint and `-t`, so we do not use them.
- `-e` (transient) is used for informational notifications (`all` level), so they do not remain in the notification center. Errors are not transient.

| `errors` level (default) | Additionally with `all` |
|---|---|
| engine unavailable, microphone error, “microphone appears muted,” “recording limit reached,” transcription failed (aggregated), “paste failed—text is in the clipboard,” “no active field—text is in the clipboard,” “transcription cannot keep up—dictation stopped,” hotkey conflict at startup, engine restart failed after reload | “Dictation enabled/disabled,” “Engine ready: <model>” |

Notifications **never contain transcribed text**.
