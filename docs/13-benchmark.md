# 13. Benchmarking and model selection

The benchmark is the **first implementation step** ([15](15-implementation-plan.md), stage 0). Its results determine the default `stt.model`, `stt.threads`, and `stt.dynamic_audio_ctx`. Until results are available, use `small-q5_1`, `threads=4`, and `dynamic_audio_ctx=false`.

## 13.1 Questions it answers

1. Which model delivers the lowest WER for Polish while keeping p90 PTT latency ≤ 2.5 s for 4–10 s utterances (N2)?
2. Can the same model sustain continuous mode for 10 minutes without a growing queue (N3), including after the CPU heats up?
3. Four or eight threads?
4. Does matching `audio_ctx` to the recording length reduce latency without sacrificing quality?
5. Does OpenBLAS help (optional, separate build)?

## 13.2 Corpus

### A. Custom recordings (primary—the actual microphone, voice, and environment)

`local-stt record-corpus ~/stt-corpus`:

- displays sentences from `bench/prompts_pl.txt`, records each after Enter is pressed (Enter stops recording), and saves `NNN.wav` (16 kHz mono s16) plus `NNN.txt` (reference text),
- allows a recording to be repeated (`r`) or a sentence to be skipped (`s`).

`bench/prompts_pl.txt` (part of the repository) contains about 40 utterances:

| Group | Count | Length | Purpose |
|---|---:|---|---|
| short commands/sentences | 12 | 1–3 s | PTT, typical short notes |
| medium | 16 | 4–10 s | primary N2 case |
| long | 8 | 12–25 s | `max_segment_s` and `audio_ctx` boundaries |
| difficult | 4 | 5–10 s | proper names, numbers, technical Anglicisms, many ą/ę/ł/ż/ź/ś/ć/ń characters |

Also include a **continuous recording** under `long/` (`record-corpus --long`): about five minutes of read-aloud text with natural pauses (for example, a Polish Wikipedia article saved as the reference) for testing continuous mode.

### B. FLEURS pl_pl (optional—for comparability with the paper)

`local-stt bench --dataset DIR`. The directory uses the format from A. `scripts/fleurs_to_corpus.py` converts 50 random samples from the FLEURS test set (CC-BY-4.0). Data is downloaded manually and online, outside the daemon.

## 13.3 Metrics

| Metric | Definition | Source |
|---|---|---|
| **WER** | word-level Levenshtein distance / number of reference words, after normalization | `local_stt/bench/wer.py` (dependency-free) |
| **CER** | the same calculation at character level | as above |
| Normalization | lowercase, remove punctuation `.,;:!?…„”"'()-–—`, collapse whitespace; **leave Polish characters unchanged** (losing them is an error); do not normalize numbers—differences such as `2024`/“dwa tysiące…” are reported separately as `numeric_mismatch` | |
| **RTF** | `processing_s / audio_s` | client-side HTTP timing |
| **Time-to-text latency (`text_ready_s`)** | `vad_trim + http + text_processing`; benchmark excludes the injector, capture-finalization cost, and production queue; stage 0 uses the RMS gate, and v0.2 onward uses VAD trimming according to the config | measured by `bench` |
| **PTT latency (`total`)** | from PTT release until the injector returns after successful insertion, including audio finalization, queueing, waiting, and clipboard restoration (12 §12.1); insertion cost is not constant | full daemon, `timings` logs |
| p50 / p90 | separate `text_ready_s` and `total` percentiles for the “medium” group; these metrics are not equivalent | |
| CPU | server-process `utime+stime` from `/proc/<pid>/stat` / wall-clock time | |
| Peak RAM | `VmHWM` from `/proc/<pid>/status` | |
| Thermals | `/sys/class/thermal/thermal_zone*/temp` (`x86_pkg_temp` zone), `scaling_cur_freq` for all CPUs, sampled every second | |
| Queue | `queued_audio_s` over time (soak test) | |
| Segmentation | segment count, percentage of `max_length` cuts, number of filtered segments | |
| **Incorrect segmentations** | a cut at `t` is incorrect only if a reference word spans `[start, end]` and `start + 0.040 < t < end - 0.040` (seconds). Cuts in silence and between words are valid. Report the count and percentage of recording-internal cuts | soak test |

The `long/NNN.words.json` reference contains the words actually spoken and manually verified `start`/`end` intervals in seconds. Obtain an initial transcript and token timestamps with `whisper-cli -m <path to ggml-large-v3-turbo-q5_0.bin> -l pl -ojf -f long/NNN.wav`, then combine tokens into words and correct the boundaries against the recording. `-ml 1 -ojf` alone does not produce a reliable word reference: without `-sow`, the division is token-based ([CLI v1.9.4 source](https://github.com/ggml-org/whisper.cpp/blob/v1.9.4/examples/cli/cli.cpp)). Record the manually verified reference version in the report. Words shorter than 80 ms have no interior beyond the tolerance; report their count as a metric limitation. File boundaries and technical joins between soak-test loops are not Segmenter cuts and are excluded from the denominator.

## 13.4 Matrix

For each configuration, `local-stt bench` starts a **temporary** `whisper-server` on a random free port (the OS picks it: bind a socket to `127.0.0.1:0`, read the port, close the socket, pass it to `--port`; if the server fails to bind in the meantime, retry with a new port, at most 3 attempts), so `bench` and `transcribe --model` never collide: `nice -n 5` (matching `Nice=5` in the unit), random `--request-path`, start → `/health` → warm-up (one discarded request) → measurements → stop.

Before starting, it checks whether `local-stt-whisper.service` is active. If so, it refuses to proceed and displays `systemctl --user stop local-stt-whisper local-stt` (the `--allow-concurrent` flag skips this check), because two servers would distort CPU and RAM results.

| Dimension | Values |
|---|---|
| model | `base-q5_1`, `small-q5_1`, `small-q8_0`, `small`, `medium-q5_0`, `large-v3-turbo-q5_0` |
| threads | 4, 8 |
| dynamic_audio_ctx | false, true |
| beam | greedy (all); `-bs 5` only for the top two after stage 1 |

The sequence is economical because the full matrix would take hours on this CPU:

1. **Stage 1—`bench --quick`.** All models × `t∈{4,8}` × `dynamic_audio_ctx∈{false,true}`, using only the “medium” group (16 files). A model is eliminated for speed only when p50 `text_ready_s` > 6 s in **all four** configurations. This prevents the full encoder window from eliminating a model before `audio_ctx` is tested. `base-q5_1` remains a test control, not a production candidate.
2. **Stage 2.** Remaining models × `t∈{4,8}` × `dynamic_audio_ctx∈{false,true}` over the entire A corpus.
3. **Stage 3—`bench --soak --model M --threads T --audio-ctx X`.** For the top one or two: play the `long/` recording through the real `Segmenter` in real time, looped to 10 minutes, while measuring the queue, RTF, and thermals. Run on **AC power and battery** (`powersave` governor).
4. **Sanity check.** Run `whisper-bench -m <model> -t 4` for each model (raw encoder time) to separate HTTP and pipeline overhead from engine performance.

Measure each configuration three times. Report the WER for every run plus its mean and spread; do not assume identical answers because the production HTTP contract permits temperature fallback (06 §6.5), and the matrix also includes beam search. For each file, report the median time from three repetitions; calculate “medium” group percentiles from those medians. Record decoding parameters so equivalent settings are compared.

## 13.5 Decision rule

```text
production = configurations excluding base-q5_1, with peak server RSS ≤ 1 GB (N1),
             and with dynamic_audio_ctx=true only when
             WER(true) − WER(false) ≤ 1.0 pp for the same model, threads, and beam

# Stage 0: provisional selection, without N2 confirmation yet.
provisional = { configurations from production with p90_text_ready_s(medium) ≤ 2.5 s }

# v0.1: full daemon + injector, at least 20 utterances of 4–10 s per configuration.
ptt_candidates = { configurations from provisional with p90_total ≤ 2.5 s }
PTT_default    = argmin mean WER(corpus A) over ptt_candidates
                 tie (WER difference < 1 pp) → lower peak RAM → lower latency
                 latency tie (< 5%) → 4 threads

# Do not change threads or audio_ctx after selection without rechecking N1/N2.

continuous: PTT_default passes if, in the soak test (on battery):
    mean RTF ≤ 0.5  AND  queued_audio_s has no upward trend over the final 5 min
    (linear-regression slope ≤ 0.05 s/min)  AND  no sustained CPU-frequency drop > 30%
if it fails → test the next faster production model satisfying N1/N2; if that model passes and the WER difference > 3 pp,
    only then implement stt.continuous_model (15, v0.3); otherwise use the same faster model for both modes.
```

Stage 0 ranks configurations by quality and time to text; it does not claim N2 compliance. In v0.1, measure the full `total` for successive candidates until one meets the threshold. Do not subtract an arbitrary 100 ms or add separate stage percentiles. The measurement includes successful insertion into the applications listed in 14.4, including with existing clipboard content; the report states the backend, application, clipboard content type, wait times, and failed-paste rate. Do not classify failures as fast successes. Report `inject` separately alongside `total`; the additional 150 ms before restoration is part of it.

If no model satisfies N2 (for example, even `small-q5_1` has p90 > 2.5 s), do not silently change the requirement. Instead, the report presents the **best available compromise and an explicit recommendation to change N2** for approval.

## 13.6 Results

- Raw data: `~/.local/share/local-stt/bench/<ISO-timestamp>/results.jsonl` (one line = one file × configuration) plus `system.json` (CPU, governor, power source, whisper.cpp version, kernel).
- Report: `local-stt bench report DIR` generates Markdown tables. Copy the result that establishes the defaults to `docs/benchmark-results.md` in the repository, together with the date and selection rationale.
- Corpus transcripts stored in the results are content recorded by the user for testing, under the rule in [12](12-logging-privacy-errors.md) §12.2.

## 13.7 Hypotheses to verify (not facts)

- The Whisper paper's Polish (FLEURS) tables report WER of 30.8% for base, 14.7% for small, and 8.0% for medium. WER will be higher with the user's microphone and spontaneous speech, but the model ranking should remain similar.
- Public `whisper-bench` results from four-core laptop CPUs of this generation (ggml-org/whisper.cpp issue #89) vary widely (for example, on an i7-8750H: small encoder ~4.2 s, medium ~13 s with four threads; eight threads did not improve speed). The i5-8365U (U-series, lower TDP) is expected to be slower, so **`medium` and `large-v3-turbo` without `audio_ctx` will probably fail N2**. This is why stages 1–2 test `audio_ctx`.
- q5_1 versus f16 quantization for `small` is expected to have little effect on WER. This must be measured.
