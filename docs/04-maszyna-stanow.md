# 04. Maszyna stanów i przepływ zdarzeń

## 4.1 Dlaczego nie diagram z projektu wstępnego

Diagram z `local-stt-daemon-design.md` ma dwie wady:

1. **Continuous gubi mowę.** Przejście `LISTENING → TRANSCRIBING → INJECT → LISTENING` oznacza, że w trakcie transkrypcji nikt nie słucha. Na i5-8365U transkrypcja 5 s mowy modelem `small` trwa sekundy, a użytkownik w tym czasie mówi dalej.
2. **Tryb i praca w tle są sklejone w jeden stan.** To, co robi użytkownik (trzyma PTT, dyktuje), jest niezależne od tego, co robi silnik (kolejka transkrypcji).

Dlatego stan daemona składa się z **trzech niezależnych składowych**, a zmienia je wyłącznie wątek `controller`:

```text
DaemonState = (mode, pipeline, engine)

mode     ∈ { IDLE, PTT_RECORDING, CONTINUOUS(speech: bool, reconnecting: bool) }
pipeline = { queued_jobs: int, queued_audio_s: float, busy: bool, paused: bool, generation: int }
engine   ∈ { STARTING, READY, DOWN }
```

Dodatkowo controller przechowuje `pending_reload: Config | None` (4.6).

## 4.2 Zdarzenia

Wszystkie źródła wysyłają zdarzenia do jednej kolejki `controller.events` (`queue.Queue`). Controller przetwarza je sekwencyjnie, więc nie potrzebuje blokad na własnym stanie.

| Zdarzenie | Źródło | Dane |
|---|---|---|
| `PttPressed` | HotkeyListener / IPC `ptt start` | `reply: Future \| None` |
| `PttReleased` | HotkeyListener / IPC `ptt stop` | `reply` |
| `PttCancelKey` | HotkeyListener (`hotkeys.ptt_cancel_key` przy trzymanym PTT) | — |
| `ContinuousToggle` | HotkeyListener / IPC `toggle` | `reply` |
| `CancelRequested` | IPC `cancel` | `reply` |
| `RecordingStarted` | audio-consumer (pierwsza ramka po otwarciu strumienia) | — |
| `RecordingLimitReached` | audio-consumer (Recorder, PTT) | — |
| `SpeechStarted` / `SpeechEnded` | audio-consumer (Segmenter) | — |
| `SegmentReady` | audio-consumer (Segmenter) | `AudioSegment` |
| `FlushDone` | audio-consumer (po poleceniu `flush`) | — |
| `AudioError` | AudioCapture / audio-consumer | `kind ∈ {open_failed, device_lost}`, opis |
| `ReconnectTick` | timer controllera | numer próby |
| `CaptureOpenDue` | timer controllera (150 ms po starcie continuous) | `session_id` |
| `ServerRestartDone` | wątek pomocniczy wykonujący `systemctl --user restart` (4.6) | kod wyjścia |
| `JobStarted` / `JobFinished` / `JobDiscarded` / `JobFailed` | PipelineWorker | `job_id`, timingi, powód (`no_speech`, `filtered`, `cancelled`) lub błąd |
| `EngineStateChanged` | EngineMonitor / PipelineWorker | `READY` / `STARTING` / `DOWN` |
| `ReloadRequested` | IPC `reload`, `SIGHUP` | `reply` |
| `ShutdownRequested` | `SIGTERM`, `SIGINT` | — |
| `X11ConnectionLost` | HotkeyListener / ClipboardOwner | — |

**Odpowiedzi IPC.** Zdarzenie z `reply` dostaje zawsze odpowiedź:

- `{"ok": true}` gdy zostało przyjęte,
- `{"ok": false, "error": "<kod>", "message": "…"}` gdy zostało odrzucone lub zignorowane w bieżącym trybie.

Kody błędów:

- `invalid_in_mode` — np. `toggle` w trakcie PTT, `ptt start` w trakcie continuous,
- `engine_down`,
- `engine_starting`,
- `vad_disabled`,
- `audio_error`.

CLI mapuje każdą odmowę na kod wyjścia 4.

## 4.3 Tabela przejść — `mode`

Kombinacje (stan, zdarzenie), których **nie ma** w tabeli:

- zdarzenia z `reply` → odmowa `invalid_in_mode`,
- zdarzenia bez `reply` → ignorowane, log DEBUG `ignored <event> in <mode>`.

Dźwięki są opisane nazwami z [10](10-cli-ipc-status.md) §10.6 (jedyne źródło prawdy o dźwiękach).

### IDLE

| Zdarzenie | Warunek | Akcje | Nowy stan |
|---|---|---|---|
| `PttPressed` | `engine == READY` | `recorder.begin()`, `capture.open()` | PTT_RECORDING |
| `PttPressed` | `engine != READY` | dźwięk `error`, powiadomienie „Silnik STT niedostępny”, odmowa `engine_down`/`engine_starting` | IDLE |
| `ContinuousToggle` | `engine == READY` i `vad.enabled` | dźwięk `start`, `session_id += 1`, `segmenter.reset()`, timer `CaptureOpenDue(session_id)` za 150 ms (po dźwięku, żeby nie trafił do mikrofonu; controller nie blokuje). Odpowiedź IPC wysyłana dopiero po próbie otwarcia | CONTINUOUS |
| `ContinuousToggle` | `engine != READY` / `!vad.enabled` | dźwięk `error`, odmowa `engine_*` / `vad_disabled` | IDLE |
| `CancelRequested` | — | `pipeline.cancel_all()`; dźwięk `cancel`, jeśli coś zostało odrzucone | IDLE |
| `JobDiscarded(no_speech)` | źródło PTT | dźwięk `cancel` | IDLE |

### PTT_RECORDING

| Zdarzenie | Warunek | Akcje | Nowy stan |
|---|---|---|---|
| `RecordingStarted` | — | dźwięk `start`; Recorder pomija próbki do końca dźwięku + 80 ms (5.3) | PTT_RECORDING |
| `PttReleased` | długość ≥ `ptt.min_duration_ms` | `clip = recorder.end()`, `capture.close()`, `pipeline.submit(Job(ptt, clip, cut="release"))`, dźwięk `stop` | IDLE |
| `PttReleased` | długość < `ptt.min_duration_ms` | `recorder.end()` i odrzucenie, `capture.close()`, **bez dźwięku** (przypadkowe tapnięcie prawego Ctrl nie może hałasować), DEBUG `ptt too short` | IDLE |
| `RecordingLimitReached` | — | jak `PttReleased` (z `cut="max_duration"`) + powiadomienie „Osiągnięto limit nagrania” | IDLE* |
| `PttCancelKey` / `CancelRequested` | — | `recorder.end()` i odrzucenie, `capture.close()`, dźwięk `cancel` | IDLE |
| `AudioError` | — | `recorder.end()` i odrzucenie, `capture.close()`, dźwięk `error`, powiadomienie | IDLE |

\* Klawisz jest nadal wciśnięty. Jego `PttReleased` przyjdzie w stanie IDLE i zostanie zignorowany.

Bramka ciszy i przycinanie VAD **nie** działają w controllerze. Wykonuje je PipelineWorker (4.4), bo przycinanie 120 s nagrania to kilka tysięcy wywołań ONNX, a controller nie może blokować.

### CONTINUOUS

| Zdarzenie | Warunek | Akcje | Nowy stan |
|---|---|---|---|
| `CaptureOpenDue(sid)` | `sid == session_id` i nie `stopping` | `capture.open()`: sukces → odpowiedź `ok`; porażka → wiersz `AudioError(open_failed)` | CONTINUOUS |
| `SpeechStarted` / `SpeechEnded` | — | ustaw `speech` | CONTINUOUS |
| `SegmentReady` | — | `pipeline.submit(Job(continuous, segment, session_id, seq, cut))`; jeśli `queued_audio_s > continuous.max_backlog_s` → wiersz *Backlog* | CONTINUOUS |
| `ContinuousToggle` | — | wiersz *Stop(flush)*, dźwięk `stop` | → IDLE po `FlushDone` |
| *Backlog* (wewnętrzny) | — | wiersz *Stop(flush)*, dźwięki `stop` + `error`, powiadomienie „Transkrypcja nie nadąża — dyktowanie zatrzymane”; kolejka jest dokańczana | → IDLE po `FlushDone` |
| `CancelRequested` | — | `capture.close()`, `audio_consumer.discard()`, `pipeline.cancel_all()`, dźwięk `cancel` | IDLE |
| `EngineStateChanged(DOWN)` | — | wiersz *Stop(flush)*, dźwięki `stop` + `error`; segmenty czekają w wstrzymanej kolejce (4.5); powiadomienie „Silnik STT przestał działać — dyktowanie zatrzymane” | → IDLE po `FlushDone` |
| `AudioError(device_lost)` | `reconnecting == false` | `capture.close()`, `audio_consumer.flush()` (emituje ostatni segment), `reconnecting = true`, timer `ReconnectTick(1)` za 1 s | CONTINUOUS(reconnecting) |
| `ReconnectTick(n)` | — | próba `capture.open()`: sukces → `reconnecting = false`, `segmenter.reset()`, INFO; porażka i `n < 3` → timer `ReconnectTick(n+1)` za 1 s; porażka i `n == 3` → wiersz *Stop(flush)*, powiadomienie „Mikrofon niedostępny — dyktowanie zatrzymane”, dźwięki `stop` + `error` | CONTINUOUS / IDLE |
| `AudioError(open_failed)` | przy starcie trybu (`CaptureOpenDue`) | dźwięk `error`, powiadomienie, odmowa `audio_error` | IDLE |
| `JobFailed` | — | powiadomienie (4.4); **tryb trwa** | CONTINUOUS |

*Stop(flush)* działa tak:

1. controller wywołuje `capture.close()` (jeśli strumień jest otwarty). `stream.stop()` w PortAudio czeka na zakończenie callbacków, więc po powrocie do kolejki ramek nie trafi już nic nowego,
2. controller wysyła polecenie `audio_consumer.flush()`,
3. wątek audio-consumer przetwarza ramki pozostałe w kolejce, wywołuje `segmenter.flush()`, emituje ewentualny `SegmentReady(cut="flush")`, a potem `FlushDone`,
4. controller po `SegmentReady` zleca zadanie jak zwykle, a po `FlushDone` przechodzi do IDLE.

Między krokami 1 i 4 kolejne `ContinuousToggle` jest ignorowane (flaga `stopping`).

### Dowolny stan

| Zdarzenie | Akcje |
|---|---|
| `ShutdownRequested` | zamknij capture, `pipeline.cancel_all()`, ungrab, zamknij gniazdo IPC → wyjście 0 |
| `X11ConnectionLost` | zamknij capture i gniazdo IPC, **bez** operacji X11 → WARNING, wyjście 0 (sesja się kończy; systemd nie restartuje, a `PartOf` zatrzymuje unit) |
| `ReloadRequested` | 4.6 |
| `EngineStateChanged(s)` | `engine = s`; `READY` → `pipeline.paused = false`; w CONTINUOUS dla `DOWN` dodatkowo wiersz z tabeli CONTINUOUS |
| `JobStarted` / `JobFinished` | aktualizacja `pipeline` i statystyk statusu; `JobFinished` z `left_in_clipboard` → powiadomienie z [08](08-text-injection.md) §8.5 (wysyła je controller na podstawie `InjectResult`; injector sam nie powiadamia) |
| `JobFailed` | aktualizacja statystyk + powiadomienie zbiorcze (4.4), niezależnie od trybu |
| `JobDiscarded` | aktualizacja statystyk; dźwięk `cancel` tylko w IDLE i tylko dla `no_speech` z PTT (tabela IDLE) |
| `CaptureOpenDue`, `ReconnectTick`, `RecordingStarted`, `SegmentReady`, `FlushDone` spoza właściwego trybu/sesji | ignorowane (spóźnione timery i zdarzenia po zmianie trybu) |
| `ServerRestartDone` | 4.6 |

## 4.4 Pipeline (kolejka zadań)

```text
Controller ──submit(Job)──► jobs ──► PipelineWorker (1 wątek)
                                        │
                                        ├─ [PTT] bramka ciszy / przycinanie VAD ──► brak mowy → JobDiscarded(no_speech)
                                        ├─ generation check
                                        ├─ engine.transcribe(audio, prompt)
                                        ├─ generation check
                                        ├─ processor.process(transcript, ctx) ──► None → JobDiscarded(filtered)
                                        ├─ generation check
                                        └─ injector.inject(text) ──► JobFinished
```

- **Jeden wątek roboczy.** `whisper-server` przetwarza jedno żądanie naraz, a jeden worker gwarantuje, że tekst jest wpisywany w kolejności nagrania.
- **Generacje zamiast przerywania.** `cancel_all()` zwiększa `generation` i opróżnia kolejkę. Każdy `Job` pamięta generację z chwili utworzenia, a worker porzuca nieaktualne zadanie przed każdym krokiem (`JobDiscarded(cancelled)`). Trwającego żądania HTTP nie przerywamy: jego wynik zostaje odrzucony.
- **PTT w trakcie pracy workera** jest dozwolone. Nowe zadanie ustawia się w kolejce.
- **Kontekst continuous.** Worker trzyma `last_text[session_id]`. Ostatnie maks. 200 znaków trafia do silnika jako część `prompt` ([06](06-silnik-stt.md) §6.6).
- **Błąd transkrypcji:**
  - Błąd **połączenia** (serwer nie odpowiada) → worker emituje `EngineStateChanged(DOWN)`, **wstrzymuje się** (`paused = true`) i odkłada zadanie z powrotem na początek kolejki (4.5).
  - HTTP 5xx / timeout → jedno ponowienie po 1 s; potem `JobFailed`.
  - HTTP 4xx → `JobFailed` od razu.
  - `JobFailed` → powiadomienie „Nie udało się przetranskrybować fragmentu (N s)”. Kilka porażek w ciągu 10 s łączymy w jedno powiadomienie „N fragmentów nie przetranskrybowano”. Audio jest usuwane z pamięci.
- **Pusty wynik** (brak mowy, halucynacja odfiltrowana) nie jest błędem: `JobDiscarded`, log DEBUG.

## 4.5 Silnik (`engine`)

`EngineMonitor` odpytuje `GET <request_path>/health` ([06](06-silnik-stt.md) §6.5):

- co 500 ms w `STARTING` i `DOWN`,
- co 10 s w `READY`.

Przejścia:

- `200 ok` → `READY`,
- `503 loading model` → `STARTING`,
- błąd połączenia → `STARTING`, jeśli od startu daemona (lub od zleconego restartu serwera) minęło mniej niż `stt.startup_timeout_s`, w przeciwnym razie `DOWN`.

**Wstrzymana kolejka.** Po `EngineStateChanged(DOWN)` (także zgłoszonym przez workera) pipeline ma `paused = true`. Zadania czekają, bo systemd restartuje serwer (`RestartSec=2` + ładowanie modelu), co trwa kilka sekund.

- `READY` → `paused = false`, worker wznawia pracę od zadania, które się nie udało.
- Jeśli w ciągu `stt.startup_timeout_s` od przejścia w DOWN silnik nie wróci, wszystkie zadania w kolejce dostają `JobFailed` i wyświetlamy **jedno** zbiorcze powiadomienie.

Daemon nie uruchamia serwera sam przy awarii (robi to `Restart=on-failure`). Jedynym wyjątkiem jest zlecony restart przy reload (4.6).

## 4.6 Reload

`local-stt reload` lub `systemctl --user reload local-stt` (`SIGHUP`):

1. Wczytaj i zwaliduj nowy config. Jeśli jest błędny, stary zostaje, a odpowiedź to `{"ok": false, "errors": [...]}`.
2. Policz różnicę sekcji. Klucze dzielą się na trzy grupy:

| Grupa | Klucze | Kiedy stosowane |
|---|---|---|
| **na żywo** | `logging.*`, `text.*`, `injection.*`, `feedback.*`, `ptt.*`, `continuous.*`, `stt.vocabulary_prompt`, `stt.continuous_context`, `stt.dynamic_audio_ctx`, `stt.audio_ctx_margin`, `stt.no_speech_threshold`, `stt.logprob_threshold`, `stt.startup_timeout_s`, `stt.request_timeout_max_s` | natychmiast |
| **przy IDLE** | `audio.*`, `vad.*`, `hotkeys.*` | natychmiast, jeśli `mode == IDLE`; inaczej zapisane w `pending_reload` i stosowane przy najbliższym przejściu do IDLE (bez przerywania nagrania) |
| **restart serwera** ⟳ | `stt.engine`, `stt.model`, `stt.models_dir`, `stt.language`, `stt.threads`, `stt.beam_size`, `stt.port`, `stt.extra_server_args` | patrz niżej |

3. **Restart serwera** (⟳):
   - daemon generuje nowy `whisper-server.env` ([09](09-konfiguracja.md) §9.4),
   - czeka, aż `mode == IDLE` i kolejka będzie pusta (albo `engine == DOWN` — wtedy restart jest i tak potrzebny),
   - wstrzymuje pipeline i uruchamia `systemctl --user restart local-stt-whisper.service` w wątku pomocniczym (controller nie blokuje), który zgłasza `ServerRestartDone`,
   - przełącza klienta na nowy `port`, ustawia `engine = STARTING` i wznawia pipeline po `READY`; kod ≠ 0 lub brak `READY` w `startup_timeout_s` → błąd E16 ([12](12-logi-prywatnosc-bledy.md)).
   - `stt.models_dir` wpływa też na ścieżkę modelu VAD, która jest stosowana jak grupa „przy IDLE”.

   Nie używamy `POST /load` — uzasadnienie w [06](06-silnik-stt.md) §6.5.
4. Odpowiedź: `{"ok": true, "applied": [...], "deferred": [...], "server_restart": true|false}`.

## 4.7 Status widziany z zewnątrz

`local-stt status` składa jeden czytelny stan (priorytet od góry):

| Wyświetlany stan | Warunek |
|---|---|
| `ERROR: engine down` | `engine == DOWN` |
| `STARTING` | `engine == STARTING` |
| `RECORDING` | `mode == PTT_RECORDING` |
| `LISTENING (reconnecting)` | `mode == CONTINUOUS` i `reconnecting` |
| `LISTENING (speech)` / `LISTENING` | `mode == CONTINUOUS` |
| `TRANSCRIBING (n queued)` | `mode == IDLE` i (`busy` lub `queued_jobs > 0`) |
| `IDLE` | pozostałe |

W trybie `LISTENING` do statusu dopisujemy `, transcribing n`. Format JSON opisuje [10](10-cli-ipc-status.md) §10.4.
