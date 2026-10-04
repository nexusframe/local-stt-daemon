# v0.2 acceptance

Results of the v0.2 acceptance from [15](15-implementation-plan.md): the [14.4](14-tests.md) v0.2 checklist, a quick v0.1 regression, the soak test (N3), the incorrect-segmentation report, N4, and the final model defaults. Each item records the date, how it was checked, and the result. Status: **checklist complete** (2026-10-04). All five v0.2 items pass; the USB half of item 3 is N/A (no USB microphone). Two defects were found and fixed on the way: a dead audio stream after a PipeWire restart, and an inaudible `cancel` sound. The incorrect-segmentation report stays open (user decision 2026-10-04), and the soak test passes only on AC with the `performance` profile (see [Plan criteria](#plan-criteria)).

## Environment

- Reference machine: Ubuntu 24.04, GNOME on X11 (`DISPLAY=:1`), AC power, governor `powersave`, platform profile `performance`.
- Code: commit `11024d5`, reinstalled with `install.sh` at 19:17 because the installed copy lacked tasks 2.8 and 2.9 (`doctor` 0 FAIL, 17 OK). From 21:01 with the watchdog fix, from 21:36 also with the sound-queue fix; each time reinstalled (`doctor` 0 FAIL). With both fixes: 728 unit tests and the E2E + integration suites (`e2e`, `needs_x11`, `needs_whisper`, `needs_audio`; 67 tests) pass.
- Config: the defaults (`small-q8_0`, `threads = 4`, `audio_ctx = 1000`, `notifications = "errors"`). During the session `logging.level = "DEBUG"` (live reload, `log_text = false`); restored to INFO afterwards (`config.toml` again equal to `config.example.toml`).
- Microphone: built-in only (`alsa_input.pci-0000_00_1f.3.analog-stereo`); no USB or Bluetooth microphone available.

## 14.4 checklist (v0.2)

| # | Item | Result | Notes |
|---|---|---|---|
| 1 | Continuous: five minutes of an article → complete, ordered text, no duplicate segments | pass | 2026-10-04 19:32–19:40, GNOME Text Editor: the user read the whole `long_pl.txt` (8 min). 51 segments (37 `silence`, 14 `max_length`), all `injected`, `seq` 1…51 without gaps. 774 words inserted vs 771 in the source, from the first to the last sentence; the word alignment contains only single-word insertions and deletions, no missing passage, and no duplicated phrase (the repeated n-grams are recognition errors, e.g. “Irène Joliot-Curie i Ève Curie” → “Irena żyli od Kuri i i w Kuri”). WER 15.8 % (recognition only). RTF 0.33, queue max 3.1 s, `total` p90 4.9 s. |
| 2 | Fan / background typing → no false segments (or isolated, filtered) | pass | 2026-10-04 20:40:34–20:42:51, GNOME Text Editor focused: air purifier on, then typing, nobody speaking. VAD did not start a single segment in 2 min 17 s, so nothing was transcribed or inserted. |
| 3 | USB microphone unplugged → rerouted; `systemctl --user restart pipewire` → up to three reopen attempts, on failure a notification and mode shutdown, daemon keeps running | pass (after fix); USB part N/A | First run 20:46 **FAIL**: the daemon hung and was killed by the kernel OOM killer at 11.4 GB, see [finding](#finding-a-pipewire-restart-leaves-a-dead-stream). After the fix, three runs: (a) 21:02 restart without speech → stall detected after 2.0 s, reconnected at attempt 1, 90 s LISTENING with stable RSS (~105 MB); (b) 21:04 PipeWire stopped for 10 s → three failed reopen attempts at 1 s intervals, `Microphone lost — dictation stopped` notification (the user saw it in the notification tray), mode off, same daemon PID; (c) 21:15 restart while the user dictated → stall after 2.3 s, the speech before the restart was flushed (`cut=flush`) and inserted, reconnected at attempt 1, dictation continued. USB rerouting not checked (no USB microphone; user decision 2026-10-04). |
| 4 | `local-stt cancel` during operation → no new insertions; a started operation reported through `injection_in_flight` and may finish | pass | 2026-10-04 21:15–21:17, driven by a script during continuous dictation: (B) `cancel` while a segment was in STT → `{"injection_in_flight": false}`, `job 11 discarded: cancelled`, nothing inserted, mode IDLE; (C) `cancel` 13 ms after the paste of job 12 started (triggered by the daemon taking CLIPBOARD, XFixes) → `{"injection_in_flight": true}`, that paste completed, nothing after it, mode IDLE. The user heard the `cancel` sound faintly in B and not in C, while speaking; three `toggle` + `cancel` cycles in silence gave all six sounds. |
| 5 | System-muted microphone → “muted” notification | pass | 2026-10-04 19:19 and 19:24: source muted with `pactl`, continuous mode on → after 5 s `microphone appears to be muted` logged and the notification sent (D-Bus `Notify` captured, same `replaces_id` as the earlier one); the user saw the banner in the second run (not watching in the first). |

## v0.1 regression

Reduced set (user decision 2026-10-04: items v0.2 could affect).

| v0.1 item | Result | Notes |
|---|---|---|
| 1 `doctor` | pass | 0 FAIL, 0 WARN, 17 OK after each of the three reinstalls |
| 3 PTT in applications, Polish characters | pass | 2026-10-04 21:23–21:24: GNOME Text Editor (2), Firefox (1), GNOME Terminal (1); all `injected` via clipboard, Polish characters correct (user) |
| 4 old clipboard text kept | pass | `SCHOWEK-R` copied before; Ctrl+V after the four dictations pasted it |
| 7 tap right Ctrl | pass | six taps of 93–131 ms → `ptt too short`, no transcription |
| 9 PTT with silence → nothing inserted, `cancel` sound | pass (after fix) | First run: four silent recordings (0.6–18.4 s) → `discarded: no_speech`, nothing inserted, but **no audible `cancel` sound**, see [finding](#finding-the-cancel-sound-is-masked-by-the-stop-sound). After the fix: `cancel` starts 197 ms after `stop`; the user hears it after silent recordings of 1–5 s. |
| 14 `kill -9` → restart within 5 s (N9) | pass | 2026-10-04 21:22: `status` answers 2.70 s after `kill -9` (unit now with `MemoryMax=1G`) |
| 17 journal contains no dictated text | pass | Journal of both units since 19:17 (152 055 lines, most of them the PortAudio flood from the first item 3 run): none of 13 distinctive dictated or copied words (“Skłodowsk”, “Sorbon”, “Polonu”, “Zażółć”, “gęślą”, “SCHOWEK”, …), while the control strings `job=` and `WM_CLASS` occur 68 times each. |

## Plan criteria

| Criterion ([15](15-implementation-plan.md)) | Result | Notes |
|---|---|---|
| 14.4 (v0.2) checklist | pass | above |
| 10-minute battery soak satisfies N3 | pass on AC `performance`; `power-saver` FAIL | [benchmark-results](benchmark-results.md#stage-3--soak-test-continuous-mode-2026-10-04-task-29): no battery on the machine, replaced by the `power-saver` profile (RTF 1.99, backlog stop); run 3 on `performance`, unloaded: RTF 0.33, queue slope −0.115 s/min. Item 1 above repeats it live: RTF 0.33. |
| Incorrect-segmentation report (13 §13.3) | open | the automatic word reference is unusable; a manually verified `long/001.words.json` is needed (user decision 2026-10-04: left open, does not block v0.2) |
| N4 CPU in silence | pass | 2026-10-04 19:20, `pidstat -p <daemon> 1 60` in continuous mode in a silent room, `performance` profile: mean **2.0 %**, max 4.0 % of one core (limit 5 %); no segment started |
| Final model defaults recorded | pass | `small-q8_0`, 4 threads, `audio_ctx = 1000` in `benchmark-results.md` and `config.example.toml` |

N1 (RAM) after the whole session, including three PipeWire restarts: daemon `VmHWM` **110 MB** (limit 150 MB), `whisper-server` 453 MB since its 21:02 restart.

## Finding: a PipeWire restart leaves a dead stream

2026-10-04 20:46, item 3, first run. After `systemctl --user restart pipewire` the continuous stream stopped delivering frames, but PortAudio reported nothing, so the daemon neither flushed nor reconnected. The user's stop at 20:47:37 was logged as a hotkey, but the controller never handled it: `close()` → `stream.stop()` blocked the controller thread. Some time later PortAudio's ALSA xrun recovery started looping (`alsa_snd_pcm_prepare … failed`; journald kept 150 000 lines and suppressed about 7 million). The daemon grew to 11.4 GB and was killed by the kernel's **global** OOM killer at 20:49:27; systemd restarted it.

Reproduced outside the daemon with a bare `sounddevice` stream opened like `capture.py`: callbacks stop at the restart, `active` stays `True`, no `finished_callback`; after ~70 s the xrun loop starts (337 000 stderr lines and RSS 35 → 416 MB in 5 s, callback never invoked, so the leak is native, not the frame queue). `stop()` on the dead stream blocked for more than 40 s, while `abort()` returned at once and called `finished_callback`. Fixed (user decisions 2026-10-04): a frame watchdog in `AudioCapture` reports a stream without frames for 2 s as lost, which triggers the existing reconnect; `close()` aborts a stream whose last frame is older than 0.5 s and stops a live one (`stop()` keeps ~30 ms more audio at the end of a PTT recording); `MemoryMax=1G` in `local-stt.service` as a backstop. Details in [05](05-audio-and-vad.md) §5.6 and [11](11-daemon-systemd-installation.md) §11.5.

## Finding: the cancel sound is masked by the stop sound

2026-10-04 21:25, v0.1 regression item 9. After a PTT without speech the user heard `start` and `stop` but no `cancel`. The VAD rejects the recording ~70 ms after the release, so `cancel` (120 ms, 440 Hz) started while `stop` (130 ms) was still playing; both `pw-play` processes were started within the same second. Checked by ear with the daemon's own WAV files: `stop` alone, `stop` + `cancel` 70 ms later, `stop` + `cancel` 200 ms later; only the last made `cancel` clearly audible. The same overlap applied to `stop` + `error` (backlog limit, microphone lost). Fixed in `feedback.py`: a sound requested while another is playing starts 70 ms after it ends, and `play()` returns the time until the queued sound ends so the start-sound masking window stays correct ([10](10-cli-ipc-status.md) §10.6). Verified: `cancel` started 197 ms after `stop`, and the user hears it.

## Notes

- **Other clients read every dictated text from the clipboard.** In item 1, TeamViewer fetched `UTF8_STRING` for each of the 51 pastes (gnome-shell three times). With a TeamViewer session that synchronises the clipboard, dictated text would reach the remote side. This is inherent in the X11 clipboard backend (any client may read the selection while the daemon owns it), not a defect of the daemon.
- `cancel` in continuous mode logs only `ipc: cancel`, not `continuous dictation stopped` like every other way of ending the mode (user decision 2026-10-04: not now).
- The `cancel` sound is faint while the user is speaking (`sound_volume = 0.4`).
- 27 % of the item 1 cuts were `max_length` (13.5 % in the soak test): when reading fluently, pauses are often shorter than `min_silence_ms = 700`.
- Finished `pw-play` processes remain zombies until the next `play()` reaps them (harmless).
- A failed microphone reopen prints four PortAudio `Expression … failed` lines to stderr each time (silencing ALSA stderr is not implemented, `capture.py`).
- Item 4 needed a trigger faster than the journal: `journalctl -f` delivered lines ~0.5 s late, longer than the ~190 ms paste window, so the first attempt only cancelled the recording.
