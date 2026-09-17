# 14. Test strategy

Tools: `pytest`, `pytest-timeout`, `ruff` (lint + format), and `mypy --strict` for `src/local_stt` (excluding `bench/`). Run `pytest -m "not needs_whisper and not needs_x11 and not needs_audio and not e2e"` for the fast suite and `pytest` for the complete local suite.

## 14.1 Pyramid

| Level | Scope | Environment | Marker |
|---|---|---|---|
| Unit | pure logic | no external resources | — |
| Integration: engine | `WhisperServerEngine` ↔ real `whisper-server` (`ggml-base-q5_1` for speed) | local build | `needs_whisper` |
| Integration: X11 | hotkeys, clipboard, XTest | `xvfb-run` (`xvfb` package installed by `install.sh --dev`; no Mutter) | `needs_x11` |
| Integration: audio | device opening, resampling | real PipeWire | `needs_audio` |
| End-to-end | complete daemon | Xvfb + `FileAudioSource` + real server | `e2e`, `needs_x11`, `needs_whisper` |
| Manual acceptance | real GNOME session, microphone, applications | reference machine | 14.4 checklist |

## 14.2 Unit tests (required)

| Module | What to test | Technique |
|---|---|---|
| `controller` | **every row of the table in [04](04-state-machine.md) §4.3** + `invalid_in_mode` IPC rejections + ignored events; §4.6 reload groups (live / deferred / server restart); stale events after `cancel → start` in the same mode; distinction between `FlushDone(stop/reconnect)` and operation IDs; stopping during a reconnect flush preserves the final segment exactly once and does not reopen the microphone; cancelling a delayed start resolves the IPC Future | parameterized test: (state, event, conditions) → (actions on fakes, new state) |
| `pipeline` | `seq` order; generation-based cancellation at all three checkpoints and while waiting for the injector; global cancel during a new PTT also removes older jobs, while Esc leaves them; one retry for 5xx/timeout; pause on DOWN and resume; aggregate `JobFailed` after timeout; RMS gate in 100 ms windows; per-session prompt context; 200-character limit | `FakeEngine` with delays and errors, `RecordingInjector` |
| `segmenter` | start after `min_speech_ms`; rejection of short impulses; hysteresis (p oscillating between thresholds does not end speech); end after `min_silence_ms`; pre-roll and padding; cut at `max_segment_s` in the longest silence / lowest p; `flush()` | `ScriptedVad` returning a prescribed p sequence; assertions at sample boundaries |
| `vad` | tensor shapes, transfer of 64 context samples and state, `reset()` | real `silero_vad.onnx` on silence (p < 0.1) and speech (max p > 0.8) fixtures |
| `recorder` / `audio.consumer` | start-sound window masking; duration limit; `AudioClip`; final frames waiting in the queue are processed before `RecordingFinished`; rejection of frames outside the time boundary and from a foreign `capture_id`; discarding an old recording does not remove a new one | synthetic frames and controlled command ordering |
| `text.filters` | every hallucination pattern (positive and negative—the Polish sample “Dziękuję za uwagę, a teraz…” **must not** be removed); `no_speech`+`logprob` (only together); n-gram loops; prompt echo | case table |
| `text.processor` | steps 2–7 from [08](08-text-injection.md) §8.2, including `max_length` → period removal and lowercase; joining `" trans"` + `"krypcja"` without an extra space; preserving spaces between words; separator after a removed segment | table |
| `hotkeys.spec` | parsing, errors, rejection of AltGr/Super_L/Control_L/Shift_L, `ptt_cancel_key` validation | |
| `config` | defaults; every rule from [09](09-configuration.md) §9.3; unknown key → error; `whisper-server.env` generation | |
| `audio.wav` | float32 → WAV s16: header, clipping, round trip | |
| `stt.whisper_server` | multipart construction; `verbose_json` parsing (fixture from a real v1.9.4 response); HTTP/timeout error mapping | local `http.server` stub |
| `ipc` | protocol, unknown command, overlong line (> 64 KiB), `SO_PEERCRED` (different uid → reject; monkeypatch test) | |
| `bench.wer` / metrics / selection | WER/CER on known examples, Polish-character normalization; cuts inside a word, at a boundary, and within silence; `base` excluded from production; a model slow without `audio_ctx` but fast with it is not eliminated in stage 1; stage 0 does not confirm N2 | |
| `feedback` | generated WAV files have the correct duration and no DC offset; `notify-send` is called with expected arguments and **without transcript text** | mocked `subprocess` |

Coverage target: ≥ 90% of lines for `controller`, `pipeline`, `segmenter`, `text/`, and `config`. No hard threshold for other modules.

## 14.3 Integration and E2E

1. **`needs_whisper`.** Start `whisper-server` with `base-q5_1` on a random port → `transcribe(fixtures/pl_short.wav)`:
   - non-empty text,
   - time < 30 s,
   - no connection during startup → `/health` returns 200; 503 is not required during loading (the server begins listening after loading the model). A separate HTTP stub verifies defensive 503 handling and the STARTING → READY transition,
   - kill the server mid-job → `engine=DOWN`, queue paused; after restart the job succeeds (E7); without restart → `JobFailed` after `startup_timeout_s` (shortened in the test),
   - request without the `--request-path` prefix → 404.
2. **`needs_x11` (Xvfb).** Warning: Xvfb does not include Mutter, so these tests **do not** detect GNOME conflicts. The 14.4 checklist covers them.
   - grab `Control_R` + XTest press/release → `PttPressed`/`PttReleased` events (release with `ControlMask` state); `Shift+Control_R` with Shift released before Ctrl → only `ContinuousToggle`; autorepeat (XTest press-release-press with the same timestamp on `F9`) → no false release,
   - `BadAccess`: a second client grabs the same key → `hotkeys: degraded`,
   - `ClipboardOwner`: another client sets text + `text/html` → save → take ownership → receiving client (test window that sends `ConvertSelection` on `Ctrl+V`) receives `UTF8_STRING` with Polish characters → restore all targets byte-for-byte (including type and format); target > 256 KiB or INCR response → `type` backend,
   - a “clipboard manager” (third client fetching content immediately after the owner changes) **does not** confirm the paste,
   - injected `Control_L` does not trigger the PTT grab,
   - no receiver → `left_in_clipboard=True`,
   - modifier wait: XTest holds `Shift_L` → injection waits until it is released,
   - `cancel` while waiting for PTT, modifiers, or clipboard saving → no paste after release; cancellation after taking clipboard ownership but before the shortcut → restore previous content without overwriting a new owner,
   - controlled cancel/XTest-start race: either no shortcut, or `injection_in_flight=true` and exactly one started operation completes with modifiers correctly released; no subsequent segment starts,
   - `type` backend: cancellation between chunks does not start the next subprocess or clipboard fallback; the current chunk may finish.
3. **`e2e`.** Daemon with `FileAudioSource` (plays WAV in real time instead of using a microphone), Xvfb, the receiving window above, and a real server:
   - IPC `ptt start` → 3 s → `ptt stop` → within 30 s the window receives text containing the expected keywords,
   - continuous mode with a recording of three sentences and pauses → three insertions in order,
   - `cancel` before insertion begins (including while waiting for the injector) → no insertions; a started operation is reported according to 08 §8.3, and the next one does not start.
4. **No network.** `unshare -rn sh -c 'ip link set lo up && XDG_RUNTIME_DIR=$(mktemp -d) pytest -m e2e'`. In the new namespace, `lo` is disabled by default and `/run/user/1000` belongs to an unmapped uid, hence both setup steps. The E2E fixture starts `whisper-server` and Xvfb **inside** this namespace. The test must pass, confirming F1/N5.

## 14.4 Acceptance checklist (manual, on the reference machine, before every release)

v0.1:

- [ ] Fresh `install.sh` → `doctor` reports no FAIL.
- [ ] After logging out and back in, both services run; `status` = IDLE within 60 s.
- [ ] PTT in gedit/GNOME Text Editor, Firefox (text field), Chrome/Electron (for example, VS Code), GNOME Terminal (Ctrl+Shift+V), and LibreOffice Writer—Polish characters are correct.
- [ ] PTT with text in the clipboard → after pasting, the clipboard contains the old text.
- [ ] PTT with a screenshot in the clipboard (image) → the `type` backend is used and the image remains in the clipboard.
- [ ] PTT on the unfocused desktop → “text left in clipboard” notification.
- [ ] Tap right Ctrl (< 300 ms press→release) → no transcription or stop sound; a start sound is acceptable if the first frame arrived.
- [ ] PTT + Esc → current recording cancelled; earlier jobs remain. `local-stt cancel` during a new PTT also cancels earlier pending jobs.
- [ ] PTT without speaking (5 s silence) → nothing is inserted (no “Amara.org”); the `cancel` sound plays (the speaker's start sound does not pass the gate).
- [ ] Clipboard-history manager enabled (for example, the GNOME Clipboard Indicator extension) → dictated text is pasted, not old content.
- [ ] Browser copy (text + HTML) in clipboard → after PTT, pasting in LibreOffice preserves formatting.
- [ ] `Super+Space` still switches layouts; the Super overlay works normally.
- [ ] `systemctl --user stop local-stt-whisper` → PTT produces an error sound + notification; `start` → returns to READY.
- [ ] `kill -9` the daemon → restart within 5 s (N9).
- [ ] Change `stt.model` in the config + `local-stt reload` → server restarts itself; `status` goes STARTING → IDLE with the new model (N6).
- [ ] Copy files in Nautilus → after PTT they can still be pasted in Nautilus.
- [ ] `journalctl --user -u local-stt` contains no dictated text.

v0.2 (additional):

- [ ] Continuous: dictate an article for five minutes → complete, ordered text with no duplicate segments.
- [ ] Continuous with a fan running / background typing → no false noise segments (or isolated ones that are filtered out).
- [ ] Disconnect the USB microphone during continuous mode → PipeWire moves the stream to the built-in microphone (INFO log), dictation continues; run `systemctl --user restart pipewire` during it → up to three reopen attempts, then on failure a notification and mode shutdown while the daemon remains running.
- [ ] `local-stt cancel` during operation → no new insertion operations; an operation already started is reported through `injection_in_flight` and may finish.
- [ ] System-muted microphone → “muted” notification.

v0.3 (additional):

- [ ] Preview requires `continuous.preview=true` and `status --watch --preview`; ordinary status, `job` events, logs, and notifications contain no preview text. After the final subscriber disconnects, no further preview requests are submitted.
