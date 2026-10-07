#!/usr/bin/env bash
# local-stt installer (docs/11-daemon-systemd-installation.md §11.3).
# Idempotent: every step checks whether it has already been completed.
# Runs as a regular user; sudo is used only for apt.
#
set -euo pipefail

readonly DEFAULT_WHISPER_TAG="v1.9.4"
readonly WHISPER_REPO="https://github.com/ggml-org/whisper.cpp"
readonly APT_PACKAGES=(build-essential cmake git python3-venv libportaudio2 xdotool
                       libnotify-bin pipewire-bin pulseaudio-utils)
readonly APT_DEV_PACKAGES=(xvfb)

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly REPO_DIR
readonly DATA_DIR="$HOME/.local/share/local-stt"
readonly CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/local-stt"
readonly WHISPER_SRC="$DATA_DIR/src/whisper.cpp"
readonly BIN_DIR="$DATA_DIR/bin"
readonly VENV_DIR="$DATA_DIR/venv"
readonly LOCAL_STT="$VENV_DIR/bin/local-stt"
readonly BIN_LINK="$HOME/.local/bin/local-stt"
readonly UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
readonly UNITS=(local-stt-whisper.service local-stt-engine.service local-stt.service)
# Engine services are never enabled: the daemon starts the one stt.engine selects (task 4.3).
readonly ENGINE_UNITS=(local-stt-whisper.service local-stt-engine.service)

MODEL="small-q8_0"
WHISPER_TAG="$DEFAULT_WHISPER_TAG"
REBUILD_WHISPER=0
DEV=0
NO_APT=0
NO_ENABLE=0

log()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mWARN:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<EOF
Usage: $(basename "$0") [--model NAME] [--rebuild-whisper] [--whisper-tag TAG] [--dev] [--no-apt] [--no-enable]

  --model NAME       STT model to download (default: $MODEL)
  --rebuild-whisper  rebuild whisper.cpp even if bin/.whisper-tag matches
  --whisper-tag TAG  whisper.cpp tag to build (default: $DEFAULT_WHISPER_TAG)
  --dev              also install xvfb and the package in editable mode with [dev] extras
  --no-apt           skip the apt step
  --no-enable        do not enable the systemd units
EOF
}

parse_args() {
    while (($#)); do
        case "$1" in
            --model)           MODEL="${2:?--model requires a value}"; shift ;;
            --whisper-tag)     WHISPER_TAG="${2:?--whisper-tag requires a value}"; shift ;;
            --rebuild-whisper) REBUILD_WHISPER=1 ;;
            --dev)             DEV=1 ;;
            --no-apt)          NO_APT=1 ;;
            --no-enable)       NO_ENABLE=1 ;;
            -h|--help)         usage; exit 0 ;;
            *)                 usage >&2; exit 2 ;;
        esac
        shift
    done
}

# 1. Environment checks
check_environment() {
    log "1/9 Checking environment"
    local os_id="" os_version=""
    if [[ -r /etc/os-release ]]; then
        # shellcheck disable=SC1091
        os_id="$(. /etc/os-release && echo "${ID:-}")"
        os_version="$(. /etc/os-release && echo "${VERSION_ID:-}")"
    fi
    [[ "$os_id" == "ubuntu" && "$os_version" == "24.04" ]] \
        || warn "tested only on Ubuntu 24.04 (found: ${os_id:-unknown} ${os_version:-unknown})"

    [[ "${XDG_SESSION_TYPE:-}" == "x11" ]] \
        || warn "XDG_SESSION_TYPE is '${XDG_SESSION_TYPE:-unset}', the daemon requires an X11 session"

    # Everything installs into $HOME and runs as systemd user units
    ((EUID != 0)) || die "run as your regular user, not root or sudo (sudo is used only for apt)"

    grep -qw avx2 /proc/cpuinfo || die "CPU does not support AVX2"

    # systemd splits \$LOCAL_STT_WHISPER_ARGS on whitespace (§11.4)
    [[ "$HOME" != *[[:space:]]* ]] || die "\$HOME must not contain whitespace: '$HOME'"
}

# 2. System packages
install_apt_packages() {
    local packages=("${APT_PACKAGES[@]}")
    ((DEV)) && packages+=("${APT_DEV_PACKAGES[@]}")

    if ((NO_APT)); then
        log "2/9 Skipping apt (--no-apt)"
        return
    fi

    local missing=() pkg
    for pkg in "${packages[@]}"; do
        dpkg-query -W -f='${db:Status-Status}' "$pkg" 2>/dev/null | grep -qx installed \
            || missing+=("$pkg")
    done
    if ((${#missing[@]} == 0)); then
        log "2/9 System packages already installed"
        return
    fi
    log "2/9 Installing system packages: ${missing[*]}"
    sudo apt-get install -y "${missing[@]}"
}

# 3. whisper.cpp
build_whisper() {
    local tag_file="$BIN_DIR/.whisper-tag"
    if ((!REBUILD_WHISPER)) && [[ -x "$BIN_DIR/whisper-server" && -f "$tag_file" ]] \
        && [[ "$(<"$tag_file")" == "$WHISPER_TAG" ]]; then
        log "3/9 whisper.cpp $WHISPER_TAG already built"
        return
    fi

    log "3/9 Building whisper.cpp $WHISPER_TAG"
    mkdir -p "$(dirname "$WHISPER_SRC")" "$BIN_DIR"
    if [[ -d "$WHISPER_SRC/.git" ]]; then
        git -C "$WHISPER_SRC" fetch --depth 1 origin tag "$WHISPER_TAG"
        # -f: the checkout is owned by this script, and CMake configure rewrites tracked files
        # (bindings/javascript/package.json), which would otherwise block switching tags.
        git -C "$WHISPER_SRC" -c advice.detachedHead=false checkout -q -f "$WHISPER_TAG"
    else
        git -c advice.detachedHead=false clone --depth 1 --branch "$WHISPER_TAG" \
            "$WHISPER_REPO" "$WHISPER_SRC"
    fi

    # BUILD_SHARED_LIBS=OFF: binaries are copied out of build/ and must not depend on its .so files.
    # GGML_NATIVE=ON explicitly: ggml turns it off when SOURCE_DATE_EPOCH is set.
    cmake -S "$WHISPER_SRC" -B "$WHISPER_SRC/build" \
        -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_SHARED_LIBS=OFF \
        -DGGML_NATIVE=ON \
        -DWHISPER_BUILD_TESTS=OFF
    cmake --build "$WHISPER_SRC/build" -j"$(nproc)" --config Release \
        --target whisper-server whisper-cli whisper-bench

    local bin
    for bin in whisper-server whisper-cli whisper-bench; do
        install -m755 "$WHISPER_SRC/build/bin/$bin" "$BIN_DIR/$bin"
    done
    # whisper-server has no --version flag; remember the tag the binaries were built from.
    printf '%s\n' "$WHISPER_TAG" >"$tag_file"
}

# 4. Python virtualenv
install_venv() {
    log "4/9 Installing Python package into $VENV_DIR"
    [[ -x "$VENV_DIR/bin/python" ]] || python3 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install --quiet --require-hashes -r "$REPO_DIR/requirements.lock"
    if ((DEV)); then
        "$VENV_DIR/bin/pip" install --quiet -e "$REPO_DIR[dev]"
    else
        "$VENV_DIR/bin/pip" install --quiet --no-deps "$REPO_DIR"
    fi
}

# 5. Command on PATH
link_command() {
    log "5/9 Linking $BIN_LINK"
    mkdir -p "$(dirname "$BIN_LINK")"
    ln -sfn "$LOCAL_STT" "$BIN_LINK"
    case ":$PATH:" in
        *":$HOME/.local/bin:"*) ;;
        *) warn "~/.local/bin is not on PATH (it is added at the next login if the directory exists)" ;;
    esac
}

# 6. Models (verified against the pinned SHA256 by `models pull`)
pull_models() {
    log "6/9 Downloading models: $MODEL, silero-vad"
    "$LOCAL_STT" models pull "$MODEL"
    "$LOCAL_STT" models pull silero-vad
}

# 7. Config, secret (server --request-path, docs/09-configuration.md §9.4), whisper-server.env
install_config() {
    local config="$CONFIG_DIR/config.toml"
    mkdir -p "$CONFIG_DIR"
    if [[ -f "$config" ]]; then
        log "7/9 Keeping existing $config"
    else
        log "7/9 Creating $config (stt.model = $MODEL)"
        # the first `model = ...` line is stt.model; the VAD model comes later
        sed -E "0,/^model = \"[^\"]*\"/s//model = \"$MODEL\"/" \
            "$REPO_DIR/config.example.toml" >"$config"
    fi

    local secret_file="$CONFIG_DIR/secret"
    if [[ ! -s "$secret_file" ]]; then
        log "Generating $secret_file"
        (umask 077 && python3 -c 'import secrets; print(secrets.token_hex(16))' >"$secret_file")
    fi
    chmod 600 "$secret_file"

    log "Generating $CONFIG_DIR/whisper-server.env"
    "$VENV_DIR/bin/python" - <<'PY' || die "cannot generate whisper-server.env (see the errors above)"
import sys

from local_stt.config import (
    ConfigError, config_dir, load_config, render_whisper_env, write_whisper_env,
)
from local_stt.stt.whisper_server import read_request_path

try:
    config, _ = load_config()
except ConfigError as e:
    for error in e.errors:
        print(f"config error: {error}", file=sys.stderr)
    sys.exit(78)
directory = config_dir()
write_whisper_env(
    directory / "whisper-server.env",
    render_whisper_env(config.stt, read_request_path(directory / "secret")),
)
PY
}

# 8. systemd user units (docs/11 §11.4-11.5)
install_units() {
    log "8/9 Installing systemd units into $UNIT_DIR"
    mkdir -p "$UNIT_DIR"
    local unit
    for unit in "${UNITS[@]}"; do
        # Documentation= points at this checkout, wherever it is
        sed "s|%h/projects/local-stt-daemon|$REPO_DIR|" "$REPO_DIR/systemd/$unit" >"$UNIT_DIR/$unit"
        chmod 644 "$UNIT_DIR/$unit"
    done
    systemctl --user daemon-reload
    if ((NO_ENABLE)); then
        # only units already running pick up the new code and env (user decision 2026-10-03)
        systemctl --user try-restart "${UNITS[@]}"
        return
    fi
    # earlier versions enabled local-stt-whisper.service
    systemctl --user disable --quiet "${ENGINE_UNITS[@]}" 2>/dev/null || true
    systemctl --user enable local-stt.service
    # the running engine picks up the new code and env; the daemon starts the selected engine
    # if none runs. local-stt.service is Type=notify: restart returns once it sent READY=1
    systemctl --user try-restart "${ENGINE_UNITS[@]}"
    systemctl --user restart local-stt.service \
        || warn "local-stt.service failed to start: journalctl --user -u local-stt.service"
}

# 9. Diagnostics
run_doctor() {
    log "9/9 local-stt doctor"
    "$LOCAL_STT" doctor || warn "doctor reported FAIL (see above)"
}

main() {
    parse_args "$@"
    [[ "$MODEL" =~ ^[A-Za-z0-9._-]+$ ]] || die "invalid --model: '$MODEL'"
    check_environment
    install_apt_packages
    build_whisper
    "$BIN_DIR/whisper-server" --help >/dev/null 2>&1 || die "$BIN_DIR/whisper-server --help failed"
    install_venv
    link_command
    pull_models
    install_config
    install_units
    run_doctor
    log "Done"
}

main "$@"
