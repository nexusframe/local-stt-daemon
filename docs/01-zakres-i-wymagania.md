# 01. Zakres i wymagania

## 1.1 Co budujemy

`local-stt` to działający w tle program (daemon) dla Ubuntu. Zamienia mowę po polsku na tekst **całkowicie offline** i wpisuje ten tekst do aktualnie aktywnego okna.

Składa się z dwóch procesów:

| Proces | Język | Rola |
|---|---|---|
| `local-stt-whisper.service` | C++ (gotowy `whisper-server` z whisper.cpp) | trzyma model Whisper w pamięci i transkrybuje audio przesłane przez `127.0.0.1` |
| `local-stt.service` | Python 3.12 (nasz kod) | hotkeye, mikrofon, VAD, maszyna stanów, post-processing tekstu, wpisywanie tekstu, CLI/status |

Do tego dochodzi narzędzie CLI `local-stt` (pełna lista: [10](10-cli-ipc-status.md) §10.1).

## 1.2 Środowisko docelowe (zweryfikowane na maszynie referencyjnej)

| Element | Wartość |
|---|---|
| OS | Ubuntu 24.04.5 LTS (noble) |
| Sesja | **GNOME na Xorg (X11)**, `XDG_SESSION_TYPE=x11`, WM: mutter |
| CPU | Intel Core i5-8365U, 4 rdzenie / 8 wątków, AVX2 |
| RAM | 16 GB (w praktyce ok. 4–5 GB wolnego przy normalnej pracy) |
| GPU | brak dedykowanego |
| Audio | PipeWire 1.0.5 z `pipewire-pulse` i `pipewire-alsa`; mikrofon: wbudowany ALC3254 |
| Układy klawiatury | GNOME input sources: `pl` (X11 `setxkbmap -query`: `pl,us`); prawy Alt = AltGr (`ISO_Level3_Shift`) — **niezbędny do polskich znaków** |
| Skróty zajęte przez GNOME | `Super+Space`, `Shift+Super+Space` (przełączanie źródła wprowadzania), `Super_L` (overlay/Activities), `Control_L` (locate-pointer, wyłączone) |
| Python | 3.12.3 (systemowy) |
| systemd | 255, `graphical-session.target` aktywny w sesji użytkownika |
| Narzędzia obecne | `xdotool 3.20160805.1`, `notify-send`, `pw-record`, `gcc`, `git` |
| Narzędzia brakujące | `cmake`, `python3-venv`, `libportaudio2` (instaluje je `scripts/install.sh`) |

Wayland **nie jest celem** w wersjach v0.1–v0.3. Architektura go nie wyklucza: hotkeye i wpisywanie tekstu są za interfejsami, więc wsparcie dla Waylanda można dodać później jako osobne backendy (zob. [03-decyzje.md](03-decyzje.md), ADR-011).

## 1.3 Tryby pracy

### Push-to-talk (PTT)

1. Użytkownik **wciska i trzyma** klawisz PTT (domyślnie prawy `Ctrl`).
2. Daemon otwiera strumień z mikrofonu i nagrywa.
3. Użytkownik puszcza klawisz. Nagranie trafia do kolejki transkrypcji.
4. Tekst jest przetwarzany i wpisywany do okna, które jest aktywne **w chwili wpisywania**.

`Esc` wciśnięty w trakcie trzymania PTT anuluje nagranie. Nagrania krótsze niż `ptt.min_duration_ms` są odrzucane jako przypadkowe tapnięcia.

### Continuous dictation

1. `Shift + prawy Ctrl` włącza nasłuchiwanie. Daemon gra dźwięk startu.
2. Mikrofon jest cały czas otwarty, a VAD (Silero) dzieli audio na wypowiedzi.
3. Każda zakończona wypowiedź trafia do kolejki transkrypcji. **Nagrywanie nie zatrzymuje się w trakcie transkrypcji.**
4. Wyniki są wpisywane w kolejności nagrania.
5. Ponowne `Shift + prawy Ctrl` kończy tryb. Trwająca wypowiedź jest domykana i też zostaje przetranskrybowana.
6. `local-stt cancel` kończy tryb i odrzuca wszystko, czego jeszcze nie wpisano. Działa także po zakończeniu PTT, dopóki tekst nie został wpisany.

## 1.4 Wymagania funkcjonalne → gdzie są opisane

| ID | Wymaganie | Wersja | Dokument |
|---|---|---|---|
| F1 | działanie całkowicie offline | v0.1 | 12, 11 |
| F2 | brak wymogu GPU | v0.1 | 06 |
| F3 | język polski | v0.1 | 06, 13 |
| F4 | push-to-talk | v0.1 | 04, 07 |
| F5 | continuous dictation | v0.2 | 04, 05 |
| F6 | globalne hotkeye | v0.1 | 07 |
| F7 | automatyczne wykrywanie mowy (VAD) | v0.2 | 05 |
| F8 | wpisywanie do aktywnego okna | v0.1 | 08 |
| F9 | praca jako daemon | v0.1 | 11 |
| F10 | autostart z sesją | v0.1 | 11 |
| F11 | czytelny status | v0.1 (CLI, dźwięki, powiadomienia o błędach), v0.2 (`status --watch`, `STATUS=` w systemd, powiadomienia `all`) | 10 |
| F12 | ręczne zatrzymanie nasłuchiwania | v0.2 | 04, 10 |

## 1.5 Wymagania niefunkcjonalne — mierzalne cele

| ID | Cel | Jak mierzymy |
|---|---|---|
| N1 | RAM: daemon Python ≤ 150 MB RSS; `whisper-server` z wybranym modelem ≤ 1 GB RSS | `local-stt bench`, `ps -o rss` |
| N2 | PTT: p90 latencji od puszczenia klawisza do końca wpisywania (`total` w logach) ≤ 2,5 s dla wypowiedzi 4–10 s (model domyślny) | `bench` (bez wklejania, próg 2,4 s) + logi `timings` z 20 dyktowań |
| N3 | Continuous: RTF ≤ 0,5 średnio w 10-minutowym teście, kolejka nie rośnie monotonicznie | `bench --soak` |
| N4 | Continuous w ciszy: CPU daemona ≤ 5% jednego rdzenia | `pidstat` |
| N5 | żaden bajt audio ani tekstu nie opuszcza hosta; `whisper-server` nasłuchuje tylko na `127.0.0.1` | `ss -ltnp`, `doctor` |
| N6 | wymiana modelu bez przebudowy: zmiana `stt.model` + `local-stt reload` | [06](06-silnik-stt.md) |
| N7 | wymiana silnika STT bez zmian poza modułem `stt/` | interfejs `SttEngine` |
| N8 | cała konfiguracja w `~/.config/local-stt/config.toml` | [09](09-konfiguracja.md) |
| N9 | po awarii procesu restart ≤ 5 s (systemd) | test ręczny `kill -9` |

## 1.6 Poza zakresem (świadomie)

- Wayland, KDE, inne dystrybucje (mogą działać, ale nie są testowane).
- GPU, CUDA, OpenVINO.
- Transkrypcja częściowa „na żywo” z poprawianiem już wpisanego tekstu (wstępnie planowana na v0.3 jako podgląd w powiadomieniu, nigdy nie wpisywana — zob. [04](04-maszyna-stanow.md) i [15](15-plan-implementacji.md)).
- Komendy głosowe, GUI, tray, LLM do post-processingu — tylko jako punkty rozszerzeń ([15](15-plan-implementacji.md), sekcja „Po v0.3”).
- Akcje `PAUSE`/`RESUME` z projektu wstępnego (§9): odłożone. Continuous włącza się i wyłącza jednym skrótem, a stan kolejki zostaje zachowany, więc osobna pauza nie wnosi nic nowego. Wrócimy do tego, jeśli pojawi się potrzeba zachowania kontekstu promptu między sesjami.
- Konfigurowalne `sample_rate`/`channels` (projekt wstępny §5.1): odrzucone, patrz [05](05-audio-i-vad.md) §5.2.
- Uruchamianie jako root i jakakolwiek usługa systemowa (poza pakietami z apt podczas instalacji).
