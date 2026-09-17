# 05. Audio capture i VAD

## 5.1 Format wewnętrzny

W całym daemonie audio ma jeden format: **`numpy.float32`, mono, 16 000 Hz, zakres [-1, 1]**, dzielone na **ramki po 512 próbek (32 ms)**. 512 próbek to dokładnie rozmiar okna, którego wymaga Silero VAD dla 16 kHz, więc ramka z mikrofonu jest od razu ramką VAD. Konwersja do WAV s16le następuje dopiero przy wysyłce do silnika.

## 5.2 `AudioCapture` (moduł `local_stt/audio/capture.py`)

Biblioteka: **`sounddevice`** (PortAudio 19.6, `libportaudio2`). Na Ubuntu 24.04 strumień idzie przez ALSA → `pipewire-alsa` → PipeWire.

Wybór uzasadnia [03](03-decyzje.md), ADR-005. W skrócie: callback z numpy, flagi overflow, brak podprocesów w ścieżce audio. Alternatywa `pw-record` przez pipe jest prostsza, ale nie daje statusu strumienia i wiąże nas z PipeWire.

```python
class AudioCapture:
    def open(self) -> None                   # rzuca AudioOpenError; ramki trafiają do self.frames (SimpleQueue)
    def close(self) -> None
    @property
    def is_open(self) -> bool
    @staticmethod
    def list_devices() -> list[DeviceInfo]    # pactl -f json list sources (bez .monitor)
```

### Wybór urządzenia i otwieranie

PortAudio 19.6 widzi urządzenia tylko przez ALSA i tylko z chwili `Pa_Initialize`. Urządzeń `hw:` nie da się otworzyć, bo trzyma je PipeWire, a źródła USB/BT podłączone później nie pojawiłyby się na liście. Dlatego **zawsze otwieramy PCM ALSA `pipewire`** (z `pipewire-alsa`), a konkretne źródło wybieramy po stronie PipeWire:

1. `audio.device = "default"` → nie ustawiamy nic. PipeWire podłącza strumień do domyślnego źródła, czyli tego wybranego w ustawieniach dźwięku GNOME, i sam przełącza się przy zmianie domyślnego urządzenia lub hot-plugu.
2. Inna wartość to **nazwa węzła PipeWire** (np. `alsa_input.pci-0000_00_1f.3.analog-stereo`, `bluez_input.…`). Przed każdym `open()` ustawiamy `os.environ["PIPEWIRE_NODE"] = <nazwa>`, bo plugin ALSA odczytuje tę zmienną przy otwarciu PCM. `local-stt devices` wypisuje dostępne nazwy przez `pactl -f json list sources` (bez źródeł `.monitor`). Nieistniejąca nazwa → PipeWire podłączy domyślne źródło, więc po otwarciu weryfikujemy przez `pactl -f json list source-outputs`, że nasz strumień trafił do właściwego węzła. Jeśli nie, logujemy WARNING `device <X> not found, using default`.
   - **Zweryfikowano** na maszynie referencyjnej (`pipewire-alsa` 1.0.5): `PIPEWIRE_NODE=alsa_input.pci-0000_00_1f.3.analog-stereo arecord -D pipewire …` tworzy source-output z `target.object` równym tej nazwie. Test z PortAudio (`sounddevice`) należy powtórzyć w zadaniu 0.4 ([15](15-plan-implementacji.md)).
3. `InputStream(device="pipewire", samplerate=16000, channels=1, dtype="float32", blocksize=512, latency="low", callback=...)`. PipeWire (zegar 48 kHz) resampluje transparentnie.
4. Jeśli PCM `pipewire` nie istnieje (brak `pipewire-alsa`), używamy `default` z WARNING. Jeśli 16 kHz zostanie odrzucone (`-9997 Invalid sample rate`), otwieramy strumień z `default_samplerate` i resamplujemy w procesie: `soxr.ResampleStream(in_rate, 16000, 1, dtype="float32")` + układanie ramek po 512 próbek.
5. `import sounddevice` (czyli `Pa_Initialize`) wykonujemy **raz przy starcie daemona**. Komunikaty ALSA pisane przez bibliotekę C na fd 2 wyciszamy przez `snd_lib_error_set_handler` (ctypes, `libasound.so.2`) handlerem przekazującym je na TRACE. Przekierować stderr się nie da, bo to ten sam fd, na który trafiają logi journald.

**Parametry stałe.** Projekt wstępny przewidywał konfigurowalne `sample_rate` i `channels`. Świadomie z tego rezygnujemy: Whisper i Silero wymagają 16 kHz mono, a konwersję zapewnia PipeWire lub `soxr`. Opcja w configu mogłaby tylko zepsuć pipeline.

### Callback

Callback działa w wątku PortAudio i **nie może blokować**:

```python
def _callback(indata, frames, time_info, status):
    if status.input_overflow:
        self._overflows += 1          # zliczane, logowane co 5 s przez wątek konsumenta
    self._q.put_nowait(indata[:, 0].copy())   # queue.SimpleQueue
```

Wątek konsumenta `audio-consumer` odbiera ramki z `SimpleQueue` i przekazuje je do aktywnego odbiorcy: `Recorder` w PTT lub `Segmenter` w continuous.

`finished_callback` strumienia albo wyjątek `sd.PortAudioError` → `AudioError(device_lost)` do Controllera.

### Kiedy strumień jest otwarty

| Tryb | Strumień |
|---|---|
| IDLE | **zamknięty** — GNOME nie pokazuje wskaźnika mikrofonu, a słuchawki Bluetooth nie przełączają się na profil HFP |
| PTT | otwierany na `PttPressed`, zamykany na `PttReleased` |
| CONTINUOUS | otwarty przez cały czas trwania trybu |

**Ucięty początek i przeciek dźwięku startu w PTT.** Otwarcie strumienia trwa zwykle 30–150 ms. Dźwięk `start` gramy dopiero po pierwszej ramce z mikrofonu (`RecordingStarted`), więc użytkownik zaczyna mówić, gdy nagrywanie już trwa. Dźwięk z głośników laptopa wpada jednak do wbudowanego mikrofonu. Dlatego Recorder **odrzuca próbki od `RecordingStarted` do końca dźwięku + 80 ms** (~210 ms). W continuous dźwięk `start` gra *przed* otwarciem strumienia ([04](04-maszyna-stanow.md) §4.3).

Opcja „strumień otwarty jeszcze N s po PTT” została odrzucona w v0.1–v0.3: komplikuje maszynę stanów, a zysk (~100 ms) jest mniejszy niż maskowanie dźwięku startu.

## 5.3 `Recorder` (PTT)

- Zbiera ramki do listy i przy `end()` skleja je przez `np.concatenate`. Odrzuca próbki z okna dźwięku startu (5.2).
- Limit `ptt.max_duration_s` (domyślnie 120): po przekroczeniu emituje `RecordingLimitReached`.
- `end()` zwraca `AudioClip` ([02](02-architektura.md) §2.6).

**Bramka ciszy i przycinanie działają w PipelineWorkerze**, nie w controllerze ([04](04-maszyna-stanow.md) §4.4):

- **v0.1 — bramka RMS.** Liczymy RMS w oknach 100 ms. Jeśli żadne okno nie przekracza `ptt.silence_rms_dbfs` (domyślnie -50 dBFS), zadanie kończy się `JobDiscarded(no_speech)` i dźwiękiem `cancel`. Liczymy w oknach, a nie z całego nagrania, bo 1 s mowy w 20 s ciszy nie może przepaść. Powód bramki: Whisper na samej ciszy halucynuje (np. „Napisy stworzone przez społeczność Amara.org”).
- **v0.2 — przycinanie VAD** (gdy `vad.enabled`). Nagranie przechodzi przez Silero offline:
  - brak ramki z `p ≥ start_threshold` → `JobDiscarded(no_speech)`,
  - w przeciwnym razie przycięcie ciszy na początku i końcu z zachowaniem `vad.speech_pad_ms`.

  Skraca to audio, więc przy `dynamic_audio_ctx` przyspiesza też enkoder. Worker używa **własnej instancji** `SileroVad`, bo sesja ONNX jest stanowa i nie może być współdzielona z audio-consumerem.

## 5.4 Silero VAD (moduł `local_stt/audio/vad.py`)

- Model: `silero_vad.onnx` z repozytorium `snakers4/silero-vad`, tag **v6.2.1**, plik `src/silero_vad/data/silero_vad.onnx` (2 327 524 B, licencja MIT). Pobierany przez `install.sh` do `~/.local/share/local-stt/models/`, suma SHA256 przypięta w `scripts/models.sha256`.
- Runtime: `onnxruntime` (CPU), **bez torch**. Pakiet PyPI `silero-vad` ciągnie torch, więc go nie używamy.
- Sesja: `SessionOptions.intra_op_num_threads = 1`, `inter_op_num_threads = 1`, żeby VAD nie konkurował z whisper.cpp o rdzenie.

Kontrakt modelu (16 kHz):

| Tensor | Kształt | Typ | Zawartość |
|---|---|---|---|
| wejście `input` | `[1, 576]` | float32 | 64 ostatnie próbki poprzedniej ramki (kontekst) + 512 nowych |
| wejście `state` | `[2, 1, 128]` | float32 | stan RNN; zera po `reset()` |
| wejście `sr` | skalar | int64 | `16000` |
| wyjście 0 | `[1, 1]` | float32 | prawdopodobieństwo mowy |
| wyjście 1 | `[2, 1, 128]` | float32 | nowy stan |

```python
class SileroVad:
    def reset(self) -> None                     # stan = 0, kontekst = 0
    def __call__(self, frame512: np.ndarray) -> float
```

Koszt to rząd 1 ms CPU na ramkę, czyli ~31 wywołań/s, co daje ok. 3% jednego rdzenia w continuous. Budżet N4 to 5% (zapas na resztę daemona). Jeśli pomiar go przekroczy, ramki o RMS < -60 dBFS pomijają Silero (p = 0, stan RNN jest resetowany po 1 s takiej ciszy). `bench --soak` weryfikuje N4.

## 5.5 `Segmenter` (continuous)

Dzieli strumień ramek na wypowiedzi z **histerezą**: mowa zaczyna się powyżej `start_threshold`, a kończy dopiero po `min_silence_ms` z prawdopodobieństwem poniżej `end_threshold`. Wartości pomiędzy progami nie zmieniają stanu. To eliminuje oscylację SPEECH/SILENCE przy słabym mikrofonie.

### Parametry (`[vad]` w configu)

| Parametr | Domyślnie | Znaczenie |
|---|---:|---|
| `start_threshold` | 0.50 | `p ≥` → ramka „mowa” przy wykrywaniu początku |
| `end_threshold` | 0.35 | `p <` → ramka „cisza” przy wykrywaniu końca |
| `min_speech_ms` | 250 | minimalny ciąg mowy, żeby uznać start (odsiewa stuknięcia, kaszel) |
| `min_silence_ms` | 700 | cisza kończąca wypowiedź (krótsza = szybciej, ale tnie w pół zdania) |
| `speech_pad_ms` | 300 | audio dołączane przed startem i po końcu wypowiedzi |
| `max_segment_s` | 15 | twardy limit długości fragmentu |
| `split_search_s` | 3 | okno na końcu fragmentu, w którym szukamy miejsca podziału |

### Automat

```text
            p ≥ start                       ciąg (p ≥ end) ≥ min_speech_ms
 SILENCE ─────────────► CANDIDATE ───────────────────────────────────────► SPEECH ──► emit SpeechStarted
    ▲                      │ p < end                                        │  ▲
    │                      ▼                                                │  │ p ≥ start: silence_ms = 0
    └───────────── (ramki wracają do bufora pre-roll)                        │  │
                                                                            ▼  │
                                              p < end: silence_ms += 32 ─► TRAILING
                                                                            │
                          silence_ms ≥ min_silence_ms: emit Segment, SpeechEnded ──► SILENCE
```

Reguły szczegółowe:

1. **Pre-roll.** W SILENCE trzymamy bufor cykliczny `speech_pad_ms` ostatnich ramek. Przy przejściu do SPEECH segment zaczyna się od zawartości tego bufora, dzięki czemu nie ginie początek pierwszej sylaby.
2. **CANDIDATE → SPEECH** wymaga, żeby przez `min_speech_ms` każda ramka miała `p ≥ end_threshold`. Jedna ramka poniżej cofa automat do SILENCE.
3. **SPEECH/TRAILING.** Ramka z `p < end_threshold` uruchamia licznik ciszy lub go zwiększa. Ramka z `p ≥ start_threshold` go zeruje. Ramka pomiędzy progami nie zmienia licznika (histereza; tak samo działa `VADIterator` Silero).
4. **Koniec wypowiedzi.** Segment zawiera audio do końca mowy plus `speech_pad_ms` ciszy z bufora TRAILING. Reszta ciszy jest odrzucana.
5. **Limit długości.** Gdy segment osiągnie `max_segment_s`:
   - szukamy w ostatnich `split_search_s` sekundach najdłuższego ciągu ramek z `p < end_threshold` (min. 96 ms) i tniemy w jego środku,
   - jeśli taki ciąg nie istnieje, tniemy w ramce o najniższym `p` z tego okna,
   - pierwsza część jest emitowana, druga zostaje jako początek nowego segmentu (stan SPEECH trwa).

   Tak ograniczamy cięcie w pół słowa.
6. **`flush()`** (wyłączenie continuous): jeśli stan to SPEECH/TRAILING i zebrano ≥ `min_speech_ms` mowy, emitujemy segment od razu.
7. **`reset()`**: czyści bufory i wywołuje `SileroVad.reset()`. Wywoływane na starcie sesji continuous.

Emitowany obiekt:

```python
@dataclass(frozen=True)
class AudioSegment:
    samples: np.ndarray        # float32, 16 kHz
    session_id: int
    seq: int                   # numer w sesji
    speech_ms: int             # czas mowy bez paddingu
    cut: Literal["silence", "max_length", "flush"]
```

Znaczenie `cut`:

- `max_length` informuje TextProcessor, że fragment kończy się w środku zdania. Usuwamy wtedy końcową kropkę, a w następnym fragmencie pierwszą literę zamieniamy na małą ([08](08-text-injection.md) §8.2).
- Odpowiednik w PTT to `cut="max_duration"` (nagranie przerwane limitem).

## 5.6 Obsługa błędów audio

PTT: v0.1 (błąd → odrzucenie nagrania). Continuous: v0.2.

| Sytuacja | Wykrycie | Reakcja |
|---|---|---|
| Brak urządzenia przy otwarciu | `AudioOpenError` | PTT: dźwięk `error` + powiadomienie z nazwą urządzenia; continuous: to samo, tryb się nie włącza |
| Strumień przerwany (np. restart PipeWire) | `finished_callback` bez `close()` / `PortAudioError` | PTT: nagranie odrzucone. Continuous: `flush()`, potem do 3 prób ponownego otwarcia co 1 s; po 3 porażkach tryb się wyłącza z powiadomieniem ([04](04-maszyna-stanow.md) §4.3). Odłączenie mikrofonu USB/BT zwykle **nie** przerywa strumienia — PipeWire przepina go na domyślne źródło (log INFO z nazwą nowego węzła) |
| Overflow | `status.input_overflow` | licznik, WARNING co 5 s z liczbą; przy > 20 overflow/min WARNING o przeciążeniu CPU |
| Cisza cyfrowa (mikrofon wyciszony w systemie) | RMS < -80 dBFS przez 5 s w continuous | jedno powiadomienie „Mikrofon wydaje się wyciszony” na sesję (poziom `errors`) |

## 5.7 Czego nie robimy

- Nie zapisujemy audio na dysk (wyjątek: `record-corpus` do benchmarku, na wyraźne polecenie).
- Nie stosujemy odszumiania ani AGC. PipeWire/WebRTC echo-cancel można włączyć systemowo, poza projektem. Opisuje to `doctor`, jeśli poziom sygnału jest bardzo niski.
