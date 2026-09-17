# 02. Architektura

## 2.1 Procesy

```text
┌──────────────────────── sesja użytkownika (systemd --user, graphical-session.target) ─────────────────────────┐
│                                                                                                             │
│  ┌──────────── local-stt.service (Python) ────────────┐        HTTP 127.0.0.1:8178     ┌─ local-stt-whisper ─┐ │
│  │                                                    │  POST /inference (WAV)       │   .service          │ │
│  │  HotkeyListener ─┐                                 │ ───────────────────────────► │  whisper-server     │ │
│  │  IpcServer ──────┼──► Controller ──► PipelineWorker│ ◄─────────────────────────── │  (whisper.cpp,      │ │
│  │  AudioCapture ───┘      ▲   │          │           │     verbose_json             │   model w RAM)      │ │
│  │   └► Recorder/Segmenter─┘   │          ├► TextProcessor                           └─────────────────────┘ │
│  │        (Silero VAD, ONNX)   │          └► Injector ──► X11: CLIPBOARD + XTest / xdotool                  │
│  │  EngineMonitor ─────────────┘                                                                            │
│  └─────────────────────────────────────────────────────┘                                                  │
│        ▲ Unix socket $XDG_RUNTIME_DIR/local-stt/control.sock                                               │
│        └── local-stt CLI (status / toggle / cancel / reload / ptt)                                          │
└─────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
          ▲ mikrofon: PortAudio → ALSA → pipewire-alsa → PipeWire         ▲ X server (:1) — mutter, aplikacje
```

## 2.2 Wątki w procesie daemona

| Wątek | Właściciel danych | Komunikacja | Krytyczny* |
|---|---|---|---|
| `main` | — | uruchamia komponenty, obsługuje sygnały (`signal.set_wakeup_fd`), po starcie czeka na `Controller` | tak |
| `controller` | **cały `DaemonState`** | czyta `events: queue.Queue`; wywołuje metody komponentów | tak |
| `hotkeys` | połączenie X11 #1 (grab) | → `events`; polecenia (regrab) przez własną kolejkę + wake-pipe | tak |
| PortAudio callback | bufor ramek | → `SimpleQueue` | (wątek biblioteki) |
| `audio-consumer` | Recorder / Segmenter / SileroVad | ramki z `SimpleQueue`, polecenia (`flush`, `discard`) z kolejki poleceń; → `events` (`RecordingStarted`, `SpeechStarted/Ended`, `SegmentReady`, `FlushDone`, `RecordingLimitReached`, `AudioError`) | tak |
| `pipeline` | kolejka zadań, bramka ciszy / przycinanie VAD PTT (własna instancja SileroVad), połączenie X11 #2 (XTest), kontekst promptu | `jobs: queue.Queue`; → `events` (`JobStarted/Finished/Discarded/Failed`, `EngineStateChanged`) | tak |
| `clipboard-owner` | połączenie X11 #3 (okno 1×1, selekcje) | żądania z `pipeline` przez kolejkę + `Future` | tak |
| `engine-monitor` | — | → `events` (`EngineStateChanged`) | nie |
| `timers` (`threading.Timer`) | — | → `events` (`ReconnectTick`, `CaptureOpenDue`) | nie |
| `server-restart` (krótkotrwały, tylko przy reload ⟳) | — | `systemctl --user restart local-stt-whisper` → `events` (`ServerRestartDone`) | nie |
| `ipc-server` (+ wątki per połączenie) | — | → `events` z `Future` na odpowiedź; subskrypcje dostają kopie statusu | nie |

\* Wyjątek w wątku krytycznym kończy proces kodem 1, a systemd go restartuje ([12](12-logi-prywatnosc-bledy.md), E14).

Zasady:

- Stan zmienia wyłącznie `controller`. Inne wątki przesyłają zdarzenia.
- Każdy wątek używający X11 ma **własne** połączenie (`Xlib.display.Display`), bo python-xlib nie jest thread-safe.
- Komponenty nie importują się nawzajem poza interfejsami z `local_stt/interfaces.py`. Wszystko skleja `local_stt/app.py`.
- Wybór wątków zamiast `asyncio`: sounddevice, python-xlib i `http.client` są blokujące, a liczba wątków jest mała i stała. `asyncio` dodałby tylko warstwę `run_in_executor`.

## 2.3 Struktura repozytorium

```text
local-stt-daemon/
├── pyproject.toml
├── requirements.lock
├── config.example.toml
├── README.md                          # krótkie: co to jest, install, link do docs/
├── docs/                              # ta dokumentacja
├── bench/prompts_pl.txt               # zdania korpusu
├── scripts/
│   ├── install.sh
│   ├── uninstall.sh
│   ├── models.sha256
│   └── fleurs_to_corpus.py
├── systemd/
│   ├── local-stt.service
│   └── local-stt-whisper.service
├── src/local_stt/
│   ├── __init__.py                    # __version__
│   ├── __main__.py                    # python -m local_stt → cli.main
│   ├── cli.py                         # argparse, subkomendy, klient IPC
│   ├── app.py                         # składanie komponentów (composition root), start/stop
│   ├── config.py                      # dataclasses, ładowanie TOML, walidacja, generowanie whisper-server.env
│   ├── interfaces.py                  # Protocol: SttEngine, Injector, HotkeyBackend, AudioSource; typy danych
│   ├── events.py                      # dataclasses zdarzeń
│   ├── controller.py                  # maszyna stanów (czysta logika + efekty przez interfejsy)
│   ├── pipeline.py                    # PipelineWorker, Job, generacje, retry, kontekst promptu
│   ├── engine_monitor.py
│   ├── audio/
│   │   ├── capture.py                 # AudioCapture (sounddevice), resampling awaryjny
│   │   ├── consumer.py                # wątek audio-consumer: ramki → Recorder/Segmenter, polecenia flush/discard
│   │   ├── file_source.py             # FileAudioSource – WAV w tempie rzeczywistym (testy e2e, bench --soak)
│   │   ├── recorder.py                # PTT
│   │   ├── vad.py                     # SileroVad (onnxruntime)
│   │   ├── segmenter.py               # automat z histerezą
│   │   └── wav.py                     # float32 ↔ WAV s16 w pamięci
│   ├── stt/
│   │   ├── __init__.py                # rejestr silników
│   │   ├── whisper_server.py          # WhisperServerEngine (http.client, multipart)
│   │   └── fake.py                    # FakeEngine
│   ├── text/
│   │   ├── processor.py               # TextProcessor (kroki 1–7)
│   │   └── filters.py                 # halucynacje, powtórzenia, no_speech
│   ├── inject/
│   │   ├── auto.py
│   │   ├── clipboard.py               # ClipboardOwner + ClipboardPasteInjector
│   │   ├── xdotool.py
│   │   └── x11util.py                 # aktywne okno, WM_CLASS, czekanie na modyfikatory, XTest
│   ├── hotkeys/
│   │   ├── spec.py                    # parser "Shift+Control_R"
│   │   └── x11.py                     # X11GrabHotkeys
│   ├── ipc.py                         # serwer i klient JSON Lines, SO_PEERCRED
│   ├── feedback.py                    # dźwięki (generowanie WAV, pw-play) i notify-send
│   ├── sdnotify.py                    # READY=1 / STATUS=
│   ├── logging_setup.py               # TRACE, format journald
│   ├── doctor.py
│   ├── models.py                      # list/pull/verify
│   └── bench/
│       ├── corpus.py                  # record-corpus
│       ├── runner.py                  # macierz, tymczasowy serwer, soak
│       ├── wer.py
│       └── report.py
└── tests/
    ├── unit/
    ├── integration/
    └── fixtures/                      # krótkie WAV-y testowe (syntetyczne + 3 nagrania mowy), odpowiedzi verbose_json
```

## 2.4 Sekwencja — PTT

```text
User        Hotkeys       Controller          Capture/Recorder     Pipeline            whisper-server    Injector
 │ ▼Ctrl_R    │               │                      │                  │                     │              │
 │──────────► │ PttPressed ─► │ engine READY?        │                  │                     │              │
 │            │               │── open()+start ────► │                  │                     │              │
 │            │               │ ◄─ first frame ───── │  (dźwięk start)  │                     │              │
 │  mówi…     │               │                      │ ramki…           │                     │              │
 │ ▲Ctrl_R    │               │                      │                  │                     │              │
 │──────────► │ PttReleased ► │── end()+close() ───► │                  │                     │              │
 │            │               │ ◄─ AudioClip ─────── │ (close stream)   │                     │              │
 │            │               │── submit(Job) ─────────────────────────►│                     │              │
 │            │               │ (dźwięk stop), mode=IDLE                │ bramka ciszy/VAD    │              │
 │            │               │                      │                  │── POST /inference ─►│              │
 │            │               │                      │                  │ ◄── verbose_json ── │              │
 │            │               │                      │                  │ TextProcessor       │              │
 │            │               │                      │                  │── inject(text) ────────────────────►│
 │            │               │ ◄──────────── JobFinished(timings) ──── │                     │   Ctrl+V     │
```

## 2.5 Sekwencja — continuous

```text
Capture ──ramki 32 ms──► Segmenter(Silero) ──SegmentReady(seq=1)──► Controller ──submit──► Pipeline ─► STT ─► inject
   │                          │                                                              ▲
   │ (strumień otwarty cały   ├──SegmentReady(seq=2)──► Controller ──submit──► (kolejka) ─────┘  kolejność = seq
   │  czas; transkrypcja      │
   │  nie blokuje capture)    └── Toggle: close() → flush() → SegmentReady(seq=n, cut=flush) → FlushDone
```

## 2.6 Kluczowe typy (w `interfaces.py`)

```python
@dataclass(frozen=True)
class AudioClip:            # PTT
    samples: np.ndarray     # float32 mono 16 kHz (bez okna dźwięku startu)
    sample_rate: int        # zawsze 16000
    duration_s: float
    started_at: float       # time.monotonic()
    ended_at: float

@dataclass(frozen=True)
class Job:
    id: int
    source: Literal["ptt", "continuous"]
    audio: np.ndarray
    ended_at: float          # koniec wypowiedzi – start pomiaru latencji
    generation: int
    session_id: int | None
    seq: int | None
    cut: Literal["release", "max_duration", "silence", "max_length", "flush"]
```

`AudioSegment` opisuje [05](05-audio-i-vad.md), `Transcript` [06](06-silnik-stt.md), a `TextContext` i `InjectResult` [08](08-text-injection.md).

## 2.7 Punkty rozszerzeń

| Zmiana | Co dodajemy | Czego nie ruszamy |
|---|---|---|
| inny silnik STT | klasa `SttEngine` w `stt/` + wpis w rejestrze | controller, pipeline, audio, inject |
| Wayland | `HotkeyBackend` (np. evdev / portal GlobalShortcuts) + `Injector` (wl-copy + ydotool) | reszta |
| inne źródło audio (np. plik, sieć lokalna) | `AudioSource` | segmenter i dalej |
| LLM post-processing | krok w `TextProcessor` za flagą, lokalny proces jak whisper-server | reszta |
| tray / GUI | osobny proces-klient IPC (`subscribe`) | daemon |
