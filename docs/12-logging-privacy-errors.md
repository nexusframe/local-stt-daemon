# 12. Logging, privacy, and error handling

## 12.1 Logging

- Mechanism: the standard library `logging` module. Output goes to stderr and, under systemd, to journald.
- Levels: `INFO` (20), `DEBUG` (10), and a custom `TRACE` (5). Level sources in precedence order: `--log-level`, `LOCAL_STT_LOG_LEVEL`, `logging.level`.
- Under systemd, detected through `JOURNAL_STREAM`, the format is `<PRI>logger: message`, where `PRI` is the sd-daemon priority (`<3>` error, `<4>` warning, `<6>` info, `<7>` debug/trace). journald adds the timestamp, and `journalctl -p warning` filters correctly. journald reads the prefix per line, so every line of a multi-line record (a traceback) carries it. systemd mode applies only when stderr is the stream named in `JOURNAL_STREAM` (`dev:inode`), not merely when the variable is set. Verified 2026-10-03 in a transient `systemd-run --user` unit: priorities 6/4/7/3, and all traceback lines at 3. Outside systemd, the format is `2026-09-17 10:21:03.412 INFO  logger: message`.
- Logger names: `local_stt.controller`, `.audio`, `.vad`, `.stt`, `.text`, `.inject`, `.hotkeys`, `.ipc`.

### What is logged at each level

| Level | Examples |
|---|---|
| INFO | start/stop, versions (daemon, whisper.cpp, model), audio device, `engine` changes, `mode` changes, **job timing line**, reload, warnings from startup `doctor` checks |
| DEBUG | Controller events and transitions, `VAD speech started/ended (speech_ms, cut)`, reason a recording was rejected, `filtered: <reason>`, backend selection and `WM_CLASS`, HTTP timings |
| TRACE | every 1 s: average/maximum VAD `p` and RMS dBFS; every X11 event; raw HTTP response headers (without body) |

Job timing line (`logging.timings`):

```text
job=17 src=continuous seq=4 audio=3.84s queued=0.21s stt=1.62s rtf=0.42 text=2ms inject=234ms total=2.07s chars=62 backend=clipboard result=injected
```

Field meanings:

- `audio` — duration of the audio sent to the engine: for PTT with `vad.enabled`, after VAD trimming (05 §5.3); `rtf` uses it too,
- `queued` — time spent waiting in the queue,
- `inject` — full injector call duration: waiting for modifiers/PTT, writing the clipboard, sending keys, confirmation, and restoration (including the additional 150 ms).
- `total` — time from releasing PTT or the end of VAD silence detection until the injector returns, including audio finalization and the entire `inject`. For PTT, this is metric N2. The measurement start is not reset after draining the frame queue; the sum of stage fields may not include all finalization overhead.

### What is never logged

- Raw audio, either in logs or files.
- Transcript content, unless `logging.log_text = true`. In that case, an additional DEBUG line `text job=17: "…"` appears, along with a one-time startup WARNING: `log_text is enabled — transcripts will be stored in the journal`.
- Clipboard content (written or restored).
- The `prompt` field (it may contain previously dictated text).

## 12.2 Privacy

Principle: **audio and text never leave the computer and are written to disk only at the user's explicit request.**

| Area | Guarantee | Enforcement |
|---|---|---|
| Runtime network | the daemon process connects only to `127.0.0.1:<stt.port>` (HTTP to the server) and the Unix IPC socket | host hard-coded to `127.0.0.1` (not configurable); modules imported by the daemon use only `http.client` for loopback — Internet networking code (`models.py`: `urllib` to HF) resides in a module loaded exclusively by the `models pull` command, as verified by an import test; `doctor` checks the server listener |
| Internet | required only by `install.sh` and `local-stt models pull` | the daemon has no code path that connects to the Internet; the e2e test runs the daemon and server in a separate network namespace containing only loopback ([14](14-tests.md) §14.3) |
| Disk | no audio or text is written; exceptions: `record-corpus` (directory specified by the user), `bench` (numeric results + corpus transcripts), `log_text=true` | code review; `tmp` is unused (WAV in `BytesIO`) |
| Core dumps | audio in RAM is excluded from core dumps | `LimitCORE=0` in both units |
| Other local users | cannot control the daemon | `0600` socket in a `0700` directory + `SO_PEERCRED` |
| Server port | a web page can send a request to `127.0.0.1` without a CORS preflight (for example, a multipart `POST`), which could replace the model through `/load` or block the server with a long `/inference` request | all endpoints are placed under a random `--request-path` from the `secret` file (0600), which the page does not know; other processes owned by **the same user** can read it (accepted: they already have access to the microphone and screen) |
| Clipboard | dictated text remains in CLIPBOARD for ~0.2–1 s (and deliberately stays there after an unconfirmed paste); clipboard-history managers may save it | documentation + `injection.backend = "type"` for sensitive use cases |
| Preview (v0.3) | partial transcription only for an explicit `status --watch --preview` with `continuous.preview=true`; disabled by default | separate IPC subscription; no content in ordinary status, `job` events, logs, or notifications (10 §10.2) |
| Notifications | never contain content | [10](10-cli-ipc-status.md) §10.6 |

## 12.3 Error matrix

| # | Error | Detection | User-facing response | Log |
|---|---|---|---|---|
| E1 | Invalid config at startup | validator | process exits with code 78 (no restart); `systemctl status` shows the message | ERROR with the list of problems |
| E2 | Invalid config on reload | validator | old config remains active; CLI prints the errors | ERROR |
| E3 | Non-X11 session | startup (`loginctl`, [07](07-hotkeys-x11.md) §7.5) | code 78, no restart; missing `DISPLAY` in an X11 session → code 1 and restart (limit 5/60 s) | ERROR |
| E4 | Hotkey already taken (`BadAccess`) | grab | daemon remains running; `hotkeys: degraded`; startup notification | ERROR with shortcut name |
| E5 | X11 connection lost during operation | `ConnectionClosedError` | exit 0 without X11 operations or restart ([07](07-hotkeys-x11.md) §7.4); connection error **at startup** → exit 1 and restart (limit 5/60 s) | WARNING |
| E6 | Server unresponsive at startup | `/health` | `STARTING` until `startup_timeout_s`, then `DOWN`; hotkey → `error` sound + notification | WARNING → ERROR |
| E7 | Server fails during operation | connection error in worker | `engine=DOWN`, queue paused and resumed after `READY`; if it has not returned after `startup_timeout_s`, all jobs → `JobFailed` + one aggregate notification; continuous → stopped with flush ([04](04-state-machine.md) §4.3, §4.5) | ERROR |
| E8 | Transcription timeout | `socket.timeout` | 1 retry after 1 s, then `JobFailed` (continuous continues) | ERROR with audio duration |
| E9 | HTTP 4xx/5xx from server | status | `JobFailed` without retry for 4xx, with retry for 5xx | ERROR with status code and body ≤ 200 characters |
| E10 | Microphone missing / busy / disconnected | [05](05-audio-and-vad.md) §5.6 | described there | WARNING/ERROR |
| E11 | Queue cannot keep up | `queued_audio_s > max_backlog_s` | continuous disabled, queue allowed to finish | WARNING |
| E12 | Unconfirmed paste | no matching selection request within `paste_timeout_ms` ([08](08-text-injection.md) §8.5 step 7) | text remains in clipboard + notification | WARNING |
| E13 | `xdotool` error / timeout | exit code | `InjectResult(ok=False)`, text remains in clipboard (deliberately sacrificing its previous contents — dictated text must not be lost), notification | ERROR with xdotool stderr |
| E14 | Exception in a thread (bug) | `threading.excepthook` | critical threads ([02](02-architecture.md) §2.2: controller, hotkeys, audio-consumer, pipeline, clipboard-owner) → log + `os._exit(1)` (because `sys.exit` in a thread terminates only that thread) → systemd restart; IPC and engine-monitor threads → log + restart the thread | CRITICAL with traceback |
| E15 | IPC socket occupied by a running instance | `connect()` OK | exit 1 with a message | ERROR |
| E16 | Server restart on reload fails (for example, damaged model) | `systemctl` code ≠ 0 or `DOWN` after `startup_timeout_s` | “Failed to start engine with new config” notification; `doctor` identifies the cause; the old config is **not** restored automatically (the env file has already changed) | ERROR |

Overriding rule: **no exception is ever swallowed silently.** It is either handled according to the table or terminates the process so systemd can restart it.
