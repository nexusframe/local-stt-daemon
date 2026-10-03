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
    ├── ggml-small-q8_0.bin
    └── silero_vad.onnx
~/.local/bin/local-stt  →  ~/.local/share/local-stt/venv/bin/local-stt   (symlink)
~/.config/local-stt/
├── config.toml
├── secret                        # 0600, server --request-path prefix
└── whisper-server.env            # 0600, generated from config.toml
~/.config/systemd/user/
├── local-stt.service
└── local-stt-whisper.service
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
| `onnxruntime` | Silero VAD | CPU wheel, no torch |
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
 6. models: local-stt models pull $MODEL (default: small-q8_0) and silero-vad; verify SHA256.
 7. config: if ~/.config/local-stt/config.toml is absent → copy config.example.toml (with the model substituted).
    If ~/.config/local-stt/secret is absent → generate it (umask 077, 32 hex characters).
    Generate whisper-server.env.
 8. systemd: copy both units, systemctl --user daemon-reload,
    (without --no-enable) systemctl --user enable local-stt-whisper.service local-stt.service,
    then restart both (on first installation: start) so that the new code and new env take effect
 9. local-stt doctor — result shown at the end of installation.
```

`scripts/uninstall.sh [--purge]`:

- always: `disable --now` both services and remove the units, symlink, and venv,
- `--purge`: also remove `~/.local/share/local-stt` (models, whisper.cpp) and `~/.config/local-stt`.

Without `--purge`, models and config are retained because downloading or recreating them is costly.

## 11.4 `local-stt-whisper.service`

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

- **`Wants=`, not `Requires=`.** A server restart or failure must not kill the daemon. The daemon handles `engine=DOWN` itself ([04](04-state-machine.md) §4.5).
- **`Type=notify`.** The daemon sends `READY=1` through `$NOTIFY_SOCKET` (a few lines using a raw `AF_UNIX` socket, with no `systemd-python` dependency) when the config is loaded, the IPC socket is listening, and hotkeys have either been grabbed or reported as `degraded`. It does **not** wait for the engine because model loading may take time, and the status reflects this. It also sends `STATUS=<state>` on state changes, so `systemctl --user status local-stt` shows, for example, `Status: "LISTENING (1 queued)"`.
- **`RestartPreventExitStatus=78`.** A configuration error or non-X11 session is not restarted in a loop.
- **`DISPLAY` and `XAUTHORITY`.** On Ubuntu 24.04 with GNOME Xorg, these are imported into the user manager automatically. Verified: `systemctl --user show-environment` contains `DISPLAY=:1` and `XAUTHORITY=/run/user/1000/gdm/Xauthority`. The `DISPLAY` number may change between logins, so the unit does **not** hard-code it.
- **Audio.** `XDG_RUNTIME_DIR` is always set, and the `pipewire-pulse` socket runs in the same user session.
- **End of the X session.** The X11 connection is severed → exit code 0 with no restart ([07](07-hotkeys-x11.md) §7.4) → `PartOf` stops the unit with the target, and systemd starts it with the new environment after the next login.
- **Residue from the previous session.** If the user logs into a Wayland session after logging out of X11, stale `DISPLAY`/`XAUTHORITY` values may remain in the manager environment. The daemon determines the session type through `loginctl show-user $UID -p Display` + `loginctl show-session <id> -p Type` ([07](07-hotkeys-x11.md) §7.5; `XDG_SESSION_ID` does not exist in the user-service environment — verified) and exits with code 78 outside X11.

## 11.6 Operations

```bash
systemctl --user status local-stt local-stt-whisper
journalctl --user -u local-stt -f                   # daemon logs
journalctl --user -u local-stt -p warning           # warnings and errors only (sd-daemon priorities)
systemctl --user reload local-stt                   # = local-stt reload
systemctl --user restart local-stt-whisper          # manually (reload does this for ⟳ changes)
systemctl --user stop local-stt                     # disable for this session
systemctl --user disable --now local-stt local-stt-whisper   # disable autostart
local-stt daemon --log-level DEBUG                  # manually, in foreground (first: systemctl --user stop local-stt)
```

## 11.7 Updating

- Code: `git pull && scripts/install.sh` — rebuilds the venv. Both services are restarted as specified in installation step 8 to apply the generated env; only the whisper.cpp build is skipped if the tag has not changed and `--rebuild-whisper` was not specified.
- whisper.cpp: `scripts/install.sh --whisper-tag vX.Y.Z --rebuild-whisper`, followed by `local-stt bench --quick` to compare with the previous result stored in `~/.local/share/local-stt/bench/`.
