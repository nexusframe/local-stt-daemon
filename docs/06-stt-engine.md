# 06. STT engine — whisper.cpp

Knowledge baseline: whisper.cpp **v1.9.4** (2026-09-11). The facts below were verified against `examples/server/server.cpp` from that tag, which differs from the outdated server README in several places.

## 6.1 Why a separate `whisper-server`

Options considered ([03](03-decisions.md), ADR-002):

| Option | Pros | Cons |
|---|---|---|
| **`whisper-server` (HTTP, localhost)** ✅ | native build (AVX2, `GGML_NATIVE`), model remains in RAM between requests, model change = service restart, engine crash does not kill the daemon, same binary for benchmarking, no Python C bindings | additional systemd service, HTTP overhead (~ms, negligible) |
| `pywhispercpp` / custom ctypes | one process | compilation through pip, wheel may lack native flags, a C segfault kills the daemon, harder engine replacement |
| `whisper-cli` per recording | simplest | loads the model on every invocation (0.3–1 s + cold cache) |
| faster-whisper (CTranslate2) | fast on CPU | Python + CTranslate2 + HF models (~GB of dependencies), outside the “whisper.cpp” requirement — remains a candidate for a second `SttEngine` |

## 6.2 Build

```bash
sudo apt install build-essential cmake git
git clone --depth 1 --branch v1.9.4 https://github.com/ggml-org/whisper.cpp ~/.local/share/local-stt/src/whisper.cpp
cd ~/.local/share/local-stt/src/whisper.cpp
cmake -B build -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF -DGGML_NATIVE=ON -DWHISPER_BUILD_TESTS=OFF
cmake --build build -j"$(nproc)" --config Release --target whisper-server whisper-cli whisper-bench
install -m755 build/bin/whisper-server build/bin/whisper-cli build/bin/whisper-bench ~/.local/share/local-stt/bin/
```

- `GGML_NATIVE` is ON by default, so the build immediately uses this machine's AVX2/FMA. **The binary is not portable to older CPUs**, which is acceptable because it is built locally. We still pass `-DGGML_NATIVE=ON` explicitly: ggml switches the default to OFF when `SOURCE_DATE_EPOCH` is set in the environment (verified in `ggml/CMakeLists.txt`, v1.9.4).
- `-DBUILD_SHARED_LIBS=OFF` is required: on Linux the default is ON, so the binaries would link `libwhisper.so`/`libggml*.so` from `build/` and break once copied to `bin/` or when `build/` is removed (verified in `CMakeLists.txt`, v1.9.4).
- Flash attention is **enabled** by default (`-fa`) in the server, CLI, and library.
- The version is pinned by tag, and `install.sh --whisper-tag TAG --rebuild-whisper` rebuilds it explicitly. `whisper-server` has no `--version` flag, so after the build we save the tag to `bin/.whisper-tag` and compare against that file.
- **OpenBLAS** (`-DGGML_BLAS=1 -DGGML_BLAS_VENDOR=OpenBLAS`, package `libopenblas-dev`) is a benchmark option. It remains disabled by default until measurements show a benefit ([13](13-benchmark.md)).
- We do not build FFmpeg support. Without `--convert`, the server reads WAV directly from the uploaded bytes.

Binaries: `whisper-server`, `whisper-cli`, `whisper-bench` (plus `quantize` if it is ever needed).

## 6.3 Models

Download: `local-stt models pull <name>` downloads `ggml-<name>.bin` from `https://huggingface.co/ggerganov/whisper.cpp/resolve/main/` (the same source as `models/download-ggml-model.sh`) and verifies its SHA256 checksum against `src/local_stt/models.sha256`. The checksum file is package data (read through `importlib.resources`), so the command works identically from the repository and from the installed venv, which does not contain `scripts/`. A file with an invalid checksum is deleted.

| Model (`ggml-<name>.bin` file) | Size | Polish WER FLEURS / CV9 (Whisper paper) | Role |
|---|---:|---|---|
| `base-q5_1` | 57 MiB | 30.8 / 32.8 (base) | test/fallback only — **too weak for Polish** |
| `small-q5_1` | 181 MiB | 14.7 / 16.9 (small) | **initial default** |
| `small-q8_0` | 252 MiB | same | benchmark candidate |
| `small` (f16) | 465 MiB | same | small quality baseline |
| `medium-q5_0` | 514 MiB | 8.0 / 10.1 (medium) | PTT candidate if latency permits |
| `large-v3-turbo-q5_0` | 547 MiB | no figures in the paper; better than medium in OpenAI charts | PTT candidate with a fixed `audio_ctx` |

Notes:

- q5/q8 quantization reduces RAM use and loading time. We measure its WER impact ourselves ([13](13-benchmark.md)).
- The preliminary design treated `base` as the primary “responsive” candidate. The paper's Polish figures (≈31% WER, or every third word wrong) disqualify it. `base` remains for testing only.
- **Model selection is measurement-driven.** The initial default is `small-q5_1`; [13-benchmark.md](13-benchmark.md) determines the final default. One model serves both modes. A second model is introduced only if required by the §13.5 rule (see 6.7).

## 6.4 Starting the server

`local-stt.service` does not start the server. The `local-stt-whisper.service` unit does so ([11](11-daemon-systemd-installation.md) §11.4), using arguments from `whisper-server.env`. That file is generated from the config ([09](09-configuration.md) §9.4). Expanded command:

```bash
~/.local/share/local-stt/bin/whisper-server \
  --host 127.0.0.1 --port 8178 \
  --request-path /<secret: 32 hex characters> \
  -m ~/.local/share/local-stt/models/ggml-small-q5_1.bin \
  -l pl -t 4 -bs -1 -sns
```

| Flag | Value | Rationale |
|---|---|---|
| `--host 127.0.0.1` | forced (not configurable) | privacy (N5) |
| `--port 8178` | `stt.port`, nonstandard to avoid the default 8080 | |
| `--request-path /<secret>` | random prefix for all endpoints (`/…/inference`, `/…/health`, `/…/load`), generated by `install.sh` in `~/.config/local-stt/secret` (0600) | CSRF protection: a browser web page does not know the path, so it cannot change the model or block the server (12 §12.2) |
| `-l pl` | **required** — the server defaults to `en` | |
| `-t 4` | `stt.threads`; defaults to the physical core count, benchmark 4 vs 8 | HT provides little benefit (whisper.cpp#89) |
| `-bs -1` (greedy) | `stt.beam_size` | beam search is ×2–5 slower; the benchmark decides |
| `-sns` (`suppress_nst`) | always | suppresses non-speech tokens (`[muzyka]`, etc.) |
| no `--vad` | VAD runs in the daemon before submission | the server receives speech only |
| no `--convert` | we send a ready-made 16 kHz mono s16 WAV | no ffmpeg |

The server processes **one request at a time** (`std::mutex`), matching the single `PipelineWorker`. A client disconnect during processing interrupts decoding (HTTP 499). We do not use this for cancellation, but it is harmless.

## 6.5 HTTP contract (the subset we use)

### `GET /health`

- `200 {"status":"ok"}` → `engine=READY`
- `503 {"status":"loading model"}` → `engine=STARTING` (handled defensively; not a stage of normal server startup)
- connection error → `STARTING` or `DOWN` according to the rule in [04](04-state-machine.md) §4.5

All paths have the `--request-path` prefix. The client reads the secret from `~/.config/local-stt/secret`. In v1.9.4, the model is loaded before HTTP begins listening, so normal startup produces **connection failure → 200**, with no observable 503. The client then derives `STARTING` from the time since startup/restart ([server code](https://github.com/ggml-org/whisper.cpp/blob/v1.9.4/examples/server/server.cpp)).

### `POST /inference` (multipart/form-data)

| Field | Value | Notes |
|---|---|---|
| `file` | WAV RIFF PCM s16le, 16000 Hz, mono | built in memory (`io.BytesIO` + `wave`) |
| `response_format` | `verbose_json` | segments with `no_speech_prob`, `avg_logprob` |
| `language` | `pl` | redundant with `-l`, to avoid depending on server flags |
| `no_language_probabilities` | `true` | **otherwise the server runs the encoder a second time** |
| `temperature` / `temperature_inc` | `0.0` / `0.2` | temperature fallback on high entropy |
| `prompt` | vocabulary + context (6.6) | omitted when empty |
| `no_timestamps` | `false` | segment boundaries are needed for filtering |
| `audio_ctx` | `0` or the fixed `stt.audio_ctx` (6.7) | |

Response (the portion we read):

```json
{
  "text": " Dzisiaj chciałbym pojechać do Warszawy.",
  "duration": 3.2,
  "segments": [
    {"id": 0, "text": " Dzisiaj chciałbym pojechać do Warszawy.",
     "start": 0.0, "end": 3.1, "avg_logprob": -0.21, "no_speech_prob": 0.02}
  ]
}
```

- `avg_logprob` includes special tokens and is therefore biased. We treat it as a relative signal with a configurable threshold, not as an absolute value.
- The response's `temperature` field echoes the requested value rather than the temperature actually used, so we ignore it.
- `compression_ratio` does not exist. We detect hallucination loops ourselves (6.8).

Request timeout: `max(10 s, 4 × audio_length × RTF_from_last_10_jobs)`, capped at `stt.request_timeout_max_s` (120 s).

### `POST /load` — intentionally **unused**

Verified in `examples/server/server.cpp` from v1.9.4:

- for a nonexistent file, the handler returns 400 but **leaves the state at `LOADING_MODEL`**, so `/health` returns 503 until restart,
- if model loading fails, the process terminates (`exit(1)`),
- loading holds the inference mutex.

We therefore always change the model by restarting the service with a new `whisper-server.env` ([04](04-state-machine.md) §4.6). This provides one path and ensures the restarted server always uses the configured model. The cost is reloading the model (seconds), the same as `/load`.

## 6.6 Prompt (vocabulary and context)

The `prompt` field is Whisper's `initial_prompt` (prompt-context budget of about 224 tokens). The validator limits `stt.vocabulary_prompt` to 300 characters and the context to 200 characters. These are usability limits, **not a guarantee that the text fits within the token limit**: Polish words and technical names may require many tokens. In v0.1–v0.3 we pass the prompt below and allow the engine to truncate it to its budget; the documentation does not guarantee preservation of the entire vocabulary. Exact budgeting requires a tokenizer compatible with the selected model and remains an STT-adapter extension:

```text
<stt.vocabulary_prompt>  +  " "  +  <last ≤200 characters of injected text from the same continuous session>
```

- `stt.vocabulary_prompt` may be, for example, `"Kubernetes, PipeWire, whisper.cpp, Gdańsk."`. Proper names in the prompt substantially improve their spelling.
- In PTT, the context consists only of the vocabulary because each recording is independent.
- In continuous mode, context carries punctuation and capitalization across segments. Risk: a hallucination repeats the prompt. The filter in 6.8 guards against this (a result identical to the suffix of the session context is rejected). The vocabulary is never treated as an echo source: a PTT utterance consisting only of a vocabulary word (e.g. “Gdańsk”) must not be discarded.

## 6.7 `audio_ctx` — the main latency lever

The Whisper encoder always processes a **30 s window** (1500 frames, 50 frames/s), even for a 3 s recording. On CPU, the encoder is the dominant cost. `audio_ctx` shortens this window. The model was trained on the full window, so a shortened context can lower quality; the benchmark decides whether a value is acceptable ([13](13-benchmark.md) §13.5).

### Policy: one fixed value per server, full window as the only fallback

`stt.audio_ctx` is either `0` (full window, the default until the benchmark selects a value) or a fixed number of frames. For every request the adapter sends exactly one of two values:

```text
needed = ceil(duration_s * 50) + stt.audio_ctx_margin        # margin defaults to 128 (~2.5 s)
audio_ctx = stt.audio_ctx  if stt.audio_ctx > 0 and needed <= stt.audio_ctx
            0             otherwise                          # full 30 s window
```

With `stt.audio_ctx = 1000` and the default margin, recordings up to 17.4 s use the shortened window; longer PTT recordings use the full window. Continuous segments (`vad.max_segment_s = 15`) always fit. A server therefore sees at most two `audio_ctx` values during its lifetime, which is the setup that was verified; changing `stt.audio_ctx` restarts the server (04 §4.6).

### Why not a per-request value (the former `stt.dynamic_audio_ctx`)

Measured 2026-09-17, whisper.cpp v1.9.4, FLEURS `pl_pl` medium group (16 files, 4.6–9.9 s), greedy decoding, timestamps on, one long-running `whisper-server` as in production. Scripts and raw logs were ad hoc (not in the repository); results were reproduced word for word in a second run on AC power with the `performance` platform profile.

| Setup (`small-q5_1` for the first two rows, `small-q8_0` for the rest) | WER | p90 per request |
|---|---|---|
| `audio_ctx = 0` (full window) | 21.7 % | — |
| per-request `audio_ctx = ceil(duration_s * 50) + 128` | 49.6 % (hallucinated multilingual segments, loops) | — |
| `small-q8_0`, `audio_ctx = 0` | 23.3 % | 4.5–6.0 s |
| `small-q8_0`, fixed `audio_ctx = 1000`, two passes | 23.8 % (identical per file in both passes) | 3.3–4.0 s |
| `small-q8_0`, fixed `audio_ctx = 750`, two passes | 26.0 % (identical per file in both passes) | 2.6–3.0 s |
| `small-q8_0`, medium @ 1000 interleaved with 12–17 s files @ full window, two passes | medium 24.2 %, long 21.7 % (dedicated servers: 23.8 % / 23.2 %) | medium 3.5–4.0 s |

Findings:

- **Per-request values break decoding in a long-running server.** The same files are correct with `whisper-cli -ac N` and with a fresh server handling a single request, so the failure depends on server state carried across requests with different `audio_ctx` (timestamp decoding contributes: a fresh server failed on one file with timestamps on and succeeded with them off). The root cause in whisper.cpp is not known.
- **A fixed value is stable:** repeated passes give identical text for every file.
- **Alternating a fixed value with the full window works but is history-dependent:** 15 of 24 files differ by a few words from the dedicated-server result (both better and worse), with no quality loss overall and identical results across passes. Benchmark comparisons therefore use the same request order for every configuration.
- **Latency varies ±20 % between runs** with temperature and background load (68–71 °C, load 2–4.5 in the second run); relative gains are stable: `1000` is ~30 % faster than the full window, `750` ~50 %.
- Only `small-q8_0` and two fixed values were tested. Other models and values need the benchmark; a whisper.cpp upgrade requires repeating the interleaving test.

### Separate models for PTT and continuous mode?

`whisper-server` holds one model. Supporting two models would require two servers (2× RAM) or restarting the server with another model on every mode change (a cost of several seconds). Decision: **one model**, configured through `stt.model`. The `stt.continuous_model` option (a second server on port `8179`, started only while continuous mode is enabled) is described as a v0.3 extension in [15](15-implementation-plan.md) and will be implemented only if benchmarking shows that one model cannot meet N2 and N3 simultaneously.

## 6.8 Result → `Transcript`

`WhisperServerEngine.transcribe()` returns an engine-independent structure:

```python
@dataclass(frozen=True)
class TranscriptSegment:
    text: str                    # original spacing; a boundary may fall within a word
    start_s: float
    end_s: float
    no_speech_prob: float | None
    avg_logprob: float | None

@dataclass(frozen=True)
class Transcript:
    text: str
    segments: list[TranscriptSegment]
    audio_duration_s: float
    processing_s: float          # measured on the client side
    engine: str                  # "whisper.cpp"
    model: str                   # "small-q5_1"
```

The adapter preserves segment text without `strip()` and without adding separators. For `verbose_json`, v1.9.4 generates token timestamps by default and wraps segments at 60 characters; a split may fall inside a word ([server code](https://github.com/ggml-org/whisper.cpp/blob/v1.9.4/examples/server/server.cpp)). These segment boundaries do not represent word or utterance boundaries. Text assembly rules are defined in 08 §8.2.

Content filtering is performed by `TextProcessor`, not the engine ([08](08-text-injection.md) §8.2):

1. reject a segment when `no_speech_prob > stt.no_speech_threshold` (0.6) **and** `avg_logprob < stt.logprob_threshold` (-1.0),
2. reject a segment for which `re.search(pattern, text, re.IGNORECASE)` matches any pattern in `text.hallucination_patterns` (the `^…$` anchors are part of the pattern) — default list:
   - `napisy (stworzone|wykonane) przez społeczność amara\.org` — **confirmed** (openai/whisper#928)
   - `(zdjęcia|tłumaczenie) i napisy stworzone przez społeczność amara\.org` — confirmed (#928)
   - `^\s*dzięk(i|uję) za (uwagę|obejrzenie|oglądanie)[.!]?\s*$` — **unconfirmed**, added by analogy with “Thanks for watching.” This entry matches only the entire segment so that these words are not removed from a normal utterance.
   - `^\s*(za)?subskrybuj[^.]*[.!]?\s*$` — unconfirmed, as above.
3. reject repetitions: a segment identical to the preceding segment in the same result, or an n-gram (n ≥ 3 words) repeated ≥ 4 consecutive times (decoder loop),
4. **continuous mode only:** reject the entire result if, after normalization, it equals the suffix of the session context passed in the prompt (the `last_text` part, not `stt.vocabulary_prompt`). In PTT, and when the context is empty, this filter does not run.

Every rejection is logged at DEBUG as `filtered: <reason>`. Content is logged only when `logging.log_text = true` ([12](12-logging-privacy-errors.md)).

## 6.9 Engine interface (replaceability — N7)

```python
class SttEngine(Protocol):
    name: str
    def health(self) -> EngineHealth: ...                         # READY / STARTING / DOWN
    def transcribe(self, audio: np.ndarray, *, sample_rate: int,  # float32 mono [-1, 1]
                   language: str, prompt: str | None,
                   timeout_s: float) -> Transcript: ...
```

Implementations:

- `WhisperServerEngine` (v0.1)
- `FakeEngine` (tests: returns predefined text after a delay)

Implementation selection: `stt.engine = "whisper-server"`. A new engine (such as faster-whisper in a separate process) is added as a new class plus an entry in the `local_stt/stt/__init__.py` registry, without changing the rest of the code.
