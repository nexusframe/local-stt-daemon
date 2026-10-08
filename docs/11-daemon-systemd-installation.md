# 11. Daemon, systemd, and installation

## 11.1 File layout after installation

```text
~/.local/share/local-stt/
├── venv/                         # virtualenv with the local_stt package
├── src/whisper.cpp/              # v1.9.4 tag checkout + build/
├── bin/whisper-server            # copied binaries
├── bin/whisper-cli
├── bin/whisper-bench
├── bin/.whisper-tag              # tag from which the binaries were built
└── models/
    ├── parakeet-tdt-0.6b-v3-int8/   # default engine (v0.4): 4 files, 639 MiB
    ├── ggml-small-q8_0.bin          # whisper-server model (--model)
    └── silero_vad.onnx
~/.local/bin/local-stt  →  ~/.local/share/local-stt/venv/bin/local-stt   (symlink)
~/.config/local-stt/
├── config.toml
├── secret                        # 0600, server --request-path prefix
└── whisper-server.env            # 0600, generated from config.toml
~/.config/systemd/user/
├── local-stt.service
├── local-stt-whisper.service
└── local-stt-engine.service      # Parakeet engine server (v0.4)
$XDG_RUNTIME_DIR/local-stt/       # created at runtime: control.sock, sounds/
```

Nothing is installed outside `$HOME` except apt packages.

## 11.2 Dependencies

### System dependencies (apt)

| Package | Purpose |
|---|---|
| `build-essential`, `cmake`, `git` | building whisper.cpp |
| `python3-venv` | virtualenv (Ubuntu does not provide `ensurepip` without this package — verified) |
| `libportaudio2` | `sounddevice` |
| `xdotool` | `type` backend (already installed on the reference machine) |
| `libnotify-bin` | `notify-send` (already installed) |
| `pipewire-bin` | `pw-play` (already installed) |
| `pulseaudio-utils` | `pactl` — source listing and stream connection verification (already installed) |
| `xvfb` | `--dev` only: `needs_x11`/`e2e` tests ([14](14-tests.md)) |

### Python (`pyproject.toml`)

| Package | Purpose | Notes |
|---|---|---|
| `numpy` | audio buffers | |
| `sounddevice` | capture | requires `libportaudio2` |
| `onnxruntime` | Silero VAD, Parakeet | CPU wheel, no torch |
| `onnx-asr` | Parakeet engine server (v0.4, task 4.2) | pinned 0.12.0 in `requirements.lock` |
| `python-xlib` | hotkeys, clipboard, XTest | pure Python |
| `soxr` | fallback resampling | wheel |

Versions are pinned in `requirements.lock` (`pip-compile --generate-hashes`), and installation uses `pip install --require-hashes -r requirements.lock` + `pip install --no-deps .`. Server HTTP, TOML, IPC, and logging use only the standard library.

## 11.3 `scripts/install.sh`

The script is idempotent: every step checks whether it has already been completed. It runs as a regular user and invokes `sudo` only for `apt`.

```text
install.sh [--model NAME] [--rebuild-whisper] [--whisper-tag TAG] [--dev] [--no-apt] [--no-enable]

Default tag: v1.9.4.

 1. Check: Ubuntu 24.04, XDG_SESSION_TYPE=x11 (WARN, not FAIL — installation over SSH is allowed), CPU supports AVX2.
 2. apt: sudo apt install -y build-essential cmake git python3-venv libportaudio2 xdotool libnotify-bin pipewire-bin pulseaudio-utils
    (--dev: xvfb as well)
 3. whisper.cpp: git clone --depth 1 --branch $TAG → cmake → build (whisper-server, whisper-cli, whisper-bench) → install into bin/.
    Skipped when bin/.whisper-tag contains the same tag and --rebuild-whisper was not specified (the server has no --version flag).
 4. venv: python3 -m venv; pip install --require-hashes -r requirements.lock; pip install --no-deps . (--dev: -e .[dev])
 5. symlink ~/.local/bin/local-stt
 6. models: local-stt models pull parakeet-tdt-0.6b-v3-int8 (the default engine, task 4.5), $MODEL
    (the whisper-server model; default: small-q8_0) and silero-vad; verify SHA256.
 7. config: if ~/.config/local-stt/config.toml is absent → copy config.example.toml (with the model substituted).
    If ~/.config/local-stt/secret is absent → generate it (umask 077, 32 hex characters).
    Generate whisper-server.env.
 8. systemd: copy the three units, systemctl --user daemon-reload,
    (without --no-enable) disable both engine units, enable local-stt.service,
    try-restart the engine units (the running one gets the new code and env),
    then restart local-stt.service, which starts the engine stt.engine selects (task 4.3)
 9. local-stt doctor — result shown at the end of installation.
```

*Implementation (task 1.14; user decision 2026-10-03 for `--no-enable`).* The default `--model` is `small-q8_0` (the script had `small-q5_1`, contradicting this list and the config default). `--model` must match `[A-Za-z0-9._-]+`. Step 7 substitutes the model into the first `model = "…"` line of `config.example.toml` (that line is `stt.model`; the VAD model comes later), keeps an existing `config.toml` unchanged, and generates `whisper-server.env` with the venv's Python through `local_stt.config` (an invalid config stops the installation with the validator messages). Step 8 installs the units from `systemd/` and replaces `%h/projects/local-stt-daemon` in `Documentation=` with the actual checkout. With `--no-enable`, nothing is enabled, and only units that are already running are restarted (`systemctl --user try-restart`). Otherwise step 8 runs as listed above. Since task 4.3, it enables only `local-stt.service`, and the daemon starts the engine unit. A failed start is a WARNING, so step 9 still runs `doctor`, which shows the cause; a `doctor` FAIL is also only a WARNING. `uninstall.sh` removes `~/.local/bin/local-stt` only if it is our symlink to the venv, and also removes `$XDG_RUNTIME_DIR/local-stt`. **Tested on the reference machine 2026-10-03:** a full install ends with `doctor` 0 FAIL / 0 WARN; a rerun is idempotent (config kept); `reload` with `stt.threads` 4 → 8 → 4 rewrites the env file and restarts the server through `systemctl` (the restart was previously untested); `systemctl --user reload local-stt` reloads; `kill -9` on the daemon → restarted (`NRestarts=1`); an invalid config → `status=78/CONFIG`, not restarted; `uninstall.sh` without `--purge` keeps models, the whisper.cpp build and the config, and a reinstall works.

`scripts/uninstall.sh [--purge]`:

- always: `disable --now` the three units (`local-stt`, `local-stt-whisper`, `local-stt-engine`) and remove the units, symlink, and venv,
- `--purge`: also remove `~/.local/share/local-stt` (models, whisper.cpp) and `~/.config/local-stt`.

Without `--purge`, models and config are retained because downloading or recreating them is costly.

## 11.4 Engine units

Two units serve the two engines. They share `stt.port`, so only one runs at a time. Neither unit is enabled: the daemon starts the unit that `stt.engine` selects (§11.5).

### `local-stt-whisper.service`

```ini
[Unit]
Description=local-stt: whisper.cpp inference server (loopback only)
Documentation=file://%h/projects/local-stt-daemon/docs/06-stt-engine.md
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

- **`PartOf`/`WantedBy=graphical-session.target`.** The server starts after login and ends with the session, so the model (~0.3–1 GB RAM) does not remain in memory after logout.
- **`Nice=5`.** Under full CPU load, the desktop and the applications receiving dictation remain responsive. The tradeoff is slightly higher latency while other processes are running concurrently. The temporary benchmark server runs under `nice -n 5`, so its measurements reflect this configuration.
- **`MemoryMax=2G`.** Protects the system from a config mistake (for example, `large-v3` f16). The limit will be exceeded and the server will terminate with a clear log instead of forcing the system into swap. We do not set `MemoryHigh`, because memory throttling would distort latency rather than cap it.
- **`$LOCAL_STT_WHISPER_ARGS` without braces.** systemd splits the value on whitespace into separate arguments. Paths under `$HOME` therefore cannot contain spaces; `install.sh` checks this.
- `whisper-server` listens on TCP, so `PrivateNetwork=` cannot be used: the daemon would be unable to access it. Loopback-only access is enforced by hard-coding host `127.0.0.1` in the `whisper-server.env` generator and verifying it in `doctor`.

### `local-stt-engine.service` (v0.4, tasks 4.2–4.5)

```ini
[Unit]
Description=local-stt: Parakeet inference server (loopback only)
Documentation=file://%h/projects/local-stt-daemon/docs/06-stt-engine.md
PartOf=graphical-session.target
After=graphical-session.target
StartLimitIntervalSec=120
StartLimitBurst=5

[Service]
# READY=1 is sent once the model is loaded and the socket listens (~3 s).
Type=notify
ExecStart=%h/.local/share/local-stt/venv/bin/local-stt engine-server
Restart=on-failure
RestartSec=2
# 78 = config error, missing secret or model: a restart cannot fix it.
RestartPreventExitStatus=78
Environment=PYTHONUNBUFFERED=1
Nice=5
# Parakeet int8 via onnx-asr peaks at ~2.1 GB RSS for 120 s of audio (task 4.8).
MemoryMax=3000M
NoNewPrivileges=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
LockPersonality=yes
LimitCORE=0

[Install]
WantedBy=graphical-session.target
```

- **Same package and venv as the daemon** (user decision 2026-10-07). The server reads `config.toml` and `secret` itself, so it needs no env file. Details: [06](06-stt-engine.md) §6.10.
- **`Type=notify`.** The server sends `READY=1` after the model loads and the socket listens. Thus `systemctl --user start` returns when the server can answer.
- **`RestartPreventExitStatus=78`.** A missing secret or model directory stops the restarts. `doctor` shows the cause.
- **`MemoryMax=3000M`.** This is above the measured peak (~2.1 GB for a 120 s recording) and N1 (2.2 GB). It stops a leak. The unit has no swap limit: at the old limit (2500M), 120 s recordings pushed the server into swap instead of a stop (task 4.8).
- **`Nice=5`** and the other settings are the same as for whisper-server.
- **Loopback only (N5).** The host `127.0.0.1` is hard-coded in `engine_server.py`. `doctor` checks the port.

## 11.5 `local-stt.service`

```ini
[Unit]
Description=local-stt: offline Polish voice-to-text daemon
Documentation=file://%h/projects/local-stt-daemon/docs/README.md
PartOf=graphical-session.target
After=graphical-session.target
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
# Guard against native leaks (05 §5.6: PortAudio xrun loop); N1 allows 150 MB
MemoryMax=1G

[Install]
WantedBy=graphical-session.target
```

- **`MemoryMax=1G`.** Added during the v0.2 acceptance (2026-10-04): a dead audio stream drove PortAudio into a leaking xrun loop that grew the daemon to 11.4 GB and triggered the kernel's global OOM killer, which may pick any process (05 §5.6). With the limit, the cgroup OOM killer ends only this unit and `Restart=on-failure` brings it back. Far above N1 (150 MB), so it never affects normal operation; the user manager delegates the `memory` controller on Ubuntu 24.04 (checked: `cpu memory pids`).
- **No dependency on an engine unit** (task 4.3, user decision 2026-10-07; until then `Wants=local-stt-whisper.service`). `local-stt-whisper.service` and `local-stt-engine.service` (Parakeet) share `stt.port`, so only one may run. At startup, in a helper thread, the daemon stops the unit of the other engine and starts the one `stt.engine` selects (`systemctl --user start` keeps a running server); a server-restart reload stops the other and restarts the selected one ([04](04-state-machine.md) §4.6). Neither engine unit is enabled. A server restart or failure still never kills the daemon, which handles `engine=DOWN` itself ([04](04-state-machine.md) §4.5).
- **`Type=notify`.** The daemon sends `READY=1` through `$NOTIFY_SOCKET` (a few lines using a raw `AF_UNIX` socket, with no `systemd-python` dependency) when the config is loaded, the IPC socket is listening, and hotkeys have either been grabbed or reported as `degraded`. It does **not** wait for the engine because model loading may take time, and the status reflects this. It also sends `STOPPING=1` at shutdown and `STATUS=<state>` on state changes, so `systemctl --user status local-stt` shows, for example, `Status: "LISTENING (1 queued)"`. The daemon removes `NOTIFY_SOCKET` from its environment after reading it (like `sd_notify`'s `unset_environment`). Tested 2026-10-03: otherwise every `loginctl`/`systemctl` child sends `EXIT_STATUS=0` to the socket (systemd 255), and systemd logs a WARNING `Got notification message from PID …, but reception only permitted for main PID` for each one.
- **`RestartPreventExitStatus=78`.** A configuration error or non-X11 session is not restarted in a loop.
- **`DISPLAY` and `XAUTHORITY`.** On Ubuntu 24.04 with GNOME Xorg, these are imported into the user manager automatically. Verified: `systemctl --user show-environment` contains `DISPLAY=:1` and `XAUTHORITY=/run/user/1000/gdm/Xauthority`. The `DISPLAY` number may change between logins, so the unit does **not** hard-code it.
- **Audio.** `XDG_RUNTIME_DIR` is always set, and the `pipewire-pulse` socket runs in the same user session.
- **End of the X session.** The X11 connection is severed → exit code 0 with no restart ([07](07-hotkeys-x11.md) §7.4) → `PartOf` stops the unit with the target, and systemd starts it with the new environment after the next login.
- **Residue from the previous session.** If the user logs into a Wayland session after logging out of X11, stale `DISPLAY`/`XAUTHORITY` values may remain in the manager environment. The daemon determines the session type through `loginctl show-user $UID -p Display` + `loginctl show-session <id> -p Type` ([07](07-hotkeys-x11.md) §7.5; `XDG_SESSION_ID` does not exist in the user-service environment — verified) and exits with code 78 outside X11.

## 11.6 Operations

```bash
systemctl --user status local-stt local-stt-engine   # or local-stt-whisper
journalctl --user -u local-stt -f                   # daemon logs
journalctl --user -u local-stt -p warning           # warnings and errors only (sd-daemon priorities)
systemctl --user reload local-stt                   # = local-stt reload
systemctl --user restart local-stt-engine           # manually (reload does this for ⟳ changes)
systemctl --user stop local-stt                     # disable for this session
systemctl --user disable --now local-stt            # disable autostart (the engine units are not enabled)
local-stt daemon --log-level DEBUG                  # manually, in foreground (first: systemctl --user stop local-stt)
```

## 11.7 Updating

- Code: `git pull && scripts/install.sh` — rebuilds the venv. Installation step 8 restarts the services, so they use the new code and the generated env; only the whisper.cpp build is skipped if the tag has not changed and `--rebuild-whisper` was not specified.
- whisper.cpp: `scripts/install.sh --whisper-tag vX.Y.Z --rebuild-whisper`, followed by `local-stt bench --quick` to compare with the previous result stored in `~/.local/share/local-stt/bench/`.
