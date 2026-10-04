# v0.1 acceptance

Results of the v0.1 acceptance from [15](15-implementation-plan.md): the [14.4](14-tests.md) checklist and the nonfunctional requirements N1, N2, N5, N8 and N9 from [01](01-scope-and-requirements.md). Each item records the date, how it was checked and the result. Status: **in progress**.

## Environment

- Reference machine: Ubuntu 24.04, GNOME on X11 (`DISPLAY=:1`), governor `powersave`, AC power.
- Code: commit `c7b5471` (task 1.15); from 2026-10-04 00:08 with the VAD gate (tasks 2.1 + 2.6), from 00:59 also with the clipboard restore fix, from 01:50 with the paste-confirmation fix; each time reinstalled with `install.sh` (`doctor` 0 FAIL). Installed 2026-10-03 23:05 with `scripts/uninstall.sh` (without `--purge`: models, the whisper.cpp v1.9.4 build and `~/.config/local-stt` kept) followed by `scripts/install.sh`.
- Config: the defaults (`config.toml` equal to `config.example.toml`): `small-q8_0`, `threads = 4`, `audio_ctx = 1000`.
- Not installed on this machine: LibreOffice, a clipboard-history manager (user decision 2026-10-03: no installs). ONLYOFFICE Desktop Editors (deb, `/opt/onlyoffice`) replaces LibreOffice Writer in items 3 and 11; its editor runs in embedded Chromium (CEF), so it does not cover LibreOffice's own (VCL) clipboard handling.
- During the manual items and N2, `logging.level = "DEBUG"` (live reload; `log_text = false`), so the log records each paste target's WM_CLASS; restored to INFO on 2026-10-04 after N2 (`config.toml` again equal to `config.example.toml`).

## Nonfunctional requirements

| Req. | Result | Method and values |
|---|---|---|
| N1 RAM | pass | 2026-10-03 idle after install: daemon 49 MB, `whisper-server` 359 MB. 2026-10-04 peak after all tests and N2 (`VmHWM` from `/proc/<MainPID>/status`): daemon **108 MB** (limit 150 MB; the increase is mostly onnxruntime + Silero for the VAD gate), `whisper-server` **582 MB** (limit 1 GB; process running since the item 13 restart, so it covers N2). |
| N2 PTT latency | pass | 2026-10-04: p90 `total` **3.70 s** (limit 5 s) over 20 dictations of 4–10 s; see [N2](#n2-ptt-latency). |
| N5 no network | pass | 2026-10-03: `ss -ltnp` — `whisper-server` listens only on `127.0.0.1:8178`; the daemon has only `/run/user/1000/local-stt/control.sock` and no TCP sockets; `doctor` port check OK (loopback only). The E2E suite passes in a network namespace without network (`unshare -rn`, 14 §14.3 item 4, task 1.15). |
| N8 config | pass | 2026-10-03: every setting comes from `config.toml`. Environment variables read by the daemon only select paths (`XDG_CONFIG_HOME`, `XDG_RUNTIME_DIR`, `LOCAL_STT_CONFIG`) or the session (`DISPLAY`, `JOURNAL_STREAM`, `NOTIFY_SOCKET`); `secret` and `whisper-server.env` are generated, not user configuration. |
| N9 restart | pass | 2026-10-03: `kill -9` of the daemon's MainPID → new process `active` (after `READY=1`) in 2.39 s, IPC `status` answers in 2.63 s (limit 5 s); `NRestarts=1`, `RestartSec=2s`. |

## 14.4 checklist (v0.1)

| # | Item | Result | Notes |
|---|---|---|---|
| 1 | Fresh `install.sh` → `doctor` reports no FAIL | pass | 2026-10-03: 0 FAIL, 0 WARN, 17 OK |
| 2 | After logging out and back in, both services run; `status` = IDLE within 60 s | pending | |
| 3 | PTT in GNOME Text Editor, Firefox, VS Code, GNOME Terminal, LibreOffice Writer — Polish characters correct | pass | 2026-10-04 (N2 session): GNOME Text Editor, Firefox, VS Code, GNOME Terminal (`Ctrl+Shift+V`) and ONLYOFFICE instead of Writer; all pasted with Polish characters (ą ć ę ł ń ó ś ź ż). Recognition errors occurred (WER), but no character was mangled by injection. |
| 4 | PTT with text in the clipboard → the old text remains afterwards | pass | 2026-10-04: blocks in Text Editor, VS Code and ONLYOFFICE started with “SCHOWEK-A/C/E” copied; after 4–5 dictations each, Ctrl+V pasted the expected word. |
| 5 | PTT with an image in the clipboard → `type` backend, image remains | pass | 2026-10-04: a 37 KB area screenshot (`image/png`, within limits, no INCR) was saved and restored through the clipboard (`backend=clipboard`, same SHA-256 before and after) — the image remained, but the item's assumption “image → `type`” holds only for images over the limits. A full-screen screenshot: `clipboard cannot be restored (content too large): typing instead`, text typed character by character, the image pasted afterwards in ONLYOFFICE. Item wording corrected in 14.4. |
| 6 | PTT on the unfocused desktop → “text left in clipboard” notification | pass | 2026-10-03: notification shown, log `no active window: text left in the clipboard`; Ctrl+V in Text Editor pasted the text |
| 7 | Tap right Ctrl (< 300 ms) → no transcription or stop sound | pass | 2026-10-03, GNOME Text Editor |
| 8 | PTT + Esc cancels the recording, earlier jobs remain; `local-stt cancel` during a new PTT also cancels earlier pending jobs | pass | 2026-10-04, GNOME Text Editor: (a) dictation, second PTT within a second, Esc while holding → the first sentence inserted, the second not. (b) the same without Esc; a watcher script ran `local-stt cancel` once the second recording overlapped the first job (`PTT_RECORDING`, pipeline busy): the in-flight job 3 → `discarded: cancelled`, the recording cancelled, the release `ignored PttReleased in IDLE`; nothing inserted. |
| 9 | PTT with 5 s of silence → nothing inserted, `cancel` sound | pass (after fix) | 2026-10-03, first run **FAIL**: Whisper hallucinations inserted (“To jest to.”, “To jest zimno.”, …); no recording was rejected, see [RMS gate](#finding-the-rms-gate-cannot-reject-silence). Fixed by VAD for PTT (tasks 2.1 + 2.6 brought forward). 2026-10-04 retest, GNOME Text Editor: 13 silent recordings with and without the air purifier → all `discarded: no_speech`, `cancel` sound, nothing inserted; a normal sentence and “test jeden dwa trzy” between 3 s and 2 s of silence were inserted complete (no clipped words). Once Whisper appended a number (“Test 1, 2, 3, 4.”, job 9, trimmed audio 2.71 s); not reproducible, presumably the decoder continuing the sequence (hypothesis, audio is not kept). |
| 10 | Clipboard-history manager enabled → dictated text is pasted, not old content | N/A | no manager installed and none used (user decision 2026-10-03); covered only by the `needs_x11` test with a simulated manager on Xvfb |
| 11 | Browser copy (text + HTML) → after PTT, pasting in LibreOffice keeps formatting | pass | 2026-10-04: Firefox selection (10 targets incl. `text/html`, `text/_moz_htmlcontext`, `text/x-moz-url-priv`) → PTT in Text Editor → every target restored with the same type, format, length and SHA-256 (clipboard logger on `:1`, hashes only); pasted into ONLYOFFICE with formatting. |
| 12 | `Super+Space` still switches layouts; the Super overlay works | pass (partial) | 2026-10-04: tapping Super opens the overview. Layout switching cannot be observed: the machine has a single keyboard layout (the daemon grabs only `Control_R` and, while PTT is held, `Escape`) |
| 13 | `systemctl --user stop local-stt-whisper` → PTT gives an error sound + notification; `start` → READY | pass | 2026-10-04: engine DOWN logged 9 s after the stop; PTT → error sound and notification; after `start`, READY in 0.78 s. |
| 14 | `kill -9` the daemon → restart within 5 s (N9) | pass | see N9 |
| 15 | Change `stt.model` + `local-stt reload` → server restarts; `status` STARTING → IDLE with the new model (N6) | pass | 2026-10-03: `small-q8_0` → `small-q5_1`: STARTING at 0.43 s, READY at 1.00 s; the server process loaded `ggml-small-q5_1.bin`. Config restored and reloaded (back on `small-q8_0`). |
| 16 | Copy files in Nautilus → after PTT they can still be pasted in Nautilus | pass (after fix) | 2026-10-04 first run **FAIL**: files not pasted. See [silent restore](#finding-a-silent-clipboard-restore-hides-copied-files-from-nautilus). Fixed in `inject/clipboard.py` + 08 §8.5 step 8; retest: files copied after PTT. |
| 17 | `journalctl --user -u local-stt` contains no dictated text | pass | 2026-10-04: the journal of both units since the install (4639 lines, DEBUG level most of the time) contains none of 12 distinctive dictated or copied words (“łódź”, “sprawozdanie”, “Gdańsk”, “pszczoły”, “SCHOWEK”, “To jest zimno”, …), while the control strings `job=` (126) and `WM_CLASS` (129) are found. |

## N2 PTT latency

2026-10-04 13:12–13:32, 24 prepared sentences (Polish characters in each) in five blocks: GNOME Text Editor, Firefox (text field), VS Code (file editor), GNOME Terminal (at the prompt, no Enter), ONLYOFFICE; text in the clipboard before the Text Editor, VS Code and ONLYOFFICE blocks. Source: the `local_stt.timings` lines; `audio` is the VAD-trimmed duration.

- 28 jobs, 27 with audio 4–10 s (one 2.49 s excluded). All 27: `result=injected`, backend `clipboard`, 0 failed or unconfirmed pastes.
- 7 of the 27 overlapped: the next PTT was pressed before the text was inserted, so the injector waited for its release (08 §8.5 step 1) — `inject` 4.2–8.1 s (jobs 19, 21, 22 in Firefox; 24, 25 in VS Code; 36, 37 in ONLYOFFICE; e.g. job 19: text ready ≈ 13:21:43, PTT held 13:21:42.6–49.5, pasted 17 ms after release). Excluded from N2 under the 13 §13.5 rule added for this (user decision 2026-10-04).

| Set | n | `total` p50 | `total` p90 | `total` max | `stt` p90 | `inject` p50 / p90 |
|---|---|---|---|---|---|---|
| non-overlapping (N2) | 20 | 2.91 s | **3.70 s** | 3.86 s | 3.29 s | 194 / 214 ms |
| all, for reference | 27 | 3.26 s | 9.42 s | 12.06 s | 3.34 s | 206 / 6208 ms |

Maximum `total` per application (N2 set): Text Editor 3.55 s (n=5), Firefox 3.70 s (2), VS Code 3.73 s (6), GNOME Terminal 2.84 s (4), ONLYOFFICE 3.86 s (3). Conditions: AC and `powersave` governor (checked right after the session; no power-change event in the system journal), the developer's usual desktop (VS Code, Firefox, Brave, TeamViewer running).

## Findings

### Finding: the RMS gate cannot reject silence

2026-10-03, item 9. The v0.1 gate (05 §5.3) passes a recording when any 100 ms window exceeds `ptt.silence_rms_dbfs` (−50 dBFS). Measured on the reference machine (built-in microphone, PipeWire source volume 100 % with base volume 20 %, i.e. about +42 dB of gain), 5 s recordings, RMS of 100 ms windows:

| Recording | air purifier off | air purifier on |
|---|---|---|
| silence: min / median / max | −42.4 / −40.0 / −36.0 dBFS | −29.5 / −28.7 / −27.7 dBFS |
| one tap of right Ctrl: max | −8.1 dBFS | −7.4 dBFS |

Every window of every recording is above −50 dBFS, so no recording is ever rejected, and Whisper's own `no_speech` + `logprob` filter (06 §6.8) did not catch the hallucinations either. A fixed threshold cannot work: the noise floor moves by 11 dB with one household appliance, and a key click is louder than speech. Fix: Silero VAD for PTT (tasks 2.1 + 2.6) brought forward from v0.2 (user decision 2026-10-03).

### Finding: a silent clipboard restore hides copied files from Nautilus

2026-10-04, item 16. After a dictation, Ctrl+V in Nautilus did nothing although the daemon served the saved `x-special/gnome-copied-files`, `text/uri-list` and text targets byte for byte (read back on `:1`). Ruled out: the `application/vnd.portal.*` transfer tokens. A probe owner serving the snapshot without them (B) and with them (C) both pasted, and Nautilus requested only `x-special/gnome-copied-files`. Cause: 08 §8.5 step 8 restored by swapping the served content while staying the owner, so no XFixes `SetSelectionOwnerNotify` was sent; GTK4 apps cache the targets per owner change, and Nautilus still believed the clipboard held our text. Confirmed on Xvfb: paste + restore produced one notification (our takeover), the restore none; a same-window `SetSelectionOwner` produces one. Fix: announce the restore with `SetSelectionOwner(CLIPBOARD, our_window, owned_since)` (the original timestamp cannot take back a newer clipboard). Tests: `test_restore_announces_the_restored_targets`, `test_restore_announcement_never_takes_back_a_newer_clipboard` (the latter fails if the current server time is used).

### Finding: false “Could not paste” in ONLYOFFICE and Claude Code

2026-10-04. Text was inserted, but the daemon logged `paste not confirmed` (`inject` ≈ 1040 ms = `paste_timeout_ms`), showed “Could not paste — text is in the clipboard” and left the text in the clipboard. A DEBUG line per clipboard request (target, requestor, PID via XRes; never content) showed who read the text after Ctrl+V:

| Application | active window | text read by |
|---|---|---|
| GNOME Text Editor | client 0x5200000, PID 374328 | the same client → confirmed |
| ONLYOFFICE (CEF) | client 0x5a00000, PID 489999 | client 0x5e00000, **the same PID** |
| Claude Code CLI in VS Code's terminal | `code`, PID 136385 | `claude`, PID 196172, a short-lived connection; process chain `code` → node service → `zsh` → `claude` |

The 08 §8.5 step 7 rule (same X client only) was too strict. Fix: a text request after Ctrl+V also confirms when it comes from the active window's process or a descendant (PID from XRes, looked up before the reply while the requestor still waits — the first version looked it up afterwards and lost the race with `claude` closing its connection). Retest: Claude Code (3×), ONLYOFFICE, Text Editor, GNOME Terminal all `result=injected`, `inject` ≈ 190 ms, no notification.

### Finding: TeamViewer reads every dictation from the clipboard

2026-10-04. TeamViewer (PID 5607) requests `UTF8_STRING` within ~5 ms of every clipboard takeover, as does gnome-shell. Neither confirms a paste, but TeamViewer synchronizes the clipboard with a connected remote side, so during a TeamViewer session the dictated text can leave the host (N5). Inherent to the clipboard backend (08 §8.4 already lists clipboard managers); mitigation is the user's: disable TeamViewer's clipboard synchronization or use `backend = "type"`.
