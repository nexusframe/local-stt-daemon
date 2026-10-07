# 03. Decision log (ADR)

Each decision follows the same structure: context → options considered → decision → **strongest objection and response** → when to revisit it.

Legend:

- ✅ — binding decision,
- 🧪 — provisional decision, to be resolved by the benchmark ([13](13-benchmark.md)).

---

## ADR-001 ✅ Daemon in Python 3.12

- **Options:** Python, Rust, C++ (linking whisper.cpp), Go.
- **Decision:** system Python 3.12 in a venv.
- **Rationale:**
  - the heavy work (inference) happens in C++ in a separate process anyway,
  - the daemon mainly handles I/O and component orchestration,
  - mature libraries: sounddevice, onnxruntime, python-xlib,
  - shortest path to an MVP and easy testing.
- **Objection:** the GIL and Python overhead for real-time audio.
- **Response:** the stream carries 31 frames/s of 512 samples each. The PortAudio callback only copies data, while onnxruntime and numpy release the GIL. The N4 budget (≤ 5% of one core) is verified by the benchmark.
- **Revisit:** if N4 or N1 is not met after profiling.

## ADR-002 ✅ STT in a separate `whisper-server` process (HTTP over loopback)

- **Options:** `whisper-server`; bindings (`pywhispercpp`/ctypes); one `whisper-cli` process per recording; faster-whisper.
- **Decision:** `whisper-server` from a pinned whisper.cpp tag as a separate systemd service. Details: [06](06-stt-engine.md) §6.1.
- **Objection:** an additional TCP port increases the attack surface and creates a privacy risk.
- **Response:**
  - it listens only on `127.0.0.1` (the host is hard-coded in the `whisper-server.env` generator and verified by `doctor`),
  - the server does not retain data,
  - all endpoints are under a random `--request-path` (a 0600 secret), so a web page cannot call `/load` or `/inference` (CSRF),
  - a local user process with access to the secret can, at most, request transcription of its own audio.
- **Objection 2:** two processes require more management than one.
- **Response:**
  - systemd manages both,
  - in return, we get isolation from C++ failures, a native build, and the same binary for benchmarking,
  - changing the model means restarting the service rather than calling `POST /load`, which in v1.9.4 can leave the server in the `loading` state or terminate it ([06](06-stt-engine.md) §6.5).
- **Revisit:** if whisper.cpp removes the server or HTTP overhead proves measurable (> 5% of latency).

## ADR-003 🧪 Default model `small-q8_0` with `audio_ctx = 1000`; `base` for testing only

- **Context:** the preliminary design treated `base` as the “responsive” option.
- **Facts:** Polish WER (FLEURS) from the Whisper paper: base 30.8%, small 14.7%, medium 8.0%.
- **Decision:** `small-q8_0` (252 MiB), 4 threads, fixed `audio_ctx = 1000` — the only production configuration with p90 `text_ready_s` ≤ 5 s in the stage-0 benchmark on corpus B (2026-09-17) and corpus A (2026-10-03, WER 7.4 %), selected under the rule in [13](13-benchmark.md) §13.5 ([benchmark-results](benchmark-results.md)). The project started with `small-q5_1`; `medium-q5_0` is about twice as accurate but ~15 s per utterance on the reference CPU, and `large-v3-turbo-q5_0` is slower still. Remains 🧪 until v0.1 confirms N2 with the full `total` (13 §13.5).
- **Objection:** `small` may be too slow for continuous mode on the i5-8365U.
- **Response:**
  - continuous mode does not block recording (ADR-004),
  - the backlog has a hard limit with a clear message,
  - if the soak test fails, the benchmark will identify a faster configuration (fixed `audio_ctx`, quantization),
  - `base`, with one in three words wrong, is not useful regardless of speed.

## ADR-004 ✅ State as three independent components, one owner thread, capture is never paused

- **Context:** the preliminary `LISTENING → TRANSCRIBING → INJECT → LISTENING` diagram loses speech during transcription.
- **Decision:** `(mode, pipeline, engine)`, events sent to a single `controller` queue, and a job queue with one worker and cancellation generations. Details: [04](04-state-machine.md).
- **Objection:** the queue may grow indefinitely if the engine is too slow.
- **Response:** `continuous.max_backlog_s` disables the mode with a clear message. This is preferable to silently losing speech or accumulating minutes of delay.

## ADR-005 ✅ Audio: `sounddevice`, stream opened on demand

- **Options:** sounddevice (PortAudio); `pw-record`/`parec` as a subprocess; GStreamer; PyAudio.
- **Decision:** sounddevice, 16 kHz mono float32, block size 512. The stream is open only during PTT and continuous mode.
- **Objection:** opening it for every PTT recording cuts off the beginning of the utterance.
- **Response:**
  - the `start` sound played after the first frame indicates that the microphone is open; the user starts speaking after the signal and a short masking margin,
  - samples from the start-sound window are discarded, so the signal is not transcribed,
  - an always-open microphone means a permanent GNOME privacy indicator and switches Bluetooth headsets to the HFP profile, which is a worse tradeoff.
- **Objection 2:** PortAudio 19.6 has no native PulseAudio/PipeWire backend.
- **Response:** we open the `pipewire` PCM (pipewire-alsa) and select the source with `PIPEWIRE_NODE` (verified). PipeWire handles resampling and hot-plug; as a fallback, we resample in-process (`soxr`).

## ADR-006 ✅ VAD: Silero v6.2.1 through onnxruntime in the daemon

- **Options:**
  - Silero ONNX,
  - webrtcvad,
  - energy threshold,
  - whisper.cpp's built-in VAD (`--vad`),
  - Silero through the whisper.cpp C API (ctypes).
- **Decision:** streaming Silero ONNX with hysteresis in the daemon ([05](05-audio-and-vad.md)).
- **Why not server-side VAD:** the server's VAD operates on a recording that has already been sent. Continuous mode needs a live “end of utterance” decision to know what to send in the first place.
- **Why not webrtcvad or energy:** poor resistance to noise (keyboard, laptop fan), which is typical for this machine.
- **Objection:** onnxruntime adds ~15 MB+ of dependencies.
- **Response:** acceptable. A ctypes alternative using libwhisper would require building a shared library and maintaining our own ABI against an API of uncertain stability.

## ADR-007 ✅ Hotkeys: XGrabKey (python-xlib); PTT = right Ctrl, continuous = Shift + right Ctrl

- **Options:**
  - `Super+Space` (from the design),
  - XGrabKey,
  - XRecord/pynput (listening to all keys),
  - XInput2 raw events,
  - evdev (`/dev/input`, `input` group),
  - GNOME custom shortcuts.
- **Decision:** XGrabKey on the root window with Lock/NumLock variants. Default keys do not use Super. Details: [07](07-hotkeys-x11.md).
- **Rejected:**
  - `Super+Space` — used for switching input sources and conflicts with mutter's overlay key,
  - GNOME shortcuts — no release event,
  - evdev — requires adding the user to the `input` group, which grants access to all keyboards (keylogger capability),
  - XRecord and XI2 raw — listening to *all* keys collects unnecessary data, and the key also reaches the application.
- **Objection:** grabbing right Ctrl takes it away from applications.
- **Response:** yes, this is a deliberate cost. Left Ctrl remains available. The key can be changed in the config to `Pause`/`Menu`/`Insert`. The grab was empirically verified in this GNOME session.

## ADR-008 ✅ Configuration in TOML

- **Options:** YAML (from the design), TOML, JSON, INI.
- **Decision:** TOML (`tomllib` in the standard library, zero dependencies, unambiguous types, comments).
- **Objection:** `tomllib` is read-only, while `reload` might need to write.
- **Response:** the daemon never writes the user's config. It only generates `whisper-server.env`.

## ADR-009 ✅ Injection: clipboard + XTest Ctrl(+Shift)+V with our own selection owner; `xdotool type` as fallback

- **Options:** `xdotool type`; `xclip` + `xdotool key`; a custom python-xlib selection + XTest; XTest with keycode remapping.
- **Decision:** a custom `ClipboardOwner`. This lets us **know** whether the application retrieved the text (SelectionRequest), providing paste confirmation and safe clipboard restoration. Fall back to `type` for xterm and clipboard content that cannot be preserved faithfully. Details: [08](08-text-injection.md).
- **Objection:** overwriting the clipboard is an unexpected side effect.
- **Response:**
  - we save and restore every clipboard target up to 256 KiB each; larger content (such as a screenshot) forces `type` to avoid destroying it,
  - confirmation counts only requests from the active-window client after the shortcut is sent, so clipboard managers cannot produce false confirmation,
  - `xdotool` 3.20160805 from Ubuntu 24.04 has documented problems with Polish characters and multiple layouts, while Polish characters are central to the project,
  - the backend is configurable.

## ADR-010 ✅ Inject only final segments

- **Decision:** no partial results in the target window (in line with the preliminary design, §7).
- **Objection:** in continuous mode, the user sees no result for a long time.
- **Response:**
  - `min_silence_ms = 700` plus engine latency is usually 2–4 s from the end of a sentence,
  - a partial preview, available only in an explicitly enabled `status --watch`, was planned for v0.3 and moved to the backlog on 2026-10-06 (a preview request in flight delays the final segment, [15](15-implementation-plan.md) backlog item 9); it would never be sent to applications or notifications,
  - injecting and undoing text in third-party applications is unreliable on X11.

## ADR-011 ✅ X11 only in v0.1–v0.3; Wayland through interfaces

- **Context:** the target session is Ubuntu on Xorg.
- **Decision:** the only `HotkeyBackend` and `Injector` implementations are X11-based. At startup, the daemon checks the session and exits with a clear error (code 78) outside X11.
- **Revisit:** when the user moves to Wayland. Add the GlobalShortcuts portal or evdev for hotkeys and `wl-copy` + `ydotool`/`dotool` for injection.

## ADR-012 ✅ IPC: Unix socket + JSON Lines

- **Options:** D-Bus (session bus), Unix socket, HTTP over loopback, files/FIFO.
- **Decision:** a `0600` Unix socket in `$XDG_RUNTIME_DIR` + `SO_PEERCRED`, using the JSON Lines protocol ([10](10-cli-ipc-status.md)).
- **Objection:** D-Bus is the desktop standard and would simplify integration with GNOME extensions.
- **Response:** D-Bus in Python requires `dbus-next`/`PyGObject` and an event loop. The socket takes about 150 lines using the standard library. A D-Bus bridge can be added later as an IPC client.

## ADR-013 ✅ Feedback: sounds + notifications (errors only) + `status`; no tray in the MVP

- **Objection:** without an icon, the user does not know whether continuous mode is enabled.
- **Response:**
  - distinct start and stop sounds,
  - GNOME shows a microphone indicator while the stream is open (and it is open only during recording, providing a free and reliable indicator),
  - `systemctl --user status` shows `STATUS=`,
  - a tray (AppIndicator requires a GNOME extension) is a post-v0.3 enhancement.

## ADR-014 ✅ Threads instead of asyncio

Rationale in [02](02-architecture.md) §2.2. All key libraries are blocking.

## ADR-015 ✅ whisper.cpp built locally from a pinned tag (v1.9.4)

- **Options:** apt package (unavailable in 24.04), snap, prebuilt binary from GitHub, local build.
- **Decision:** local build, `GGML_NATIVE=ON`, tag pinned in `install.sh`.
- **Objection:** the build takes several minutes and requires `cmake`.
- **Response:** it is a one-time cost. In return, we get optimization for this machine's AVX2 and reproducible benchmarks.

## ADR-016 🧪 One model for PTT and continuous mode

- **Decision:** one `stt.model`. A second server for continuous mode is introduced only if the rule in [13](13-benchmark.md) §13.5 is satisfied.
- **Objection:** PTT can tolerate a slower, better model, while continuous mode needs a faster one.
- **Response:** true, but a second model costs an additional 0.2–0.6 GB of RAM and another process. First we measure whether the difference is material.

## ADR-017 ✅ Daemon-side hallucination filtering

- **Context:** on silence or noise, Whisper generates phrases including “Napisy stworzone przez społeczność Amara.org” (confirmed in openai/whisper#928).
- **Decision:**
  - a silence gate before submission (RMS in v0.1, VAD in v0.2),
  - `-sns` on the server,
  - `no_speech_prob` + `avg_logprob` filters,
  - a configurable pattern list,
  - repetition-loop detection.

  Details: [06](06-stt-engine.md) §6.8.
- **Objection:** a pattern may remove a genuine utterance.
- **Response:** unconfirmed patterns match only the **entire** segment (`^…$`), and every rejection is logged (DEBUG).

## ADR-018 🧪 Default engine Parakeet TDT 0.6B v3 in a separate process; whisper-server stays as an alternative (v0.4)

- **Context:** Goal (user, 2026-10-07): text should appear sooner; word-by-word output is not required.
- **Measured 2026-10-07** (scratch scripts outside the repo, AC, `performance`, extra cooling, 4 threads; results and scripts in `~/.local/share/local-stt/bench/parakeet-2026-10-07*/` and `canary-2026-10-07/`):

  | Corpus A (40 utterances) | `small-q8_0` @1000 (2026-10-03) | Parakeet TDT 0.6B v3 int8 (sherpa-onnx 1.13.8) | Canary 1B v2 int8 (onnx-asr 0.12, `pl` forced) |
  | --- | --- | --- | --- |
  | WER all / short / medium / long_utt / difficult | 7.4 / 19.6 / 7.1 / 3.9 / 18.0 % | 5.7 / 8.7 / 6.2 / 2.6 / 18.0 % | 4.3 / 0.0 / 2.4 / 2.6 / 23.0 % |
  | medium p50 / p90 | 3.55 / 3.68 s | 1.18 / 1.41 s | 2.08 / 2.26 s |
  | 20–24 s utterances | — | 2.4–4.2 s | 7.6–9.3 s |
  | peak RSS | 467 MB (server) | 1081 MB (Python process) | 1926 MB (Python process) |

  Same WER normalization as `bench` (`local_stt.bench.wer`). Parakeet is deterministic (same file → same text at 1, 4 and 8 threads).
- **Decision:** `stt.engine = "parakeet"` by default, served by its own systemd service like `whisper-server` (ADR-002: crash isolation, the daemon stays within its RSS budget). `stt.engine = "whisper-server"` stays as a supported alternative. No fallback engine in v0.4 (user decision 2026-10-07); see [15](15-implementation-plan.md) backlog item 10.
- **Objection:** Parakeet has no language parameter. On the user's own recordings it wrote a code-switched sentence ("Po code review zrób rebase i force push.") in Cyrillic in 3 of 5 takes, and it has no prompt, so `stt.vocabulary_prompt` and the continuous context do nothing.
- **Response:**
  - all 40 Polish corpus-A utterances and 5 of the user's English sentences came out in the right language (English 0 % WER with no switch, Whisper needs `en`),
  - code-switched speech is weak in every engine tested (5 sentences: Whisper 24 %, Parakeet 37 %, Canary 39 % WER); Whisper never changes script but garbles the fast takes as well,
  - the context tail moved WER by under 1 point (task 3.4), so losing it costs little; vocabulary biasing (sherpa-onnx hotwords) is untested — it crashed without a `bpe.vocab` file,
  - whisper-server remains one config key away.
- **Further costs:** ~1.1 GB RSS (N1 must be revised); on non-speech it outputs short English fillers ("Yeah.", "Mm-mm.") in 6 of 10 clips, where Whisper hallucinated in 10 of 10; the sherpa-onnx export fails on a 6-minute input (irrelevant with `vad.max_segment_s ≤ 28`).
- **Revisit:** if Cyrillic or wrong-language output bothers the user in daily use (backlog item 10), or if the v0.4 acceptance fails.
