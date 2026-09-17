# 02. Architecture

## 2.1 Processes

```text
┌──────────────────────── user session (systemd --user, graphical-session.target) ──────────────────────────────┐
│                                                                                                             │
│  ┌──────────── local-stt.service (Python) ────────────┐        HTTP 127.0.0.1:8178     ┌─ local-stt-whisper ─┐ │
│  │                                                    │  POST /inference (WAV)       │   .service          │ │
│  │  HotkeyListener ─┐                                 │ ───────────────────────────► │  whisper-server     │ │
│  │  IpcServer ──────┼──► Controller ──► PipelineWorker│ ◄─────────────────────────── │  (whisper.cpp,      │ │
│  │  AudioCapture ───┘      ▲   │          │           │     verbose_json             │   model in RAM)     │ │
│  │   └► Recorder/Segmenter─┘   │          ├► TextProcessor                           └─────────────────────┘ │
│  │        (Silero VAD, ONNX)   │          └► Injector ──► X11: CLIPBOARD + XTest / xdotool                  │
│  │  EngineMonitor ─────────────┘                                                                            │
│  └─────────────────────────────────────────────────────┘                                                  │
│        ▲ Unix socket $XDG_RUNTIME_DIR/local-stt/control.sock                                               │
│        └── local-stt CLI (status / toggle / cancel / reload / ptt)                                          │
└─────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
          ▲ microphone: PortAudio → ALSA → pipewire-alsa → PipeWire       ▲ X server (:1) — mutter, applications
```

## 2.2 Threads in the daemon process

| Thread | Data owner | Communication | Critical* |
|---|---|---|---|
| `main` | — | starts components, handles signals (`signal.set_wakeup_fd`), then waits for `Controller` | yes |
| `controller` | **the entire `DaemonState`** | reads `events: queue.Queue`; invokes component methods | yes |
| `hotkeys` | X11 connection #1 (grab) | → `events`; commands (regrab) through its own queue + wake pipe | yes |
| PortAudio callback | frame buffer | → `SimpleQueue` | (library thread) |
| `audio-consumer` | Recorder / Segmenter / SileroVad | frames from `SimpleQueue`, commands (`begin_ptt`, `reset`, `finish_ptt`, `flush`, `discard`) from the command queue; → `events` (`RecordingStarted/Finished`, `SpeechStarted/Ended`, `SegmentReady`, `FlushDone`, `RecordingLimitReached`, `AudioError`) | yes |
| `pipeline` | job queue, silence gate / PTT VAD trimming (its own SileroVad instance), X11 connection #2 (XTest), prompt context | `jobs: queue.Queue`; → `events` (`JobStarted/Finished/Discarded/Failed`, `EngineStateChanged`) | yes |
| `clipboard-owner` | X11 connection #3 (1×1 window, selections) | requests from `pipeline` through a queue + `Future` | yes |
| `engine-monitor` | — | → `events` (`EngineStateChanged`) | no |
| `timers` (`threading.Timer`) | — | → `events` (`ReconnectTick`, `CaptureOpenDue`) | no |
| `server-restart` (short-lived, only during reload ⟳) | — | `systemctl --user restart local-stt-whisper` → `events` (`ServerRestartDone`) | no |
| `ipc-server` (+ per-connection threads) | — | → `events` with a response `Future`; subscriptions receive status copies | no |

\* An exception in a critical thread terminates the process with exit code 1, and systemd restarts it ([12](12-logging-privacy-errors.md), E14).

Rules:

- Only `controller` changes state. Other threads send events.
- Each thread using X11 has **its own** connection (`Xlib.display.Display`), because python-xlib is not thread-safe.
- Components do not import one another except through interfaces in `local_stt/interfaces.py`. `local_stt/app.py` wires everything together.
- Threads were chosen over `asyncio`: sounddevice, python-xlib, and `http.client` are blocking, while the number of threads is small and fixed. `asyncio` would only add a `run_in_executor` layer.

## 2.3 Repository structure

```text
local-stt-daemon/
├── pyproject.toml
├── requirements.lock
├── config.example.toml
├── README.md                          # brief: what it is, installation, link to docs/
├── docs/                              # this documentation
├── scripts/
│   ├── install.sh
│   ├── uninstall.sh
│   ├── fleurs_to_corpus.py
│   └── wolnelektury_to_corpus.py
├── systemd/
│   ├── local-stt.service
│   └── local-stt-whisper.service
├── src/local_stt/
│   ├── __init__.py                    # __version__
│   ├── __main__.py                    # python -m local_stt → cli.main
│   ├── cli.py                         # argparse, subcommands, IPC client
│   ├── app.py                         # component wiring (composition root), start/stop
│   ├── config.py                      # dataclasses, TOML loading, validation, whisper-server.env generation
│   ├── interfaces.py                  # Protocol: SttEngine, Injector, HotkeyBackend, AudioSource; data types
│   ├── events.py                      # event dataclasses
│   ├── controller.py                  # state machine (pure logic + effects through interfaces)
│   ├── pipeline.py                    # PipelineWorker, Job, generations, retry, prompt context
│   ├── engine_monitor.py
│   ├── audio/
│   │   ├── capture.py                 # AudioCapture (sounddevice), fallback resampling
│   │   ├── consumer.py                # audio-consumer thread: frames → Recorder/Segmenter, flush/discard commands
│   │   ├── file_source.py             # FileAudioSource – WAV in real time (e2e tests, bench --soak)
│   │   ├── recorder.py                # PTT
│   │   ├── vad.py                     # SileroVad (onnxruntime)
│   │   ├── segmenter.py               # state machine with hysteresis
│   │   └── wav.py                     # float32 ↔ WAV s16 in memory
│   ├── stt/
│   │   ├── __init__.py                # engine registry
│   │   ├── whisper_server.py          # WhisperServerEngine (http.client, multipart)
│   │   └── fake.py                    # FakeEngine
│   ├── text/
│   │   ├── processor.py               # TextProcessor (steps 1–7)
│   │   └── filters.py                 # hallucinations, repetitions, no_speech
│   ├── inject/
│   │   ├── auto.py
│   │   ├── clipboard.py               # ClipboardOwner + ClipboardPasteInjector
│   │   ├── xdotool.py
│   │   └── x11util.py                 # active window, WM_CLASS, waiting for modifiers, XTest
│   ├── hotkeys/
│   │   ├── spec.py                    # "Shift+Control_R" parser
│   │   └── x11.py                     # X11GrabHotkeys
│   ├── ipc.py                         # JSON Lines server and client, SO_PEERCRED
│   ├── feedback.py                    # sounds (WAV generation, pw-play) and notify-send
│   ├── sdnotify.py                    # READY=1 / STATUS=
│   ├── logging_setup.py               # TRACE, journald format
│   ├── doctor.py
│   ├── models.py                      # list/pull/verify
│   ├── models.sha256                  # pinned model checksums (package data, sha256sum format)
│   └── bench/
│       ├── corpus.py                  # record-corpus
│       ├── prompts_pl.txt             # corpus A sentences (package data)
│       ├── long_pl.txt                # continuous-recording text, CC BY-SA 4.0 (+ .ATTRIBUTION.md)
│       ├── runner.py                  # matrix, temporary server, soak
│       ├── wer.py
│       └── report.py
└── tests/
    ├── unit/
    ├── integration/
    └── fixtures/                      # short test WAVs (synthetic + 3 speech recordings), verbose_json responses
```

## 2.4 Sequence — PTT

```text
User        Hotkeys       Controller          Capture/Recorder     Pipeline            whisper-server    Injector
 │ ▼Ctrl_R    │               │                      │                  │                     │              │
 │──────────► │ PttPressed ─► │ engine READY?        │                  │                     │              │
 │            │               │── open()+start ────► │                  │                     │              │
 │            │               │ ◄─ first frame ───── │  (start sound)   │                     │              │
 │  speaks…   │               │                      │ frames…          │                     │              │
 │ ▲Ctrl_R    │               │                      │                  │                     │              │
 │──────────► │ PttReleased ► │── close()+finish ──► │                  │                     │              │
 │            │               │ ◄─ clip + ids ────── │ (drain → end)    │                     │              │
 │            │               │── submit(Job) ─────────────────────────►│                     │              │
 │            │               │ (stop sound), mode=IDLE                 │ silence gate/VAD    │              │
 │            │               │                      │                  │── POST /inference ─►│              │
 │            │               │                      │                  │ ◄── verbose_json ── │              │
 │            │               │                      │                  │ TextProcessor       │              │
 │            │               │                      │                  │── inject(text, cancel=token) ─────►│
 │            │               │ ◄──────────── JobFinished(timings) ──── │                     │   Ctrl+V     │
```

The controller instructs the audio consumer to start and finish recording. `RecordingFinished` contains the `AudioClip` and the recording, stream, and operation identifiers; it is created after the final frames have been processed. The complete protocol is described in [04](04-state-machine.md) §4.2–4.3.

## 2.5 Sequence — continuous

```text
Capture ──32 ms frames──► Segmenter(Silero) ──SegmentReady(seq=1)──► Controller ──submit──► Pipeline ─► STT ─► inject
   │                          │                                                              ▲
   │ (stream remains open     ├──SegmentReady(seq=2)──► Controller ──submit──► (queue) ─────────┘  order = seq
   │  throughout; transcription│
   │  does not block capture) └── Toggle: close() → flush() → SegmentReady(seq=n, cut=flush) → FlushDone(purpose=stop)
```

## 2.6 Key types (in `interfaces.py`)

```python
@dataclass(frozen=True)
class AudioClip:            # PTT
    samples: np.ndarray     # float32 mono 16 kHz (excluding the start-sound window)
    sample_rate: int        # always 16000
    duration_s: float
    started_at: float       # time.monotonic()
    ended_at: float          # release/limit time, not frame-queue drain time

@dataclass(frozen=True)
class Job:
    id: int
    source: Literal["ptt", "continuous"]
    audio: np.ndarray
    ended_at: float          # end of utterance – start of latency measurement
    generation: int
    session_id: int | None
    seq: int | None
    cut: Literal["release", "max_duration", "silence", "max_length", "flush"]
```

`AudioSegment` is described in [05](05-audio-and-vad.md), `Transcript` in [06](06-stt-engine.md), and `TextContext` and `InjectResult` in [08](08-text-injection.md).

## 2.7 Extension points

| Change | What we add | What remains unchanged |
|---|---|---|
| another STT engine | `SttEngine` class in `stt/` + registry entry | controller, pipeline, audio, inject |
| Wayland | `HotkeyBackend` (e.g. evdev / GlobalShortcuts portal) + `Injector` (wl-copy + ydotool) | everything else |
| another audio source (e.g. file, local network) | `AudioSource` | segmenter and downstream components |
| LLM post-processing | a gated step in `TextProcessor`, a local process like whisper-server | everything else |
| tray / GUI | a separate IPC client process (`subscribe`) | daemon |
