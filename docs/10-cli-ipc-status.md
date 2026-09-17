# 10. CLI, IPC i status

## 10.1 Polecenia `local-stt`

Jeden entry point (`[project.scripts] local-stt = "local_stt.cli:main"`), subkomendy przez `argparse`. **To jedyna pełna lista poleceń** — inne dokumenty odsyłają tutaj.

| Polecenie | Wersja | Działanie | Wymaga działającego daemona |
|---|---|---|---|
| `local-stt daemon [--config P] [--log-level L]` | v0.1 | uruchamia daemon na pierwszym planie (tak startuje go systemd) | — |
| `local-stt status [--json] [--watch]` | v0.1 / `--watch` v0.2 | stan daemona (10.4) | tak (inaczej: `daemon not running`, kod 3) |
| `local-stt ptt start\|stop` | v0.1 | jak wciśnięcie/puszczenie klawisza PTT | tak |
| `local-stt toggle` | v0.2 | włącz/wyłącz continuous | tak |
| `local-stt cancel` | v0.1 | anuluj nagrywanie/continuous i odrzuć niewpisane zadania | tak |
| `local-stt reload` | v0.1 | wczytaj config ponownie; wypisuje zastosowane, odroczone i czy serwer zostanie zrestartowany ([04](04-maszyna-stanow.md) §4.6) | tak |
| `local-stt doctor` | v0.1 | diagnostyka środowiska (10.5) | nie |
| `local-stt devices` | v0.1 | lista źródeł PipeWire (`pactl -f json list sources`, bez `.monitor`): nazwa węzła do `audio.device` + opis; oznaczenie domyślnego | nie |
| `local-stt models list\|pull NAME\|verify` | etap 0 | modele w `models_dir`, pobieranie z HF, weryfikacja SHA256 (jedyne polecenie korzystające z Internetu) | nie |
| `local-stt models list --bench` | v0.3 | lista modeli z wynikami ostatniego benchmarku | nie |
| `local-stt transcribe FILE.wav [--model M]` | etap 0 | jednorazowa transkrypcja pliku (test bez mikrofonu i hotkeyów); bez `--model` przez działający serwer, z `--model` przez **tymczasowy** serwer na porcie 8199 (jak `bench`), żeby nie zmieniać modelu usługi | bez `--model`: serwer tak |
| `local-stt record-corpus DIR [--long]` | etap 0 | nagrywanie korpusu do benchmarku ([13](13-benchmark.md) §13.2) | nie |
| `local-stt bench [--quick] [--dataset DIR] …` | etap 0 | macierz modeli na tymczasowych serwerach ([13](13-benchmark.md) §13.4) | nie (usługa serwera powinna być zatrzymana) |
| `local-stt bench --soak …` | v0.2 | test continuous 10 min przez prawdziwy Segmenter | nie |
| `local-stt bench report DIR` | etap 0 | raport Markdown z wyników | nie |

Kody wyjścia: `0` OK, `1` błąd ogólny, `2` błąd użycia, `3` daemon nie działa, `4` odrzucone przez daemon (np. `toggle`, gdy silnik DOWN), `78` błąd konfiguracji.

## 10.2 IPC — gniazdo sterujące

- Ścieżka: `$XDG_RUNTIME_DIR/local-stt/control.sock` (`/run/user/1000/…`, tmpfs, prywatne dla użytkownika).
- Katalog ma uprawnienia `0700`, gniazdo `0600`. Dodatkowo daemon sprawdza `SO_PEERCRED`: `uid` klienta musi równać się `os.getuid()`, inaczej zamyka połączenie.
- Protokół: **JSON Lines** (jedno żądanie = jedna linia UTF-8 zakończona `\n`, jedna odpowiedź = jedna linia).
- Serwer: wątek `ipc-server` (`socketserver.ThreadingUnixStreamServer`). Polecenia zmieniające stan są zamieniane na zdarzenia Controllera. Odpowiedź czeka na ich przetworzenie przez `concurrent.futures.Future` z timeoutem 5 s.
- Stale gniazdo: przy starcie, jeśli plik istnieje, a `connect()` się nie udaje, plik jest usuwany. Jeśli `connect()` się uda, daemon już działa: ERROR `another instance is running` i wyjście z kodem 1.

### Żądania i odpowiedzi

```json
→ {"cmd": "status"}
← {"ok": true, "status": { ...patrz 10.4... }}

→ {"cmd": "ptt", "action": "start"}
← {"ok": true}

→ {"cmd": "toggle"}
← {"ok": false, "error": "engine_down", "message": "Silnik STT niedostępny"}

→ {"cmd": "reload"}
← {"ok": true, "applied": ["vad.min_silence_ms"], "deferred": [], "server_restart": true}

→ {"cmd": "subscribe"}
← {"event": "state", "status": {...}}        // strumień – linia przy każdej zmianie stanu
← {"event": "job", "job_id": 17, "source": "continuous", "audio_s": 4.1, "processing_s": 1.9, "chars": 62, "result": "injected"}
```

Zdarzenia `job` **nie zawierają tekstu**.

## 10.3 Sygnały

| Sygnał | Działanie |
|---|---|
| `SIGTERM`, `SIGINT` | `ShutdownRequested` — czyste zamknięcie (ungrab, zamknięcie strumienia, usunięcie gniazda), kod 0 |
| `SIGHUP` | `ReloadRequested` (`systemctl --user reload local-stt`) |
| `SIGUSR1` | zrzut stanu wewnętrznego (kolejki, wątki, liczniki) na log INFO — diagnostyka zawieszeń |

## 10.4 Status

### `local-stt status`

```text
local-stt 0.1.0 — IDLE
  engine     READY   whisper.cpp small-q5_1 @127.0.0.1:8178 (t=4)
  hotkeys    OK      PTT=Control_R  continuous=Shift+Control_R
  audio      default (closed)
  pipeline   0 queued, last: 3.8 s audio → 1.6 s (RTF 0.42) 2 min ago
  uptime     2 h 14 min
```

### `local-stt status --json`

```json
{
  "version": "0.1.0",
  "state": "LISTENING",
  "mode": "CONTINUOUS",
  "speech": true,
  "engine": {"state": "READY", "name": "whisper.cpp", "model": "small-q5_1", "port": 8178},
  "hotkeys": {"state": "OK", "problems": []},
  "audio": {"device": "default", "open": true, "overflows": 0},
  "pipeline": {"queued": 1, "queued_audio_s": 3.4, "busy": true, "generation": 5},
  "stats": {"jobs_ok": 42, "jobs_failed": 0, "jobs_filtered": 3, "rtf_avg_10": 0.44, "latency_avg_10_s": 1.7},
  "uptime_s": 8040
}
```

`state` jest wyliczany według priorytetów z [04](04-maszyna-stanow.md) §4.7.

`status --watch` subskrybuje zdarzenia i przepisuje jedną linię w terminalu. Można go użyć w pasku stanu (np. rozszerzenie GNOME „Executor” albo przyszły tray, zob. [15](15-plan-implementacji.md)).

## 10.5 `local-stt doctor`

Sprawdza i wypisuje `OK` / `WARN` / `FAIL` z podpowiedzią naprawy:

| Test | FAIL/WARN, gdy | Podpowiedź |
|---|---|---|
| sesja | `XDG_SESSION_TYPE != x11` | „Wybierz sesję Ubuntu on Xorg na ekranie logowania” |
| `DISPLAY` w `systemctl --user show-environment` | brak | `dbus-update-activation-environment --systemd DISPLAY XAUTHORITY` |
| config | błąd walidacji | komunikat walidatora |
| model STT | brak pliku / zła suma | `local-stt models pull …` |
| model VAD | brak/zła suma | `local-stt models pull silero-vad` |
| `whisper-server` binarka | brak / nie uruchamia się (`--help`) / `.whisper-tag` ≠ tag z `install.sh` | `scripts/install.sh --rebuild-whisper` |
| `secret`, `whisper-server.env` | brak / uprawnienia inne niż 0600 | `scripts/install.sh` |
| usługa `local-stt-whisper` | nieaktywna | `systemctl --user status local-stt-whisper` |
| `GET /health` | brak odpowiedzi / `loading model` > 60 s | `journalctl --user -u local-stt-whisper` |
| port | nasłuch nie tylko na loopback (`ss -ltn`) | FAIL prywatności |
| hotkeye | daemon działa → stan `hotkeys` przez IPC (`degraded` = FAIL z listą problemów); daemon nie działa → grab testowy na osobnym połączeniu (`BadAccess` = FAIL) | wskazuje konfliktujący skrót GNOME (`gsettings list-recursively` + dopasowanie) |
| mikrofon | otwarcie 1 s → RMS | WARN, gdy < -60 dBFS: „sprawdź wyciszenie/poziom wejścia w ustawieniach dźwięku” |
| `xdotool` | brak | WARN: backend `type` niedostępny |
| `pw-play` / `paplay` | brak obu | WARN: brak dźwięków |
| `notify-send` | brak | WARN: brak powiadomień |
| CPU governor / zasilanie | `powersave` na baterii | INFO: wpływ na latencję |

## 10.6 Sygnalizacja dla użytkownika

### Dźwięki (`feedback.sounds`)

Daemon przy starcie generuje cztery krótkie pliki WAV (sinus z 5 ms fade-in/out, głośność `sound_volume`) do `$XDG_RUNTIME_DIR/local-stt/sounds/`:

| Dźwięk | Brzmienie | Długość |
|---|---|---|
| `start` | 2 tony rosnące 660→880 Hz | 130 ms |
| `stop` | 2 tony opadające 880→660 Hz | 130 ms |
| `cancel` | 1 ton 440 Hz | 120 ms |
| `error` | 3× 330 Hz z przerwami | 250 ms |

**Macierz zdarzeń → dźwięk** (jedyne źródło prawdy; [04](04-maszyna-stanow.md) i [05](05-audio-i-vad.md) się do niej odwołują):

| Sytuacja | Dźwięk |
|---|---|
| PTT: pierwsza ramka z mikrofonu (`RecordingStarted`); próbki do końca dźwięku + 80 ms są odrzucane | `start` |
| PTT puszczone, nagranie przyjęte do kolejki | `stop` |
| PTT krótsze niż `ptt.min_duration_ms` | **brak** (przypadkowe tapnięcie) |
| PTT anulowane (klawisz anulowania, `local-stt cancel`) | `cancel` |
| Nagranie PTT bez mowy (`JobDiscarded(no_speech)`) | `cancel` |
| Continuous włączony (dźwięk **przed** otwarciem mikrofonu) | `start` |
| Continuous wyłączony (toggle, backlog, silnik DOWN, mikrofon po 3 próbach) | `stop`; dla backlog/DOWN/mikrofonu dodatkowo `error` |
| `local-stt cancel` z odrzuceniem czegokolwiek | `cancel` |
| Odmowa: silnik niedostępny, błąd otwarcia mikrofonu, błąd audio w trakcie PTT | `error` |
| Tekst wpisany | brak (efekt widać w oknie) |
| `JobFailed`, wklejenie niepotwierdzone | brak dźwięku, tylko powiadomienie |

Odtwarzanie: `subprocess.Popen(["pw-play", path])` (fallback `paplay`), bez czekania na zakończenie. Osobny proces nie koliduje ze strumieniem wejściowym PortAudio. Wyjątek: przy starcie continuous controller planuje `capture.open()` timerem po długości dźwięku, żeby nie blokować.

### Powiadomienia (`feedback.notifications`)

`notify-send -a local-stt -i audio-input-microphone -p [-r <id>] [-e] "<tytuł>" "<treść>"` (libnotify-bin 0.8.3 na Ubuntu 24.04):

- `-p` wypisuje ID powiadomienia, a daemon je zapamiętuje. Kolejne wywołanie z `-r <id>` **zastępuje** poprzednie, więc powiadomienia się nie mnożą. GNOME Shell ignoruje wskazówkę `x-canonical-private-synchronous` i `-t`, dlatego ich nie używamy.
- `-e` (transient) dla powiadomień informacyjnych (poziom `all`), żeby nie zostawały w centrum powiadomień. Błędy nie są transient.

| Poziom `errors` (domyślny) | Dodatkowo przy `all` |
|---|---|
| silnik niedostępny, błąd mikrofonu, „mikrofon wydaje się wyciszony”, „osiągnięto limit nagrania”, transkrypcja nie powiodła się (zbiorczo), „nie udało się wkleić — tekst w schowku”, „brak aktywnego pola — tekst w schowku”, „transkrypcja nie nadąża — dyktowanie zatrzymane”, konflikt hotkeya przy starcie, nieudany restart silnika po reload | „Dyktowanie włączone/wyłączone”, „Silnik gotowy: <model>” |

Powiadomienia **nigdy nie zawierają transkrybowanego tekstu**.
