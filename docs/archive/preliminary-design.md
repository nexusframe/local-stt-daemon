# Local Offline Voice-to-Text Daemon for Ubuntu

> **Archiwum — dokument nieaktualny.** To wstępny projekt, zastąpiony przez specyfikację w [`docs/`](../README.md). Zachowany wyłącznie jako kontekst dla odwołań „preliminary design” w specyfikacji. Nie implementować na jego podstawie.

## 1. Cel projektu

Projekt ma zapewnić lokalny, działający całkowicie offline system **speech-to-text (STT)** dla Ubuntu, zoptymalizowany pod laptop bez dedykowanego GPU.

Docelowy sprzęt referencyjny:

- CPU: Intel Core i5-8365U, 4C/8T
- RAM: 16 GB
- GPU: brak dedykowanego GPU
- OS: Ubuntu
- język główny: polski

System ma działać jako lokalny daemon i umożliwiać wpisywanie rozpoznanego tekstu do aktualnie aktywnego okna.

---

## 2. Tryby pracy

### 2.1 Push-to-talk

Użytkownik:

1. naciska i przytrzymuje hotkey;
2. system rozpoczyna nagrywanie;
3. użytkownik mówi;
4. puszcza hotkey;
5. nagranie jest transkrybowane;
6. wynik zostaje wpisany do aktualnie aktywnego okna.

Schemat:

```text
HOTKEY DOWN
    ↓
RECORDING
    ↓
HOTKEY UP
    ↓
TRANSCRIPTION
    ↓
TEXT INJECTION
```

Ten tryb ma być prosty i przewidywalny. Opóźnienie po puszczeniu klawisza jest akceptowalne.

### 2.2 Continuous dictation

Użytkownik naciska hotkey, aby rozpocząć nasłuchiwanie.

System:

1. stale pobiera audio z mikrofonu;
2. wykrywa mowę za pomocą VAD;
3. dzieli mowę na fragmenty;
4. transkrybuje fragmenty;
5. wstawia zatwierdzony tekst do aktywnego okna;
6. kontynuuje nasłuchiwanie.

Drugi hotkey kończy tryb.

Schemat:

```text
HOTKEY
  ↓
LISTENING
  ↓
VAD
  ├── silence → wait
  └── speech
        ↓
      AUDIO CHUNK
        ↓
      WHISPER
        ↓
      FINAL TEXT
        ↓
      TEXT INJECTION
        ↓
      LISTENING
```

---

## 3. Główne wymagania

### Functional requirements

- [ ] działanie całkowicie offline;
- [ ] brak wymogu GPU;
- [ ] rozpoznawanie języka polskiego;
- [ ] tryb push-to-talk;
- [ ] tryb continuous dictation;
- [ ] globalne hotkeye;
- [ ] automatyczne wykrywanie mowy;
- [ ] wpisywanie tekstu do aktywnego okna;
- [ ] możliwość uruchomienia jako daemon;
- [ ] start automatycznie wraz z sesją użytkownika;
- [ ] czytelny status systemu;
- [ ] możliwość ręcznego zatrzymania nasłuchiwania.

### Non-functional requirements

- niskie zużycie RAM;
- rozsądne obciążenie CPU;
- brak wysyłania audio poza komputer;
- możliwość działania bez połączenia z Internetem;
- możliwość wymiany modelu STT bez przebudowy całego systemu;
- komponenty powinny być możliwie niezależne;
- konfiguracja powinna znajdować się poza kodem.

---

## 4. Architektura

Proponowana architektura:

```text
                         ┌─────────────────┐
                         │   Global Hotkey │
                         └────────┬────────┘
                                  │
                                  ▼
┌──────────┐      ┌────────────────────────┐
│ Microphone├─────►│      STT Daemon       │
└──────────┘      │                        │
                  │  ┌─────┐   ┌────────┐ │
                  │  │ VAD │──►│ Buffer │ │
                  │  └─────┘   └───┬────┘ │
                  │                 │      │
                  │                 ▼      │
                  │          ┌───────────┐ │
                  │          │ Whisper   │ │
                  │          │ Engine    │ │
                  │          └─────┬─────┘ │
                  │                │       │
                  │                ▼       │
                  │          ┌──────────┐  │
                  │          │ Text     │  │
                  │          │ Processor │  │
                  │          └────┬─────┘  │
                  └───────────────┼────────┘
                                  │
                                  ▼
                         ┌─────────────────┐
                         │ Text Injection  │
                         └────────┬────────┘
                                  │
                                  ▼
                         ┌─────────────────┐
                         │ Active Window   │
                         └─────────────────┘
```

---

## 5. Komponenty

### 5.1 Audio capture

Warstwa odpowiedzialna za pobieranie dźwięku z mikrofonu.

Preferowane środowisko:

- PipeWire;
- kompatybilność z PulseAudio;
- standardowe urządzenie wejściowe użytkownika.

Interfejs powinien umożliwiać:

- wybór urządzenia;
- ustawienie sample rate;
- ustawienie liczby kanałów;
- rozpoczęcie i zatrzymanie capture.

Docelowo należy unikać silnego związania aplikacji z jednym backendem audio.

---

### 5.2 Voice Activity Detection

VAD odpowiada za rozpoznanie, czy użytkownik mówi.

Jego zadania:

- wykrywanie początku mowy;
- wykrywanie końca mowy;
- ignorowanie ciszy;
- ograniczanie liczby wywołań silnika STT;
- dzielenie ciągłego audio na fragmenty.

VAD jest szczególnie istotny w trybie continuous.

Powinien mieć konfigurowalne parametry:

```text
speech_start_threshold
speech_end_threshold
minimum_speech_duration
minimum_silence_duration
maximum_chunk_duration
```

Należy preferować histerezę lub podobny mechanizm, aby uniknąć ciągłego przełączania:

```text
SPEECH → SILENCE → SPEECH → SILENCE
```

przy słabym lub zaszumionym mikrofonie.

---

## 6. Silnik STT

Podstawowym kandydatem jest **Whisper**, uruchamiany lokalnie przez `whisper.cpp`.

Powody:

- działa CPU-only;
- obsługuje język polski;
- działa offline;
- nie wymaga zewnętrznego API;
- modele można przechowywać lokalnie;
- whisper.cpp jest zoptymalizowany pod inference na CPU.

### Modele

Początkowo należy przetestować:

| Model | Priorytet | Cel |
|---|---:|---|
| `base` | wysoki | responsywność |
| `small` | wysoki | jakość |
| `tiny` | niski | fallback / testy |
| `medium` | niski | test jakości |

Na i5-8365U należy traktować `base` i `small` jako podstawowe warianty do benchmarku.

Nie należy zakładać z góry, że większy model będzie praktycznie użyteczny. Decyzję należy oprzeć na pomiarach:

- real-time factor;
- latency;
- CPU usage;
- jakość polskiej transkrypcji.

---

## 7. Streaming / quasi-streaming

Whisper nie powinien być traktowany jako klasyczny streaming ASR.

Docelowy system będzie stosował **quasi-streaming**:

```text
continuous audio
      ↓
rolling buffer
      ↓
VAD
      ↓
speech segment
      ↓
Whisper inference
      ↓
finalized text
      ↓
injection
```

W pierwszej wersji należy preferować **stabilne finalne fragmenty** zamiast agresywnego wyświetlania predykcji częściowych.

### Dlaczego?

Przykładowo model może kolejno przewidywać:

```text
"Dzisiaj chciałbym"
"Dzisiaj chciałbym pojechać"
"Dzisiaj chciałbym pojechać do Warszawy"
```

Jeżeli każdy wynik zostanie wpisany do aplikacji, powstanie:

```text
Dzisiaj chciałbym Dzisiaj chciałbym pojechać
Dzisiaj chciałbym pojechać do Warszawy
```

Dlatego wersja MVP powinna wpisywać wyłącznie **finalized segments**.

Późniejsza wersja może implementować:

- partial transcript;
- stabilizację tekstu;
- common-prefix detection;
- rewizję ostatniego fragmentu;
- aktualizację tekstu w aplikacjach obsługujących bardziej zaawansowaną integrację.

---

## 8. Text injection

Po zakończeniu transkrypcji tekst musi trafić do aktywnego okna.

Należy odseparować ten komponent od STT:

```text
STT
 ↓
Transcript
 ↓
Text Injector
```

Dzięki temu można później obsługiwać różne mechanizmy:

- X11;
- Wayland;
- clipboard + paste;
- `ydotool`;
- inne mechanizmy dostępne w danym środowisku.

### Ważne

Wayland ogranicza część klasycznych metod globalnej automatyzacji. Dlatego warstwa `Text Injector` powinna być abstrakcją, a nie bezpośrednim wywołaniem konkretnego narzędzia w kodzie STT.

---

## 9. Hotkeys

Proponowany interfejs:

```text
PTT:
Super + Space

Continuous:
Super + Shift + Space
```

Hotkeye powinny być konfigurowalne.

Minimalne akcje:

```text
PTT_DOWN
PTT_UP
CONTINUOUS_START
CONTINUOUS_STOP
```

Opcjonalnie:

```text
CANCEL
PAUSE
RESUME
```

---

## 10. State machine

Daemon powinien mieć jawny model stanów.

```text
                 ┌──────────────┐
                 │     IDLE     │
                 └──────┬───────┘
                        │
             PTT / Continuous
                        │
            ┌───────────┴───────────┐
            ▼                       ▼
     ┌──────────────┐       ┌──────────────┐
     │  RECORDING   │       │  LISTENING   │
     └──────┬───────┘       └──────┬───────┘
            │                      │
         PTT UP                    │ VAD
            │                      ▼
            │               ┌──────────────┐
            │               │ TRANSCRIBING │
            │               └──────┬───────┘
            │                      │
            ▼                      ▼
     ┌──────────────┐       ┌──────────────┐
     │ TRANSCRIBING │       │    INJECT    │
     └──────┬───────┘       └──────┬───────┘
            │                      │
            ▼                      ▼
     ┌──────────────┐       ┌──────────────┐
     │    INJECT    │──────►│  LISTENING   │
     └──────┬───────┘       └──────────────┘
            │
            ▼
          IDLE
```

---

## 11. Daemon

Proces powinien działać jako:

```text
systemd --user service
```

Nie ma potrzeby uruchamiania go jako root.

Przykładowa usługa:

```text
~/.config/systemd/user/local-stt.service
```

Wymagania:

- start po zalogowaniu;
- restart po awarii;
- logi dostępne przez `journalctl`;
- konfiguracja użytkownika;
- brak uprawnień root.

---

## 12. Konfiguracja

Konfiguracja powinna być oddzielona od implementacji.

Przykładowa struktura:

```yaml
stt:
  engine: whisper.cpp
  model: small
  language: pl
  threads: 8

audio:
  device: default
  sample_rate: 16000
  channels: 1

vad:
  enabled: true
  minimum_speech_duration_ms: 250
  minimum_silence_duration_ms: 600
  maximum_chunk_duration_s: 15

hotkeys:
  push_to_talk: Super+Space
  continuous: Super+Shift+Space

injection:
  backend: auto
  add_space: true
```

Format konfiguracji może zostać zmieniony podczas implementacji.

---

## 13. Tryb debug

Daemon powinien mieć możliwość uruchomienia z większą ilością logów:

```text
INFO
DEBUG
TRACE
```

Przykładowe informacje diagnostyczne:

```text
[INFO] microphone: default
[INFO] sample rate: 16000 Hz
[INFO] model: small
[INFO] threads: 8
[INFO] state: LISTENING
[DEBUG] VAD: speech started
[DEBUG] VAD: speech ended
[INFO] transcription: 1.42 s
[INFO] injection: 84 chars
```

Nie należy logować surowego audio.

W trybie normalnym nie należy również logować pełnego tekstu użytkownika bez wyraźnej potrzeby.

---

## 14. Prywatność

Podstawowa zasada:

> Audio i transkrypcja pozostają lokalnie na komputerze.

System nie powinien:

- wysyłać audio do chmury;
- korzystać z zewnętrznego API STT;
- wymagać konta użytkownika;
- wymagać Internetu podczas działania.

Internet może być wymagany wyłącznie podczas instalacji lub ręcznego pobierania modeli/dependencies.

---

## 15. MVP

Pierwsza wersja powinna być możliwie mała.

### MVP v0.1

- [ ] Ubuntu;
- [ ] mikrofon;
- [ ] whisper.cpp;
- [ ] model `base` lub `small`;
- [ ] polski;
- [ ] PTT;
- [ ] transkrypcja po zakończeniu nagrania;
- [ ] wklejenie tekstu do aktywnego okna;
- [ ] prosty globalny hotkey;
- [ ] logowanie;
- [ ] konfiguracja;
- [ ] systemd user service.

### v0.2

- [ ] continuous dictation;
- [ ] VAD;
- [ ] automatyczne dzielenie na fragmenty;
- [ ] automatyczne wpisywanie kolejnych fragmentów;
- [ ] status daemon;
- [ ] obsługa błędów audio.

### v0.3

- [ ] partial transcription;
- [ ] stabilizacja wyników;
- [ ] lepsze zarządzanie kontekstem Whisper;
- [ ] możliwość przełączania modeli;
- [ ] benchmark CPU/latency.

---

## 16. Benchmark

Przed wyborem modelu należy wykonać benchmark na rzeczywistym sprzęcie.

Kluczowe metryki:

### Real-time factor

```text
RTF = processing_time / audio_duration
```

Interpretacja:

```text
RTF < 1.0  → szybsze niż rzeczywisty czas
RTF = 1.0  → około realtime
RTF > 1.0  → wolniejsze niż realtime
```

Dla continuous dictation szczególnie istotne jest, aby średnia długość kolejki transkrypcji nie rosła w czasie.

### Dodatkowe metryki

- CPU utilization;
- peak RAM;
- latency od końca wypowiedzi do tekstu;
- długość fragmentów;
- liczba błędnych segmentacji;
- jakość transkrypcji języka polskiego.

---

## 17. Potencjalne problemy

### CPU

i5-8365U jest wystarczający do lokalnego STT, ale nie należy projektować systemu tak, jakby miał do dyspozycji GPU.

Priorytet:

```text
responsiveness > maksymalna jakość modelu
```

dla continuous dictation.

### Wentylator / throttling

Długotrwała transkrypcja może utrzymywać CPU na wysokim poziomie.

Benchmark należy wykonać również przez kilka minut ciągłej pracy, a nie tylko na pojedynczym nagraniu.

### Wayland

Mechanizm wprowadzania tekstu może być trudniejszy niż na X11.

Należy wykrywać środowisko:

```text
X11
Wayland
```

i dobierać backend injection odpowiednio.

### Mikrofon

Jakość mikrofonu będzie miała duży wpływ na końcowy wynik. Optymalizacja modelu nie naprawi mocno zaszumionego lub źle ustawionego wejścia audio.

---

## 18. Przyszłe rozszerzenia

Możliwe późniejsze funkcje:

- automatyczna interpunkcja;
- automatyczne wielkie litery;
- komendy głosowe;
- profile językowe;
- polski + angielski;
- słownik własnych słów;
- korekta typowych błędów;
- clipboard mode;
- historia ostatnich transkrypcji;
- tray indicator;
- GUI;
- konfiguracja hotkeyów;
- per-application behavior;
- tryb „transkrybuj, ale nie wklejaj”;
- eksport do pliku;
- integracja z edytorami;
- lokalny LLM do post-processingu tekstu.

---

## 19. Zasada projektowa

STT, VAD, audio capture i text injection powinny być osobnymi komponentami.

Docelowo:

```text
Audio Backend
      │
      ▼
     VAD
      │
      ▼
 Audio Buffer
      │
      ▼
  STT Engine
      │
      ▼
Transcript Processor
      │
      ▼
Text Injector
```

Pozwoli to wymienić np. Whisper na inny lokalny silnik STT bez przepisywania reszty aplikacji.

---

## 20. Następny krok

Implementację należy rozpocząć od **MVP v0.1**, a nie od continuous streaming.

Kolejność:

1. przygotować środowisko Ubuntu;
2. zbudować `whisper.cpp`;
3. pobrać model;
4. sprawdzić jakość polskiego STT;
5. zmierzyć wydajność `base` i `small`;
6. przygotować audio capture;
7. zaimplementować PTT;
8. dodać text injection;
9. uruchomić jako `systemd --user`;
10. dopiero potem dodać VAD i continuous dictation.

To ogranicza liczbę zmiennych i pozwala najpierw ustalić, czy wybrany model jest wystarczająco szybki na i5-8365U.
