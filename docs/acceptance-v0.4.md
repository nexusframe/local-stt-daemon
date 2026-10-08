# v0.4 acceptance

Results of the v0.4 acceptance from [15](15-implementation-plan.md): the [14.4](14-tests.md) v0.4 checklist and the four plan criteria. Each item records the date, how it was checked, and the result. Status: **checklist complete** (2026-10-08). All seven items and all four criteria pass. Three items have a note. Three findings were decided (see [Findings](#findings)).

## Environment

- Reference machine: Ubuntu 24.04, GNOME on X11 (`DISPLAY=:1`), AC power, governor `powersave`, platform profile `performance` (except for the `power-saver` soak).
- Code: commit `166835c`. It was installed with `install.sh --no-apt` into an empty `models/` directory at 03:40 (item 1). Tests: 872 unit tests, and 72 tests of the E2E and integration suites (`e2e`, `needs_x11`, `needs_whisper`, `needs_parakeet`, `needs_audio`) pass.
- Config: the user's `config.toml`. It has the defaults (`engine = "parakeet"`, `threads = 4`; for whisper-server `small-q8_0`, `audio_ctx = 1000`), except `stt.languages = ["pl", "en"]`, an explicit `language_toggle = "Ctrl+Control_R"`, and the user's own `hallucination_patterns`.
- Microphone: built-in only (`alsa_input.pci-0000_00_1f.3.analog-stereo`).
- The soak runs (03:48–04:19) had external cooling and no browser open (the user closed Brave before the runs). The dictations (15:52–16:47) had the user's usual desktop.

## 14.4 checklist (v0.4)

| # | Item | Result | Notes |
|---|---|---|---|
| 1 | A fresh `install.sh` (empty `models/`) downloads the Parakeet model, `small-q8_0` and Silero VAD; `doctor` reports no FAIL under Parakeet | pass, with a note | 2026-10-08 03:40–03:43. `models/` was moved aside. The three models downloaded in 2 min 47 s with "checksum OK", and they are byte-identical to the previous copies. The other Whisper models were moved back. The `doctor` step of `install.sh` reported **1 FAIL**: `local-stt-engine activating`. A `doctor` run 30 s later reported 0 FAIL, 17 OK. See [finding 1](#1-doctor-in-installsh-runs-before-the-engine-is-ready). |
| 2 | PTT and 2 minutes of continuous dictation under Parakeet in the applications of the v0.1 list → correct Polish text; `total` far below the v0.1 Whisper p90 (3.70 s) | pass | 2026-10-08 15:52–16:41, by the user, in GNOME Text Editor, Firefox, VS Code, GNOME Terminal and ONLYOFFICE. The user reports correct Polish text in all five applications. A continuous session of 3 min 11 s (16:37:08–16:40:19) gave 22 segments (`seq` 1…22 without gaps, 20 `silence`, 2 `max_length`), all `injected`, with a pause of ~16 s. `total`: PTT p90 0.99 s (N2 below), continuous 0.48–1.13 s. No WARNING or ERROR in the journal, except the non-Latin warnings of item 6. |
| 3 | `stt.engine` "parakeet" → "whisper-server" → "parakeet" with `local-stt reload` → the other engine unit stops, the selected one reaches READY; dictation works; `status` shows the engine and its model | pass | 2026-10-08 03:44: READY after 1.2 s (whisper-server) and 3.5 s (Parakeet), `transcribe` of the `pl_short.wav` fixture correct both times. 2026-10-08 16:40–16:47 with dictation by the user: whisper-server READY, `local-stt-engine` inactive, PTT job 83 (15.1 s) `total` 2.77 s; Parakeet READY after 3 s, `local-stt-whisper` inactive, PTT job 85 (16.0 s) `total` 1.55 s. `status` showed `whisper.cpp small-q8_0` and `parakeet parakeet-tdt-0.6b-v3-int8`. The config file was restored and is identical to its backup. |
| 4 | Under Parakeet, the language hotkey and `local-stt language toggle` are rejected (notification "Language: automatic (Parakeet)", exit code 4) | pass | `language toggle` and `language en` → "rejected: Parakeet detects the language itself; stt.languages is for whisper-server", exit code 4; `language` → `auto (languages: pl, en)`. The user pressed `Ctrl+Control_R` and saw the notification. |
| 5 | Humming, a cough and keyboard noise during continuous dictation insert nothing | pass | By the user during a continuous session (15:55–15:57 or 16:04): nothing was inserted. `stats.jobs_filtered` counted 3 filtered jobs in the session. The journal does not show which segment each one was (only on DEBUG). |
| 6 | A code-switched sentence that comes out in Cyrillic is inserted unchanged and counted in `stats.jobs_non_latin` | pass, with a note | 7 jobs had non-Latin letters. Each one has the warning "non-Latin letters in the output (parakeet), injected unchanged" without the text, and `jobs_non_latin` = 7. The user reports that Polish speech, including the code-switched sentence, did not come out in Cyrillic this time. Cyrillic came from Russian speech and from one Polish sentence with an imitated Russian accent. Thus the mechanism passes, but this session did not reproduce the code-switching case of task 4.4. |
| 7 | Engine server RSS stays ≤ 2.2 GB after 10 minutes of dictation with one 120 s PTT recording (N1); `ss -ltnp` shows it only on `127.0.0.1` (N5) | pass, with a note | After 85 jobs, with one PTT recording of 106.2 s (`total` 11.94 s, 1006 characters, complete text): `VmHWM` 2214 MB, `VmRSS` 1.27–1.32 GB, no swap. The limit was 2253 MB (2.2 × 1024), so the margin was 1.7 %. N1 is now 2.4 GB, see [finding 2](#2-the-n1-margin-is-small). `ss -ltnp`: only `127.0.0.1:8178` (`local-stt`). |

## Plan criteria

| Criterion ([15](15-implementation-plan.md)) | Result | Notes |
|---|---|---|
| Corpus A WER with the default engine ≤ 7.4 % | pass | 5.6 % (task 4.7, run `2026-10-07T22-56-03Z`, [benchmark-results](benchmark-results.md)). |
| N2: p90 `total` from ≥ 20 PTT dictations of 4–10 s ≤ 2.0 s | pass | 21 dictations of 4–10 s (two rounds, 15:52–16:37), none overlapping (`queued` 0.00 s): p50 0.82 s, **p90 0.99 s**, maximum 1.12 s; `inject` p50 195 ms, maximum 225 ms. v0.1 with Whisper: 3.70 s. All 49 Parakeet PTT jobs except the 106 s recording (0.85–20.35 s of audio) had a `total` ≤ 1.87 s. |
| N3: soak RTF ≤ 0.5 on AC `performance`, and a `power-saver` run recorded | pass | Table below. |
| `stt.engine = "whisper-server"` still passes the v0.2 soak | pass | Table below. |

## Soak runs (N3 and the whisper-server regression)

`local-stt bench --soak` (task 4.9), 10 min each, corpus A `long/001.wav` (497 s) in a loop, `--words` with the unverified whisper-cli reference. The runs started at ≤ 55 °C. Results: `~/.local/share/local-stt/bench/acceptance-v0.4-2026-10-08/`.

| Run | Start | RTF (≤ 0.5) | queue max | slope, final 5 min (≤ 0.05 s/min) | frequency drop (≤ 30 %) | max °C | VAD, N4 (≤ 5 %) | peak RSS | Verdict |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| Parakeet, `performance` | 03:48, 53 °C | **0.09** | 14.6 s | −0.079 | 22 % | 98 | 1.5 % | 1355 MB | **PASS** |
| `small-q8_0` @1000, `performance` | 03:58, 42 °C | 0.32 | 17.0 s | −0.166 | 10 % | 100 | 1.6 % | 532 MB | **PASS** (v0.2: RTF 0.33) |
| Parakeet, `power-saver` | 04:08, 51 °C | **0.31** | 17.2 s | −0.200 | 8 % | 49 | **4.9 %** | 1399 MB | **PASS** (Whisper in v0.2: RTF 1.99, FAIL) |

- With Parakeet, continuous mode passes on `power-saver` too, the stand-in for the missing battery (user decision 2026-10-04). With Whisper it failed. Thus N3 holds on both profiles for the default engine.
- On `power-saver` the VAD share is 4.9 %, which is near the N4 limit. See [finding 3](#3-vad-share-on-power-saver-is-near-n4).
- The segmentation is the same in the three runs: 74 segments (63 `silence`, 10 `max_length`, 1 `flush`). Parakeet filtered 1 of 74 segments in both runs, and Whisper filtered none. The soak result does not record texts, so the filtered segment is not known. It is possibly the last segment, which the stop at 600 s cuts. This is not tested.
- "33/73 incorrect cuts" is the artefact of the unverified reference ([benchmark-results](benchmark-results.md), stage 3).

## Findings

### 1. `doctor` in `install.sh` runs before the engine is ready

Step 9 of `install.sh` runs `doctor` right after it restarts the daemon. The daemon starts `local-stt-engine` in a helper thread and does not wait for it (11 §11.5). After a fresh download the model load took 2.6 s, so `doctor` saw the unit as `activating` and reported FAIL. Thus a correct fresh install ends with "doctor reported FAIL". In task 4.5 the engine was already running, so the check passed. **Fixed** (user decision 2026-10-08): step 9 waits at most 30 s for an engine unit to become `active` or `failed` (11 §11.3); tests `test_install_waits_for_the_engine_before_doctor`, `test_install_engine_wait_gives_up_with_a_warning`, `test_install_engine_wait_skips_without_the_daemon`.

### 2. The N1 margin is small

The peak RSS after the 106 s recording was 2214 MB, after 84 earlier jobs. In task 4.8 the peak was 2184 MB for 120 s. Thus the peak depends on the request history, and the margin to the N1 limit (2253 MB) is 1.7 %. `MemoryMax=3000M` has more margin, so this is a limit question, not a risk to the service. **Changed** (user decision 2026-10-08): N1 for Parakeet is 2.4 GB (2458 MB, ~10 % above the measured peaks) in 01, 06, 11, 13 §13.5, 14 and `bench report`.

### 3. VAD share on `power-saver` is near N4

The `audio-consumer` thread used 4.9 % of a core on `power-saver` (1.5 % on `performance`). With Whisper in v0.2 it was 5.6 % (FAIL). The RMS pre-filter of 05 §5.4 is not implemented. **No change** (user decision 2026-10-08): the run passes, and `power-saver` is only the stand-in for the missing battery. The pre-filter stays unimplemented.
