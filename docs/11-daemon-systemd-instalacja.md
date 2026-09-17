# 11. Daemon, systemd i instalacja

## 11.1 Układ plików po instalacji

```text
~/.local/share/local-stt/
├── venv/                         # virtualenv z pakietem local_stt
├── src/whisper.cpp/              # checkout tagu v1.9.4 + build/
├── bin/whisper-server            # skopiowane binarki
├── bin/whisper-cli
├── bin/whisper-bench
├── bin/.whisper-tag              # tag, z którego zbudowano binarki
└── models/
    ├── ggml-small-q5_1.bin
    └── silero_vad.onnx
~/.local/bin/local-stt  →  ~/.local/share/local-stt/venv/bin/local-stt   (symlink)
~/.config/local-stt/
├── config.toml
├── secret                        # 0600, prefiks --request-path serwera
└── whisper-server.env            # 0600, generowany z config.toml
~/.config/systemd/user/
├── local-stt.service
└── local-stt-whisper.service
$XDG_RUNTIME_DIR/local-stt/       # tworzony w runtime: control.sock, sounds/
```

Nic nie trafia poza `$HOME` z wyjątkiem pakietów apt.

## 11.2 Zależności

### Systemowe (apt)

| Pakiet | Po co |
|---|---|
| `build-essential`, `cmake`, `git` | budowa whisper.cpp |
| `python3-venv` | virtualenv (Ubuntu nie ma `ensurepip` bez tego pakietu — zweryfikowane) |
| `libportaudio2` | `sounddevice` |
| `xdotool` | backend `type` (już zainstalowany na maszynie referencyjnej) |
| `libnotify-bin` | `notify-send` (już zainstalowany) |
| `pipewire-bin` | `pw-play` (już zainstalowany) |
| `pulseaudio-utils` | `pactl` — lista źródeł i weryfikacja podłączenia strumienia (już zainstalowany) |
| `xvfb` | tylko `--dev`: testy `needs_x11`/`e2e` ([14](14-testy.md)) |

### Python (`pyproject.toml`)

| Pakiet | Po co | Uwagi |
|---|---|---|
| `numpy` | bufory audio | |
| `sounddevice` | capture | wymaga `libportaudio2` |
| `onnxruntime` | Silero VAD | wheel CPU, bez torch |
| `python-xlib` | hotkeye, schowek, XTest | czysty Python |
| `soxr` | resampling awaryjny | wheel |

Wersje przypięte w `requirements.lock` (`pip-compile --generate-hashes`), a instalacja idzie przez `pip install --require-hashes -r requirements.lock` + `pip install --no-deps .`. HTTP do serwera, TOML, IPC i logowanie korzystają wyłącznie z biblioteki standardowej.

## 11.3 `scripts/install.sh`

Skrypt jest idempotentny: każdy krok sprawdza, czy jest już wykonany. Uruchamiany jako zwykły użytkownik, a `sudo` wywołuje tylko dla `apt`.

```text
install.sh [--model NAME] [--rebuild-whisper] [--whisper-tag TAG] [--dev] [--no-apt] [--no-enable]

Domyślny tag: v1.9.4.

 1. Sprawdź: Ubuntu 24.04, XDG_SESSION_TYPE=x11 (WARN, nie FAIL — można instalować z SSH), CPU ma AVX2.
 2. apt: sudo apt install -y build-essential cmake git python3-venv libportaudio2 xdotool libnotify-bin pipewire-bin pulseaudio-utils
    (--dev: dodatkowo xvfb)
 3. whisper.cpp: git clone --depth 1 --branch $TAG → cmake → build (whisper-server, whisper-cli, whisper-bench) → install do bin/.
    Pomijane, gdy bin/.whisper-tag zawiera ten sam tag i nie podano --rebuild-whisper (serwer nie ma flagi --version).
 4. venv: python3 -m venv; pip install --require-hashes -r requirements.lock; pip install --no-deps . (--dev: -e .[dev])
 5. symlink ~/.local/bin/local-stt
 6. modele: local-stt models pull $MODEL (domyślnie small-q5_1) i silero-vad; weryfikacja SHA256.
 7. config: jeśli brak ~/.config/local-stt/config.toml → skopiuj config.example.toml (z podmienionym modelem).
    Jeśli brak ~/.config/local-stt/secret → wygeneruj (umask 077, 32 znaki hex).
    Wygeneruj whisper-server.env.
 8. systemd: skopiuj oba unit-y, systemctl --user daemon-reload,
    (bez --no-enable) systemctl --user enable local-stt-whisper.service local-stt.service,
    a następnie restart obu (przy pierwszej instalacji: start), żeby zadziałał nowy kod i nowy env
 9. local-stt doctor — wynik na końcu instalacji.
```

`scripts/uninstall.sh [--purge]`:

- zawsze: `disable --now` obu usług, usunięcie unitów, symlinku i venv,
- `--purge`: także `~/.local/share/local-stt` (modele, whisper.cpp) i `~/.config/local-stt`.

Bez `--purge` modele i config zostają, bo ich pobranie lub odtworzenie jest kosztowne.

## 11.4 `local-stt-whisper.service`

```ini
[Unit]
Description=local-stt: whisper.cpp inference server (loopback only)
Documentation=file://%h/projects/local-stt-daemon/docs/06-silnik-stt.md
PartOf=graphical-session.target
After=graphical-session.target
StartLimitIntervalSec=120
StartLimitBurst=5

[Service]
Type=simple
EnvironmentFile=%h/.config/local-stt/whisper-server.env
ExecStart=%h/.local/share/local-stt/bin/whisper-server $LOCAL_STT_WHISPER_ARGS
Restart=on-failure
RestartSec=2
Nice=5
MemoryMax=2G
NoNewPrivileges=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
LockPersonality=yes
LimitCORE=0

[Install]
WantedBy=graphical-session.target
```

- **`PartOf`/`WantedBy=graphical-session.target`.** Serwer startuje po zalogowaniu i kończy się z sesją, więc model (~0,3–1 GB RAM) nie zostaje w pamięci po wylogowaniu.
- **`Nice=5`.** Przy pełnym obciążeniu CPU pulpit i aplikacje, do których dyktujemy, pozostają responsywne. Cena to nieco wyższa latencja przy równoległej pracy innych procesów. Tymczasowy serwer benchmarku działa pod `nice -n 5`, więc pomiar odpowiada tej konfiguracji.
- **`MemoryMax=2G`.** Chroni system przed pomyłką w configu (np. `large-v3` f16). Limit zostanie przekroczony i serwer zginie z czytelnym logiem, zamiast wypychać system do swapu. Nie ustawiamy `MemoryHigh`, bo dławienie pamięci zafałszowałoby latencję zamiast ją ograniczyć.
- **`$LOCAL_STT_WHISPER_ARGS` bez klamer.** systemd dzieli wartość po białych znakach na osobne argumenty. Ścieżki w `$HOME` nie mogą więc zawierać spacji — `install.sh` to sprawdza.
- `whisper-server` nasłuchuje na TCP, więc `PrivateNetwork=` odpada: daemon nie miałby do niego dostępu. Ograniczenie do loopbacku wynika z tego, że host `127.0.0.1` jest wpisany na sztywno w generatorze `whisper-server.env`, a `doctor` je weryfikuje.

## 11.5 `local-stt.service`

```ini
[Unit]
Description=local-stt: offline Polish voice-to-text daemon
Documentation=file://%h/projects/local-stt-daemon/docs/README.md
PartOf=graphical-session.target
After=graphical-session.target local-stt-whisper.service
Wants=local-stt-whisper.service
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=notify
ExecStart=%h/.local/share/local-stt/venv/bin/local-stt daemon
ExecReload=/bin/kill -HUP $MAINPID
Restart=on-failure
RestartSec=2
RestartPreventExitStatus=78
Environment=PYTHONUNBUFFERED=1
NoNewPrivileges=yes
LimitCORE=0

[Install]
WantedBy=graphical-session.target
```

- **`Wants=`, a nie `Requires=`.** Restart lub awaria serwera nie może zabić daemona. Daemon obsługuje `engine=DOWN` sam ([04](04-maszyna-stanow.md) §4.5).
- **`Type=notify`.** Daemon wysyła `READY=1` przez `$NOTIFY_SOCKET` (kilka linii na gołym gnieździe `AF_UNIX`, bez zależności `systemd-python`), gdy: config jest wczytany, gniazdo IPC nasłuchuje, a hotkeye są zgrabowane lub zgłoszone jako `degraded`. **Nie** czeka na silnik, bo ładowanie modelu może trwać, a status to pokazuje. Przy zmianach stanu wysyła też `STATUS=<state>`, więc `systemctl --user status local-stt` pokazuje np. `Status: "LISTENING (1 queued)"`.
- **`RestartPreventExitStatus=78`.** Błąd konfiguracji albo sesja nie-X11 nie jest restartowana w pętli.
- **`DISPLAY` i `XAUTHORITY`.** Na Ubuntu 24.04 GNOME Xorg są importowane do menedżera użytkownika automatycznie. Zweryfikowano: `systemctl --user show-environment` zawiera `DISPLAY=:1` i `XAUTHORITY=/run/user/1000/gdm/Xauthority`. Numer `DISPLAY` może się zmieniać między logowaniami, dlatego unit **nie** ustawia go na sztywno.
- **Audio.** `XDG_RUNTIME_DIR` jest zawsze ustawiony, a gniazdo `pipewire-pulse` działa w tej samej sesji użytkownika.
- **Zakończenie sesji X.** Połączenie X11 zostaje zerwane → wyjście z kodem 0, bez restartu ([07](07-hotkeys-x11.md) §7.4) → `PartOf` zatrzymuje unit razem z targetem, a po następnym logowaniu systemd startuje go z nowym środowiskiem.
- **Pozostałości po poprzedniej sesji.** Jeśli po wylogowaniu z X11 użytkownik zaloguje się do sesji Wayland, w środowisku menedżera mogą zostać stare `DISPLAY`/`XAUTHORITY`. Daemon ustala typ sesji przez `loginctl show-user $UID -p Display` + `loginctl show-session <id> -p Type` ([07](07-hotkeys-x11.md) §7.5; `XDG_SESSION_ID` nie istnieje w środowisku usług użytkownika — zweryfikowane) i poza X11 kończy się kodem 78.

## 11.6 Operacje

```bash
systemctl --user status local-stt local-stt-whisper
journalctl --user -u local-stt -f                   # logi daemona
journalctl --user -u local-stt -p warning           # tylko ostrzeżenia i błędy (priorytety sd-daemon)
systemctl --user reload local-stt                   # = local-stt reload
systemctl --user restart local-stt-whisper          # ręcznie (reload robi to sam przy zmianach ⟳)
systemctl --user stop local-stt                     # wyłącz na tę sesję
systemctl --user disable --now local-stt local-stt-whisper   # wyłącz autostart
local-stt daemon --log-level DEBUG                  # ręcznie, na pierwszym planie (najpierw: systemctl --user stop local-stt)
```

## 11.7 Aktualizacja

- Kod: `git pull && scripts/install.sh` — przebudowuje venv i restartuje `local-stt.service`. Serwer zostaje, jeśli tag whisper.cpp się nie zmienił.
- whisper.cpp: `scripts/install.sh --whisper-tag vX.Y.Z --rebuild-whisper`, a następnie `local-stt bench --quick`, żeby porównać z poprzednim wynikiem zapisanym w `~/.local/share/local-stt/bench/`.
