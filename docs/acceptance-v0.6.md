# v0.6 acceptance

Results of the v0.6 acceptance from [15](15-implementation-plan.md) (criteria K1–K7, accepted by the user 2026-10-10). Each item records the date, the method and the result. Status: **accepted** (2026-10-10). K1, K2 and K4–K7 pass. K3 fails by 25 ms, and the user accepted this deviation. Echo (task 6.5) has no threshold, and its result is recorded below.

## Environment

- Reference machine: Ubuntu 24.04, GNOME on X11, Intel i5-8365U, AC power. Built-in microphone and built-in speakers.
- Code: the v0.6 code of tasks 6.1–6.4 (`703bb68`) plus `bench --soak --conversation` and the conversation E2E test (this acceptance). Installed with `install.sh --no-apt` on 2026-10-10, before the live checks.
- Engine: Parakeet `parakeet-tdt-0.6b-v3-int8`, 4 threads. Config: the user's `config.toml`, with `[conversation] speculative_ms = 250` (default).
- Data: `~/.local/share/local-stt/bench/conversation-2026-10-10/` (K1–K4), `~/.local/share/local-stt/bench/acceptance-v0.6-2026-10-10/` (K5, K6 live), `~/.local/share/local-stt/bench/echo-2026-10-10/` (echo).

## Criteria

| # | Criterion | Threshold | Result | Notes |
|---|---|---|---|---|
| K1 | Barge-in: start of speech → `speech_start` at the client | p90 ≤ 400 ms | **pass**: p90 257 ms | 2026-10-10, task 6.1. Corpus A, 40 files through `FileAudioSource`, 42 events: p50, p90 and maximum 257 ms. Live check through the real socket: 288–320 ms. |
| K2 | Speculative text: `t_ready − t_end`, `final: false`, utterances of 2–10 s | p90 ≤ 0.9 s | **pass**: p90 0.68 s | 2026-10-10, task 6.4, third run (load ≤ 1.0), 7 texts. |
| K3 | Final text: `t_ready − t_end`, `final: true`, no retraction | p90 ≤ 0.8 s | **fail, accepted**: p90 0.825 s | 2026-10-10, task 6.4, third run, 20 utterances, p50 0.76 s. Without speculation p90 1.29 s. See [deviation 1](#1-k3-fails-by-25-ms). |
| K4 | Corpus A WER, conversation mode against dictation | at most +0.5 pp | **pass**: +0.31 pp | 2026-10-10, task 6.4: 5.41 % against 5.10 %. |
| K5 | CPU cost: 10 min soak in conversation mode, all engine jobs | RTF ≤ 0.5; record the retractions | **pass**: 0.16 per second of speech | 2026-10-10 14:44 UTC, `bench --soak --conversation`, services stopped, AC, governor `powersave`. 153 engine jobs for 63 final transcripts, 135 speculative cuts, 81 retracted. See [K5 details](#k5-details). |
| K6 | Isolation: no text to the window, the clipboard, the history or a notification; dictation again within 1 s after the disconnect; no text without `"transcripts": true` | all pass | **pass** | Xvfb: `tests/e2e/test_conversation_e2e.py` (new). Live check 2026-10-10 17:02. See [K6 details](#k6-details). |
| K7 | Regression and privacy: PTT, continuous mode, `doctor`, the full test suite; no dictated text in the journal | all pass | **pass** | See [K7 details](#k7-details). |

### K5 details

`bench --soak --conversation` (new in this acceptance, [13](13-benchmark.md) §13.4 stage 3) plays `long/001.wav` in a loop for 600 s through the daemon chain in conversation mode. The verdict uses the engine time of all jobs divided by the seconds of speech in the final segments (user decision 2026-10-10). The dictation formula gives RTF 0.08, because each speculative job adds its own audio to the denominator. The v0.4 dictation soak of the same file had RTF 0.087. Thus speculation costs about twice the engine time of dictation. This is far below the N3 limit of 0.5.

Other values: queue tail slope −0.086 s/min (pass), CPU frequency drop 16 % (pass), VAD 1.7 % of a core (N4 pass), server 0.80 cores, peak RSS 1402 MB, maximum temperature 100 °C. Segments: 63 `silence`, 10 `max_length`, 1 `flush`. One job was `filtered`. See [observation 2](#2-the-soak-queue-maximum-is-higher-in-conversation-mode).

### K6 details

Xvfb test (2026-10-10, passes in 27 s): three FLEURS sentences in conversation mode give three `final: true` transcripts with the correct words to the conversation client. All `job` events have `result = sent`. The receiving window gets no text and no keys. Nobody owns the CLIPBOARD selection. `history` is empty. A second subscriber without `"transcripts"` gets the speech events but no text. After the client disconnects, the daemon is IDLE within 1 s, and a new continuous session inserts the text into the window. The test log contains no recognized word.

Live check (2026-10-10 17:02, the user, GNOME Text Editor focused): a conversation client through the real socket for 30 s. The user said two sentences. The client got two speculative and two final transcripts with the same text. Jobs 1–2 had `result=sent`, inject 0 ms. Nothing appeared in the editor (user report). `local-stt history` did not contain the two texts. The daemon was IDLE 474 ms after the disconnect.

### K7 details

| Item | Result | Notes |
|---|---|---|
| `install.sh --no-apt` → `doctor` | pass | 2026-10-10: 0 FAIL, 0 WARN, 17 OK. |
| ruff, mypy, full test suite | pass | ruff check and format are clean, mypy is clean (43 files), pytest: 1081 passed in 2 min 46 s. |
| PTT after the conversation | pass | Job 3, `total` 0.57 s, `injected` (user report: the text appeared). |
| Continuous mode after the conversation | pass | Jobs 4–5, two `silence` segments, `total` 0.57 s and 0.67 s, `injected` (user report). |
| No dictated text in the journal | pass | `journalctl --user -u local-stt -u local-stt-engine` since 15:30: 0 matches for the words of the history texts and the conversation texts. |

## Echo (task 6.5)

No threshold (user decision 2026-10-10). Measured 2026-10-10, details in [15](15-implementation-plan.md) task 6.5 status. Built-in speakers, 180 s of local-tts speech, nobody spoke:

| Run | False `speech_start` per minute |
|---|---|
| without echo cancellation, 44 % and 100 % | 7.7 and 7.3 |
| `module-echo-cancel` (webrtc), 44 % and 100 % | 0 and 0 |

With echo cancellation, barge-in works, but speech that fully overlaps the TTS gives wrong text. User decision 2026-10-10: headphones stay the default assumption. On speakers, the orchestrator loads `module-echo-cancel`.

## Deviations and observations

### 1. K3 fails by 25 ms

K3 p90 is 0.825 s against the threshold of 0.8 s. The time is about 256 ms of silence (`speculative_ms` plus one frame) plus the engine time. The difference is smaller than the run-to-run spread of this laptop (±20 %). Without speculation, p90 is 1.29 s. **Accepted** (user decision 2026-10-10). `speculative_ms` stays 250.

### 2. The soak queue maximum is higher in conversation mode

The queue maximum was 28.7 s in conversation mode against 14.6 s in the v0.4 dictation soak of the same file. Probable cause: a queued speculative job and the final job of the same utterance both count their audio in `queued_audio_s`. Thus the value counts some audio twice. This is not verified. The tail slope is negative, so the queue does not grow. **No change**. The criteria of v0.6 do not include the queue maximum.
