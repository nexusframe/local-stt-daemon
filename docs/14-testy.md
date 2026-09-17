# 14. Strategia testów

Narzędzia: `pytest`, `pytest-timeout`, `ruff` (lint + format), `mypy --strict` dla `src/local_stt` (poza `bench/`). Uruchamianie: `pytest -m "not needs_whisper and not needs_x11 and not needs_audio"` (szybkie) i `pytest` (pełne, lokalnie).

## 14.1 Piramida

| Poziom | Zakres | Środowisko | Marker |
|---|---|---|---|
| Jednostkowe | czysta logika | brak zewnętrznych zasobów | — |
| Integracyjne: silnik | `WhisperServerEngine` ↔ prawdziwy `whisper-server` (`ggml-base-q5_1` dla szybkości) | lokalny build | `needs_whisper` |
| Integracyjne: X11 | hotkeye, schowek, XTest | `xvfb-run` (pakiet `xvfb`, instalowany przez `install.sh --dev`; bez muttera) | `needs_x11` |
| Integracyjne: audio | otwarcie urządzenia, resampling | prawdziwy PipeWire | `needs_audio` |
| End-to-end | cały daemon | Xvfb + `FileAudioSource` + prawdziwy serwer | `e2e` |
| Akceptacyjne ręczne | prawdziwa sesja GNOME, mikrofon, aplikacje | maszyna referencyjna | lista 14.4 |

## 14.2 Testy jednostkowe (wymagane)

| Moduł | Co testujemy | Technika |
|---|---|---|
| `controller` | **każdy wiersz tabeli z [04](04-maszyna-stanow.md) §4.3** + odmowy `invalid_in_mode` dla IPC + ignorowane zdarzenia; grupy reload z §4.6 (na żywo / odroczone / restart serwera) | test parametryzowany: (stan, zdarzenie, warunki) → (akcje na fake'ach, nowy stan) |
| `pipeline` | kolejność wg `seq`; anulowanie przez generację na każdym z 3 punktów kontrolnych; retry ×1 dla 5xx/timeout; wstrzymanie przy DOWN i wznowienie; zbiorcze `JobFailed` po timeoucie; bramka RMS w oknach 100 ms; kontekst promptu per sesja; limit 200 znaków | `FakeEngine` z opóźnieniami i błędami, `RecordingInjector` |
| `segmenter` | start po `min_speech_ms`; odrzucenie krótkich impulsów; histereza (p oscylujące między progami nie kończy mowy); koniec po `min_silence_ms`; pre-roll i padding; cięcie przy `max_segment_s` w najdłuższej ciszy / najniższym p; `flush()` | `ScriptedVad` zwracający zadaną sekwencję p; asercje na granicach próbek |
| `vad` | kształty tensorów, przenoszenie 64 próbek kontekstu i stanu, `reset()` | prawdziwy `silero_vad.onnx` na fixture ciszy (p < 0,1) i mowy (max p > 0,8) |
| `recorder` | maskowanie okna dźwięku startu; limit długości; `AudioClip` | syntetyczne ramki |
| `text.filters` | każdy wzorzec halucynacji (pozytywne i negatywne — „Dziękuję za uwagę, a teraz…” **nie** może zostać wycięte); `no_speech`+`logprob` (tylko łącznie); pętle n-gramów; echo promptu | tabela przypadków |
| `text.processor` | kroki 2–7 z [08](08-text-injection.md) §8.2, w tym `max_length` → usunięcie kropki i małe litery | tabela |
| `hotkeys.spec` | parsowanie, błędy, odrzucanie AltGr/Super_L/Control_L/Shift_L, walidacja `ptt_cancel_key` | |
| `config` | wartości domyślne; każda reguła z [09](09-konfiguracja.md) §9.3; nieznany klucz → błąd; generowanie `whisper-server.env` | |
| `audio.wav` | float32 → WAV s16: nagłówek, clipping, round-trip | |
| `stt.whisper_server` | budowa multipart; parsowanie `verbose_json` (fixture z prawdziwej odpowiedzi v1.9.4); mapowanie błędów HTTP/timeoutów | lokalny `http.server` stub |
| `ipc` | protokół, nieznane polecenie, zbyt długa linia (> 64 KiB), `SO_PEERCRED` (uid inny → odrzucenie; test przez monkeypatch) | |
| `bench.wer` | WER/CER na znanych przykładach, normalizacja polskich znaków | |
| `feedback` | generowane WAV mają poprawną długość i brak DC; `notify-send` wywoływany z oczekiwanymi argumentami i **bez tekstu transkrypcji** | mock `subprocess` |

Cel pokrycia: ≥ 90% linii dla `controller`, `pipeline`, `segmenter`, `text/`, `config`. Pozostałe moduły bez twardego progu.

## 14.3 Integracyjne i E2E

1. **`needs_whisper`.** Start `whisper-server` z `base-q5_1` na losowym porcie → `transcribe(fixtures/pl_short.wav)`:
   - niepusty tekst,
   - czas < 30 s,
   - `/health` przechodzi `loading` → `ok`,
   - zabicie serwera w trakcie → `engine=DOWN`, kolejka wstrzymana, po ponownym starcie zadanie kończy się sukcesem (E7); bez restartu → `JobFailed` po `startup_timeout_s` (skrócony w teście),
   - żądanie bez prefiksu `--request-path` → 404.
2. **`needs_x11` (Xvfb).** Ostrzeżenie: Xvfb nie ma muttera, więc te testy **nie** wykrywają konfliktów z GNOME. Pokrywa je lista 14.4.
   - grab `Control_R` + XTest press/release → zdarzenia `PttPressed`/`PttReleased` (release ze stanem `ControlMask`); `Shift+Control_R` z puszczeniem Shift przed Ctrl → tylko `ContinuousToggle`; autorepeat (XTest press-release-press z tym samym czasem na `F9`) → brak fałszywego release,
   - `BadAccess`: drugi klient grabuje ten sam klawisz → `hotkeys: degraded`,
   - `ClipboardOwner`: inny klient ustawia tekst + `text/html` → zapis → nasze przejęcie → klient-odbiorca (okno testowe, które na `Ctrl+V` wysyła `ConvertSelection`) dostaje `UTF8_STRING` z polskimi znakami → przywrócenie wszystkich celów bajt w bajt (z typem i formatem); cel > 256 KiB lub odpowiedź INCR → backend `type`,
   - „menedżer schowka” (trzeci klient pobierający treść zaraz po zmianie właściciela) **nie** potwierdza wklejenia,
   - wstrzyknięte `Control_L` nie wyzwala grabu PTT,
   - brak odbiorcy → `left_in_clipboard=True`,
   - czekanie na modyfikatory: XTest trzyma `Shift_L` → inject czeka, aż zostanie puszczony.
3. **`e2e`.** Daemon z `FileAudioSource` (odtwarza WAV w tempie rzeczywistym zamiast mikrofonu), Xvfb, okno odbiorcze jak wyżej i prawdziwy serwer:
   - IPC `ptt start` → 3 s → `ptt stop` → w ≤ 30 s okno odbiera tekst zawierający oczekiwane słowa kluczowe,
   - continuous z nagraniem 3 zdań i pauzami → 3 wklejenia w kolejności,
   - `cancel` w trakcie → brak wklejeń.
4. **Brak sieci.** `unshare -rn sh -c 'ip link set lo up && XDG_RUNTIME_DIR=$(mktemp -d) pytest -m e2e'`. W nowej przestrzeni nazw `lo` jest domyślnie wyłączone, a `/run/user/1000` należy do niezmapowanego uid, stąd oba kroki. Fixture e2e uruchamia `whisper-server` i Xvfb **wewnątrz** tej przestrzeni. Test musi przejść, co potwierdza F1/N5.

## 14.4 Lista akceptacyjna (ręczna, na maszynie referencyjnej, przed wydaniem każdej wersji)

v0.1:

- [ ] Świeży `install.sh` → `doctor` bez FAIL.
- [ ] Po wylogowaniu i zalogowaniu obie usługi działają; `status` = IDLE w ≤ 60 s.
- [ ] PTT w: gedit/GNOME Text Editor, Firefox (pole tekstowe), Chrome/Electron (np. VS Code), GNOME Terminal (Ctrl+Shift+V), LibreOffice Writer — polskie znaki poprawne.
- [ ] PTT z tekstem w schowku → po wklejeniu schowek zawiera stary tekst.
- [ ] PTT ze zrzutem ekranu w schowku (obraz) → użyty backend `type`, obraz nadal w schowku.
- [ ] PTT na pulpicie bez fokusu → powiadomienie „tekst w schowku”.
- [ ] Tapnięcie prawego Ctrl (< 300 ms) → nic się nie dzieje.
- [ ] PTT + Esc → anulowane, brak tekstu.
- [ ] PTT bez mówienia (5 s ciszy) → nic nie jest wpisane (brak „Amara.org”), słychać dźwięk `cancel` (dźwięk startu z głośników nie przechodzi bramki).
- [ ] Menedżer historii schowka (np. rozszerzenie GNOME Clipboard Indicator) włączony → wklejany jest dyktowany tekst, a nie stara zawartość.
- [ ] Kopia z przeglądarki (tekst + HTML) w schowku → po PTT wklejenie w LibreOffice zachowuje formatowanie.
- [ ] `Super+Space` nadal przełącza układ; overlay (Super) działa normalnie.
- [ ] `systemctl --user stop local-stt-whisper` → PTT daje dźwięk błędu + powiadomienie; `start` → wraca do READY.
- [ ] `kill -9` daemona → restart ≤ 5 s (N9).
- [ ] Zmiana `stt.model` w configu + `local-stt reload` → serwer restartuje się sam, `status` przechodzi STARTING → IDLE z nowym modelem (N6).
- [ ] Pliki skopiowane w Nautilusie → po PTT można je nadal wkleić w Nautilusie.
- [ ] `journalctl --user -u local-stt` nie zawiera treści dyktowanego tekstu.

v0.2 (dodatkowo):

- [ ] Continuous: 5 minut dyktowania artykułu → tekst kompletny, w kolejności, bez zdublowanych fragmentów.
- [ ] Continuous z włączonym wentylatorem / pisaniem na klawiaturze w tle → brak fałszywych fragmentów z szumu (lub pojedyncze, odfiltrowane).
- [ ] Odłączenie mikrofonu USB w trakcie continuous → PipeWire przepina strumień na mikrofon wbudowany (log INFO), dyktowanie trwa; `systemctl --user restart pipewire` w trakcie → do 3 prób ponownego otwarcia, a przy porażce powiadomienie i wyłączenie trybu, daemon działa.
- [ ] `local-stt cancel` w trakcie → brak dalszych wklejeń.
- [ ] Wyciszony mikrofon systemowo → powiadomienie „wyciszony”.
