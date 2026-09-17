# local-stt — dokumentacja

Lokalny, działający offline daemon **speech-to-text po polsku** dla Ubuntu 24.04 (GNOME na X11), zoptymalizowany pod CPU bez GPU (Intel i5-8365U).

- **Push-to-talk:** trzymasz **prawy Ctrl**, mówisz, puszczasz, a tekst pojawia się w aktywnym oknie.
- **Continuous:** **Shift + prawy Ctrl** włącza dyktowanie ciągłe. VAD dzieli mowę na fragmenty, a te są wpisywane na bieżąco.

Wszystko dzieje się lokalnie: whisper.cpp (`whisper-server` na `127.0.0.1`) + daemon w Pythonie.

Ta dokumentacja jest **specyfikacją do implementacji**: opisuje, co ma powstać, jak działa i dlaczego tak. Punkt wyjścia stanowił [`../local-stt-daemon-design.md`](../local-stt-daemon-design.md).

## Spis treści

| # | Dokument | Zawartość |
|---|---|---|
| 01 | [Zakres i wymagania](01-zakres-i-wymagania.md) | środowisko docelowe (zweryfikowane), tryby pracy, wymagania F/N z mierzalnymi celami, poza zakresem |
| 02 | [Architektura](02-architektura.md) | procesy, wątki, struktura repozytorium, sekwencje PTT/continuous, typy, punkty rozszerzeń |
| 03 | [Rejestr decyzji (ADR)](03-decyzje.md) | 17 decyzji z opcjami, zarzutami i odpowiedziami |
| 04 | [Maszyna stanów](04-maszyna-stanow.md) | stan `(mode, pipeline, engine)`, zdarzenia, pełna tabela przejść, kolejka zadań, reload |
| 05 | [Audio i VAD](05-audio-i-vad.md) | capture (sounddevice/PipeWire), PTT recorder, Silero VAD (ONNX), segmenter z histerezą |
| 06 | [Silnik STT](06-silnik-stt.md) | whisper.cpp v1.9.4: build, modele, flagi serwera, kontrakt HTTP, `audio_ctx`, prompt, filtr halucynacji |
| 07 | [Hotkeye X11](07-hotkeys-x11.md) | XGrabKey, domyślne klawisze i konflikty z GNOME, press/release, autorepeat |
| 08 | [Tekst i wpisywanie](08-text-injection.md) | TextProcessor, schowek + XTest z potwierdzeniem i przywracaniem, fallback xdotool |
| 09 | [Konfiguracja](09-konfiguracja.md) | pełny `config.toml` z domyślnymi wartościami, walidacje |
| 10 | [CLI, IPC, status](10-cli-ipc-status.md) | polecenia `local-stt`, protokół gniazda, `status`, `doctor`, dźwięki, powiadomienia |
| 11 | [Daemon, systemd, instalacja](11-daemon-systemd-instalacja.md) | układ plików, zależności, `install.sh`, oba unit-y systemd, operacje |
| 12 | [Logi, prywatność, błędy](12-logi-prywatnosc-bledy.md) | co logujemy, gwarancje prywatności, macierz błędów E1–E16 |
| 13 | [Benchmark](13-benchmark.md) | korpus, metryki (WER/RTF/latencja/termika), macierz, reguła wyboru modelu |
| 14 | [Testy](14-testy.md) | piramida testów, wymagane testy jednostkowe, Xvfb/E2E, lista akceptacyjna |
| 15 | [Plan implementacji](15-plan-implementacji.md) | etap 0 (pomiar) → v0.1 PTT → v0.2 continuous → v0.3 jakość; kryteria akceptacji |

Kolejność czytania:

- **implementacja:** 01 → 02 → 04 → 15, potem dokument właściwy dla bieżącego zadania,
- **„dlaczego tak?”:** 03.

## Najważniejsze decyzje w skrócie

| Obszar | Decyzja |
|---|---|
| Procesy | `local-stt-whisper.service` (whisper.cpp `whisper-server`, loopback) + `local-stt.service` (Python 3.12) |
| Model | `small-q5_1` na start; ostatecznie wybiera go benchmark (kandydaci do `medium-q5_0` i `large-v3-turbo-q5_0`); `base` odrzucony dla polskiego (~31% WER) |
| Hotkeye | XGrabKey: PTT = prawy Ctrl (trzymany), continuous = Shift + prawy Ctrl, Esc przy trzymanym PTT = anuluj |
| Audio | sounddevice 16 kHz mono, ramki 512 próbek; mikrofon otwarty tylko podczas nagrywania |
| VAD | Silero v6.2.1 (ONNX, onnxruntime) z histerezą 0,50/0,35, cisza 700 ms, max 15 s |
| Continuous | nagrywanie nigdy nie jest wstrzymywane; kolejka + 1 worker; wpisywane tylko finalne fragmenty |
| Wpisywanie | własny właściciel CLIPBOARD + XTest `Ctrl+V` / `Ctrl+Shift+V` (terminale), potwierdzenie wklejenia i przywrócenie schowka; `xdotool type` jako fallback |
| Config | TOML w `~/.config/local-stt/config.toml` |
| Sterowanie | gniazdo Unix + JSON Lines; CLI `local-stt status/toggle/cancel/reload/doctor/bench` |
| Status | dźwięki start/stop/cancel/error, powiadomienia o błędach, `status`, `STATUS=` w systemd |

## Różnice względem projektu wstępnego

| Projekt wstępny | Ta specyfikacja | Powód |
|---|---|---|
| Hotkeye `Super+Space` / `Super+Shift+Space` | prawy Ctrl / Shift + prawy Ctrl | oba zajęte w Ubuntu 24.04 (przełączanie źródła wprowadzania); konflikt z overlay-key muttera (ADR-007) |
| `LISTENING → TRANSCRIBING → INJECT → LISTENING` | stan `(mode, pipeline, engine)`; capture działa podczas transkrypcji | diagram wstępny gubił mowę (ADR-004) |
| `base` jako wariant główny | `base` tylko do testów | WER PL ~31% (ADR-003) |
| `threads: 8` | `threads: 4` do benchmarku | HT nie przyspiesza enkodera (dane z whisper.cpp#89) |
| Config YAML | TOML | stdlib, jednoznaczne typy (ADR-008) |
| Injection „X11 / clipboard / ydotool” | clipboard z potwierdzeniem + `xdotool type` jako fallback | polskie znaki w xdotool 2016 są zawodne (ADR-009) |
| `partial transcript` w v0.3 wpisywany do aplikacji | podgląd tylko w statusie/powiadomieniu | ADR-010 |

## Konwencje

- Wersje zweryfikowane na dzień **2026-09-17**: whisper.cpp v1.9.4, Silero VAD v6.2.1, python-xlib 0.33, xdotool 3.20160805.1 (Ubuntu), PipeWire 1.0.5, systemd 255.
- Twierdzenia oznaczone jako „niepotwierdzone” / „hipoteza” wymagają weryfikacji w trakcie implementacji. Wszystko inne sprawdzono na maszynie referencyjnej albo w kodzie źródłowym projektów.
