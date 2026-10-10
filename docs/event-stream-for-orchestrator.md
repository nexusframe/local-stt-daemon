# Event stream for the orchestrator: timing report

This report is for the orchestrator `local-assistant`. The orchestrator wants to start the llama-server prompt processing before the user stops speaking. This report examines whether the local-stt event stream supports this, and how long each step takes. Measured 2026-10-10.

The report has four kinds of content. **Fact** is measured or read in the code. **Observation** is seen once or in a small sample. **Assumption** is not measured. **Recommendation** is a proposal. The measurements used the code of `eade7d4`. After the report, the user decided on three problems that the report found. See [Changes after the measurement](#changes-after-the-measurement).

## Protocol

- Specification: [10](10-cli-ipc-status.md) §10.2, the part “Speech events”, “Transcripts”, “Speculative transcripts” and “Conversation mode”.
- Code: `0.6.0`, commit `eade7d4`. The last protocol change is in `703bb68` (task 6.4). The commits after it change only the benchmark, the docs and the version.
- Client example: [`scripts/conversation_client.py`](../scripts/conversation_client.py). It connects, starts conversation mode, prints each event, and disconnects. It uses only the standard library.

```
python3 scripts/conversation_client.py --seconds 60 --out events.jsonl
```

## Measurement conditions

| Item | Corpus run | Live sessions |
|---|---|---|
| Speech | corpus A: 40 recordings of the user, Polish, played in real time | the user, Polish, built-in microphone |
| Path | the daemon chain in one process (Controller, Silero, Pipeline) with a temporary Parakeet server, like `bench --soak --conversation` | the installed daemon and engine, through the real socket |
| Utterances | 42 | 10 (2 sessions) |
| Utterance length | 0.7–22.3 s, median 6.6 s | 1.2–4.0 s, median 2.1 s |
| Event time | `at`: monotonic time of the publish call | `rx`: monotonic time when the client read the line |
| Load (`load1`) | 0.5 before the run, at most 2.8 during it | 0.7–1.1 before, at most 1.7 during |
| Other | AC, platform profile `performance`, daemon services stopped, no browser | the same machine, the same day |

Config: defaults. `vad.min_silence_ms = 700`, `conversation.speculative_ms = 250`, `vad.start_threshold = 0.50`, `vad.end_threshold = 0.35`, `vad.max_segment_s = 15`, Parakeet int8 with 4 threads. The latency of this laptop can change by ±20 % between runs.

Data and scripts (outside the repository): `~/.local/share/local-stt/bench/stream-2026-10-10/`. `corpus_run.py` and `corpus-run1.json` are the corpus run. `live.jsonl` and `live3.jsonl` are the live sessions. `analyze.py` calculates the values, and `live_cpu.py` samples the CPU.

## 1. Partial results during speech

**Fact (code).** local-stt sends no partial results while the user speaks. The engine is an offline model. It transcribes a full segment. The only early text is the speculative `transcript` with `final: false`. The daemon sends it after 250 ms of silence, and only before `speech_end`. No field marks a stable prefix. The whole `final: false` text can change.

**Fact (measured).** Delay of the `final: false` text after the end of speech (`t_end`):

| Data | n | min | median | p90 | max |
|---|---|---|---|---|---|
| corpus | 15 | 502 ms | 548 ms | 588 ms | 596 ms |
| live | 8 | 604 ms | 662 ms | 706 ms | 706 ms |

**Fact (measured).** A `final: false` text arrives only for short utterances. In the corpus, all 13 utterances below 3 s got one. Only 2 of 20 utterances of 5–10 s got one, and no utterance above 10 s got one. In the live sessions, 8 of 8 utterances below 3 s got one, and 0 of 2 utterances of 4.1–4.6 s got one. The cause: the speculative job starts 250 ms after `t_end`. For an utterance of 4 s or more, the engine needs 0.45 s or more. Thus the job ends after `speech_end` (700 ms), and only the `final: true` text comes. It reuses the same job.

**Fact (measured).** A `final: false` text can come only at a pause. Inside a sentence, a pause of 250–700 ms starts a speculative job. In the live sessions, speech came back before the job ended two times (logs `02`, and the second “Włącz muzykę”). The daemon did not send that text, and it sent no `transcript_retracted`.

**Cost of real partial results (estimate from an old simulation, not from this run).** On 2026-10-07, a simulation re-transcribed the growing audio every 0.5 s with the same Parakeet int8 model and 4 threads (corpus A, 40 files; data: `~/.local/share/local-stt/bench/parakeet-2026-10-07-stream/`). A word became stable when two consecutive results agreed (LocalAgreement-2). Results:

- The engine was busy for 75 % of the speech time on average (median 85 %, maximum 105 %). It used 4 threads during this time.
- A stable word arrived 1.7 s after its end (median), and 4.9 s (p90).
- 17 of 533 stable words were wrong, in 12 of 40 utterances. A “stable” prefix thus changed in 30 % of the utterances.
- The decode time grows with the audio length. One decode took 0.9 s (median of the per-file maximum), and up to 2.9 s.

**Assumption.** llama-server uses 4 threads on the same 4 cores. Partial results would compete with it during the whole utterance, and both would become slower. This was not measured.

**Recommendation.** Do not add partial results on this CPU. Their stable text arrives 1.7 s late, and the CPU cost is high. Do not implement this without the user's approval.

## 2. Final text against the last speculative text

**Fact (measured).** Utterances that got at least one `final: false` text:

| Data | n | final identical | final starts with the speculative text | the same, without the end punctuation | final reused the speculative job |
|---|---|---|---|---|---|
| corpus | 15 | 13 | 14 | 15 | 13 |
| live | 8 | 8 | 8 | 8 | 7 |

- When no speech comes after the cut, the final text reuses the speculative job. The text is then identical (20 of 20).
- After a retraction, the final text comes from a new job. In the corpus, two retracted texts were “Rano pojechałem rowerem nad jezioro” and “Ta aplikacja działa szybko.”. The final texts continued them. In the second case, the period became a comma. Thus the prompt cache stays valid only up to the character before the end punctuation.
- In the live retraction (log `05`), the final text was identical to the retracted text.

**Recommendation for the orchestrator.** Put the `final: false` text into the prompt without its end punctuation. Add the punctuation only from the `final: true` text.

## 3. End of speech

`t_end` is the end of the last frame with `p ≥ end_threshold`. It is not the end of the silence.

| Interval | Data | n | min | median | p90 | max |
|---|---|---|---|---|---|---|
| (a) `t_end` → `speech_end` | corpus | 42 | 705 ms | 705 ms | 706 ms | 712 ms |
| (a) `t_end` → `speech_end` | live | 10 | 193 ms | 752 ms | 769 ms | 769 ms |
| (b) `speech_end` → `final: true` | corpus | 42 | 0 ms | 167 ms | 526 ms | 786 ms |
| (b) `speech_end` → `final: true` | live | 10 | 0 ms | 0 ms | 303 ms | 303 ms |
| `t_end` → `final: true` | corpus | 42 | 705 ms | 873 ms | 1232 ms | 1491 ms |
| `t_end` → `final: true` | live | 10 | 496 ms | 758 ms | 807 ms | 807 ms |
| `t_start` → `speech_start` | corpus | 42 | 257 ms | 257 ms | 257 ms | 257 ms |
| `t_start` → `speech_start` | live | 10 | 288 ms | 294 ms | 320 ms | 320 ms |

`t_end` → `final: true` in the corpus, by utterance length: below 3 s, 705 ms (13 utterances); 5–10 s, median 888 ms, maximum 969 ms (20); above 10 s, median 1249 ms, maximum 1491 ms (8). Above 15 s, the utterance has two jobs.

Live values are about 50 ms higher than the corpus values. They include the capture delay of PipeWire and the socket.

**Fact (measured, indirect).** `t_end` against the real end of speech. Parakeet token times (onnx-asr) were compared with `t_end` for the 42 corpus utterances. The last token with letters started 164 ms before `t_end` (median; p10 −409 ms, p90 +15 ms, maximum +132 ms). A token lasts about 100–250 ms. Thus `t_end` is near the real end of speech, and (a) is near the real VAD wait. An energy method was also tried. It gave results that depended strongly on the threshold (median −100 to −520 ms). It is not used.

**Observation (live, 1 of 10).** In log `05`, `speech_end` came 193 ms after `t_end`, not 700 ms. Cause (code, `segmenter.py`): a frame with a probability between `end_threshold` and `start_threshold` (for example a breath) moves `t_end` and retracts the speculative text. But it does not set the silence counter to zero. Thus the silence that ends the utterance counts from the earlier pause. The old text of [10](10-cli-ipc-status.md) §10.2 said “The line comes `min_silence_ms` after `t_end`”. This was not true in this case. The orchestrator must not calculate the time of `speech_end` from `t_end`. The behavior stays, and §10.2 now describes it (user decision 2026-10-10).

## 4. Event fields

| Field | Status |
|---|---|
| Utterance identifier | **yes**: `utt` (increases across sessions) and `session_id` |
| Sequence number | **missing**. No event has a sequence number. In one utterance, the order of the lines on the socket is the order of the events. Retractions refer to the jobs with `job_ids`. |
| `CLOCK_MONOTONIC` time stamp | **partly**. `t_start`, `t`, `t_end` and `t_ready` are `time.monotonic()` of the daemon. The time of the audio is given, not the time when the line was sent. `transcript_retracted`, `utterance_dropped` and `job` have no time stamp. The client must take its own time when it reads the line (the example client writes `rx`). |

**Fact (tested).** On this machine, Python `time.perf_counter()` and `time.monotonic()` both use `clock_gettime(CLOCK_MONOTONIC)` (`time.get_clock_info`). Thus the orchestrator can compare `time.perf_counter()` directly with the daemon times.

**Observation (code `eade7d4`).** A speculative job whose text was not sent (log `02`, job 6) still gave a `job` event with `result: "sent"` and `chars: 31`. In the corpus run, 35 of 84 jobs had `sent`, but their text was not a part of a final transcript. A speculative job withdrawn from the queue, and a job that a cancel removed from the queue, gave no `job` event. Fixed after the report, see below. The logs `02` and `05` still show the old `sent`.

## 5. CPU load

| Process | Threads | CPU during speech | CPU during a job | Data |
|---|---|---|---|---|
| daemon `local-stt` | 23 (live), 14 (corpus process) | 2–3 % of one core (Silero, 1 thread) | the same | both |
| engine `local-stt-engine` | 36; 13 use CPU | almost 0 while the user speaks | 4.5–5.4 cores for 0.3–0.5 s (short utterances) or up to 1.3 s (long) | both |

- **Fact.** The engine runs only after a pause: a speculative job at 250 ms of silence, or a final job. It does not run while the user speaks. Engine CPU in the corpus run: 224 CPU seconds for 50 s of engine time, thus 4.5 cores during a job. Live: 16.5 CPU seconds for 3.1 s of engine time, thus about 5 cores.
- **Fact.** Average engine CPU over the speech time, including the 700 ms of silence until `speech_end`: 60 % of one core (corpus), 78 % (live). The highest 1 s window had 459 % (corpus) and 241 % (live).
- **Fact (config).** The engine uses `intra_op_num_threads = 4`. More than 4 threads use CPU, because onnxruntime also has wait and pool threads.
- **Assumption.** A job at the same time as llama-server (4 threads) shares 4 physical cores with it. Both take longer. The first speculative job comes at `t_end + 250 ms`, exactly when the orchestrator would start the prompt processing. This was not measured.

## 6. Barge-in and echo

No new measurement. Task 6.5 measured this on 2026-10-10 ([15](15-implementation-plan.md) task 6.5, [acceptance-v0.6.md](acceptance-v0.6.md)).

- **Fact (measured in 6.5).** On the built-in speakers without echo cancellation, the microphone hears the TTS. There were 7.3–7.7 false `speech_start` events per minute. Almost every TTS answer became a transcript with almost correct text.
- **Fact (measured in 6.5).** With PipeWire `module-echo-cancel` (webrtc), there were 0 false `speech_start` events. The user's speech during the TTS still gave `speech_start`. But speech that fully overlapped the TTS gave wrong text.
- With headphones, this does not occur. Headphones are the default assumption (user decision 2026-10-10).

## Event logs for tests

Location (outside the repository, user decision 2026-10-10): `~/.local/share/local-stt/bench/stream-2026-10-10/logs/`. Each file has the events of one utterance, plus the `job` events of its jobs. Each line has `rx`, the monotonic time when the client read it. The full sessions are `live.jsonl` and `live3.jsonl`.

| File | Speech | What it shows |
|---|---|---|
| `01-short-question.jsonl` | “Jaka będzie jutro pogoda w Krakowie?” | `final: false` 679 ms after `t_end`, then `speech_end` and the identical `final: true` with the same job |
| `02-short-pause-no-partial.jsonl` | “Ile kosztuje bilet do Gdańska… w drugiej klasie?”, short pause | a speculative job (6) without a sent text and without `transcript_retracted`; the final text comes from job 7 |
| `03-long-sentence-no-partial.jsonl` | “Napisz krótką wiadomość do Ani, że spóźnię się 10 minut na obiad.” (4.6 s) | no `final: false`; `final: true` 38 ms after `speech_end` |
| `04a-long-pause-part1.jsonl`, `04b-long-pause-part2.jsonl` | “Ile kosztuje bilet do Gdańska? … W drugiej klasie.”, pause of about 2 s | one question becomes two utterances. The first part is a complete question with “?” |
| `05-retraction.jsonl` | “W drugiej klasie.” with a sound after it | `final: false`, `transcript_retracted`, then `speech_end` 193 ms after `t_end` and a new job |

The pause case has two files, because the pause split it into two utterances. The utterance with a short pause is `02`.

## Recommendations

1. Start the prompt processing with the `final: false` text, without its end punctuation. Use only the `final: true` text as the result.
2. Expect no `final: false` text for utterances of 4 s or more. For these, the earliest text is `final: true`, 0.8–1.5 s after `t_end`. A shorter `speculative_ms` or a faster engine would be necessary to change this. Both need a decision.
3. A pause longer than 700 ms splits a question. The first part can look complete (“Ile kosztuje bilet do Gdańska?”). The orchestrator must decide whether to wait for more speech after `speech_end`.
4. Take the receive time of each line with `time.perf_counter()`. Do not calculate the time of `speech_end` from `t_end`.
5. Measure the prompt processing time while a local-stt job runs. Both use the same 4 cores.
6. Possible protocol changes (only after approval): a sequence number for each event, and a send time stamp in each event.

## Changes after the measurement

User decisions 2026-10-10 about the three problems of this report:

| Problem | Decision | Change |
|---|---|---|
| `speech_end` less than 700 ms after `t_end` | no change of the behavior | [10](10-cli-ipc-status.md) §10.2 describes when the line comes, and that a client must not calculate it from `t_end` |
| `job` result `sent` for a speculative text that was not used | new result `retracted`; one `job` event for each job | the `job` event of a speculative job comes when its result is known: `sent` when its text is a part of the final transcript, else `retracted`. A withdrawn speculative job gives `retracted` with `processing_s` `null`. A job that a cancel removes from the queue gives `cancelled`. Specs: [10](10-cli-ipc-status.md) §10.2, [04](04-state-machine.md) |
| a pause splits a question | no change in local-stt | [10](10-cli-ipc-status.md) §10.2 describes the split and how a client can add the second part at the end of its input |

Check of the `job` change (2026-10-10): 7 new unit tests; ruff, mypy and the full test suite (1088 tests) pass. A second corpus run with the new code gave 84 `job` events for 84 jobs: 49 `sent` and 35 `retracted`. Each `sent` job is in a final transcript, and no `retracted` job is in one. The first run gave 84 `sent`. Live check after `install.sh --no-apt` (`doctor` 17 OK), 4 utterances with a short pause: job 13 (its text was not sent) and job 15 (its text was sent as `final: false`, then retracted) gave `retracted`. Jobs 14, 16, 17 and 18 were in the final transcripts and gave `sent`.
