# 15. Plan implementacji

Kolejność wynika z zasady projektu wstępnego: **najpierw ustalić, czy model jest wystarczająco szybki, potem PTT, na końcu continuous.** Każdy etap kończy się kryteriami akceptacji. Etapu nie zaczynamy, dopóki poprzedni ich nie spełnia.

## Etap 0 — Środowisko i pomiar (przed v0.1)

**Cel:** działający `whisper-server`, korpus nagrań i pierwsze liczby. Bez daemona, bez hotkeyów.

| # | Zadanie | Wynik |
|---|---|---|
| 0.1 | Szkielet repo: `pyproject.toml` (src layout), `ruff`, `mypy`, `pytest`, `requirements.lock`, `git init` | `pytest` przechodzi na pustym zestawie |
| 0.2 | `scripts/install.sh` kroki 1–4 (apt, build whisper.cpp v1.9.4 + `.whisper-tag`, venv) i generowanie `secret` | `bin/whisper-server --help` |
| 0.3 | `local_stt/models.py` + `local-stt models pull/list/verify` + `scripts/models.sha256` (base-q5_1, small-q5_1, small-q8_0, small, medium-q5_0, large-v3-turbo-q5_0, silero-vad) | modele na dysku, sumy OK |
| 0.4 | `audio/capture.py` (minimum: PCM `pipewire` 16 kHz, ramki, `PIPEWIRE_NODE`) + `audio/wav.py` | test `needs_audio`; potwierdzenie, że `sounddevice` z `PIPEWIRE_NODE` trafia do wskazanego węzła (dla `arecord` już zweryfikowane, [05](05-audio-i-vad.md) §5.2) |
| 0.5 | `stt/whisper_server.py` + `local-stt transcribe FILE.wav` | polski tekst z pliku |
| 0.6 | `bench/corpus.py` (`record-corpus`, także `--long`) + `bench/prompts_pl.txt` | nagrany korpus A |
| 0.7 | `bench/wer.py`, `bench/runner.py` (etapy 1–2 z [13](13-benchmark.md) §13.4; tymczasowy serwer), `bench/report.py` | `docs/benchmark-results.md` (wstępny) |

**Akceptacja:**

- raport z etapów 1–2 dla wszystkich modeli,
- wybrane tymczasowe `stt.model`, `threads` i `dynamic_audio_ctx` według reguły z §13.5 (bez soak — ten przychodzi w v0.2).

## v0.1 — MVP: PTT

| # | Zadanie | Dokument |
|---|---|---|
| 1.1 | `config.py` z pełną walidacją + `config.example.toml` + generowanie `whisper-server.env` | 09 |
| 1.2 | `logging_setup.py` (TRACE, format journald), `sdnotify.py` | 12, 11 |
| 1.3 | `events.py`, `interfaces.py`, `controller.py` — stany IDLE / PTT_RECORDING, `engine`, wiersze „Dowolny stan” | 04 |
| 1.4 | `engine_monitor.py` | 04 §4.5 |
| 1.5 | `audio/recorder.py` z limitem i maskowaniem dźwięku startu | 05 §5.2–5.3 |
| 1.6 | `pipeline.py` (worker, bramka RMS, generacje, retry, wstrzymanie przy DOWN, timingi) | 04 §4.4–4.5 |
| 1.7 | `text/filters.py`, `text/processor.py` (bez kroku 5 — ciągłość continuous) | 08 §8.2, 06 §6.8 |
| 1.8 | `audio/consumer.py`; `inject/x11util.py`, `inject/clipboard.py`, `inject/xdotool.py`, `inject/auto.py` | 05, 08 |
| 1.9 | `hotkeys/spec.py`, `hotkeys/x11.py` (PTT + Esc) | 07 |
| 1.10 | `ipc.py` + CLI z [10](10-cli-ipc-status.md) §10.1 oznaczone v0.1 (w tym `reload` z grupami i restartem serwera) | 10, 04 §4.6 |
| 1.11 | `feedback.py` — dźwięki wg macierzy + powiadomienia o błędach (`notify-send -p/-r`) | 10 §10.6 |
| 1.12 | `doctor.py` | 10 §10.5 |
| 1.13 | `app.py` — składanie, start/stop, sygnały, `threading.excepthook` | 02, 12 |
| 1.14 | `systemd/*.service`, `install.sh` kroki 5–9, `uninstall.sh` | 11 |
| 1.15 | `audio/file_source.py` (`FileAudioSource`, potrzebny do e2e) + testy: jednostkowe z 14.2 dla powyższych, `needs_whisper`, `needs_x11`, e2e PTT | 14 |

**Akceptacja v0.1:**

- lista 14.4 (v0.1) w całości,
- N1 (RAM), N2 (latencja PTT — pomiar z logów `timings` dla 20 dyktowań), N5, N8, N9.

Wymagania z projektu wstępnego pokryte przez v0.1:

- Ubuntu, mikrofon, whisper.cpp, model (wg benchmarku), polski,
- PTT, transkrypcja po nagraniu, wklejenie do aktywnego okna, globalny hotkey,
- logowanie, konfiguracja, systemd user service.

## v0.2 — Continuous dictation

| # | Zadanie | Dokument |
|---|---|---|
| 2.1 | `audio/vad.py` (Silero ONNX) | 05 §5.4 |
| 2.2 | `audio/segmenter.py` z histerezą i cięciem przy `max_segment_s` | 05 §5.5 |
| 2.3 | Controller: stan CONTINUOUS, toggle z flush, cancel, backlog, reconnect, `EngineStateChanged(DOWN)`, odroczony reload | 04 §4.3, §4.6 |
| 2.4 | Pipeline: kontekst promptu per sesja; TextProcessor krok 5 (ciągłość) | 04 §4.4, 08 §8.2 |
| 2.5 | Hotkey continuous toggle; CLI `toggle`, `status --watch` (IPC `subscribe`) | 07, 10 |
| 2.6 | VAD-trim dla PTT (zastępuje bramkę RMS, gdy `vad.enabled`) | 05 §5.3 |
| 2.7 | Obsługa błędów audio: reconnect ×3, overflow, cisza cyfrowa | 05 §5.6 |
| 2.8 | `STATUS=` do systemd; powiadomienia `all` | 11, 10 |
| 2.9 | `bench --soak` (na `FileAudioSource` z v0.1) | 13 §13.4 etap 3 |
| 2.10 | Testy: segmenter, controller continuous, e2e continuous | 14 |

**Akceptacja v0.2:**

- lista 14.4 (v0.2),
- soak 10 min na baterii spełnia N3; raport błędnych segmentacji (13 §13.3),
- N4 (CPU w ciszy) zmierzone przez `pidstat -p <pid> 1 60`,
- ostateczne defaulty modelu wpisane do `docs/benchmark-results.md` i `config.example.toml`.

Wymagania z projektu wstępnego pokryte przez v0.2: continuous dictation, VAD, automatyczne dzielenie na fragmenty, automatyczne wpisywanie kolejnych fragmentów, status daemona, obsługa błędów audio.

## v0.3 — Jakość i sterowanie

Zakres z projektu wstępnego („partial transcription, stabilizacja wyników, lepsze zarządzanie kontekstem, przełączanie modeli, benchmark CPU/latency”) przełożony na zadania zgodne z ADR-010:

| # | Zadanie | Uwagi |
|---|---|---|
| 3.1 | **Przełączanie modeli:** zmiana `stt.model` + `local-stt reload` działa już od v0.1 (restart serwera, [04](04-maszyna-stanow.md) §4.6). W v0.3 dochodzi tylko `local-stt models list --bench`, czyli lista modeli z wynikami z ostatniego benchmarku (WER, p90 latencji, RAM), żeby wybór był świadomy. Bez runtime override — config jest jedynym źródłem prawdy (ADR-008) | 06 §6.5, 13 |
| 3.2 | **Podgląd częściowy (partial), bez wpisywania:** w continuous, gdy wypowiedź trwa > 4 s, co 2 s wysyłamy dotychczasowe audio z `dynamic_audio_ctx` do silnika **tylko jeśli kolejka jest pusta**; wynik trafia do `status --watch` i (opcjonalnie) jednego zastępowanego powiadomienia. Nigdy do okna. Nowy klucz `continuous.preview = false` (dopisać do [09](09-konfiguracja.md) w v0.3) | ADR-010 |
| 3.3 | **Stabilizacja granic fragmentów:** cięcie `max_length` z 1 s nakładki audio + usuwanie zdublowanych słów na styku (najdłuższy wspólny sufiks/prefiks słów ≥ 2) | 05 §5.5 |
| 3.4 | **Kontekst:** strojenie `continuous_context` (długość końcówki, reset po `min_silence` > 5 s = nowy akapit) na podstawie korpusu `long/` | 06 §6.6 |
| 3.5 | `stt.continuous_model` (drugi serwer) — **tylko jeśli** reguła z 13 §13.5 tego wymaga | ADR-016 |
| 3.6 | Pełny raport benchmarku (`bench report`) z termiką; aktualizacja `docs/benchmark-results.md` | 13 |

**Akceptacja v0.3:**

- WER na korpusie `long/` w continuous jest nie gorszy niż w v0.2, a liczba zdublowanych słów na granicach = 0,
- preview nie zwiększa średniej latencji finalnych fragmentów o więcej niż 10%.

## Po v0.3 — backlog (bez zobowiązań)

Kolejność według wartości dla użytkownika:

1. `text.replacements` z gotowymi „komendami” (nowa linia, kropka, przecinek) — mechanizm już istnieje.
2. Historia ostatnich N transkrypcji **tylko w RAM** + `local-stt last` (ponowne wklejenie) — przydatne przy E12.
3. Tray / wskaźnik (osobny proces-klient IPC, AppIndicator).
4. Tryb „transkrybuj, nie wklejaj” (`injection.backend = "clipboard-only"`).
5. Profile per aplikacja (`WM_CLASS` → backend, `append_space`, prompt).
6. Polski + angielski (`language = "auto"` z ograniczeniem do {pl, en}).
7. Lokalny LLM do interpunkcji/korekty (osobny proces, jak whisper-server).
8. Wayland (ADR-011).
