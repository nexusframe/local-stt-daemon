#!/usr/bin/env bash
# local-stt uninstaller (docs/11-daemon-systemd-installation.md §11.3).
# Without --purge, models, the whisper.cpp build and ~/.config/local-stt are kept:
# downloading or recreating them is costly. apt packages are never removed.
set -euo pipefail

readonly DATA_DIR="$HOME/.local/share/local-stt"
readonly CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/local-stt"
readonly VENV_DIR="$DATA_DIR/venv"
readonly BIN_LINK="$HOME/.local/bin/local-stt"
readonly UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
readonly UNITS=(local-stt.service local-stt-whisper.service)

PURGE=0

log() { printf '\033[1m==>\033[0m %s\n' "$*"; }

usage() {
    cat <<EOF
Usage: $(basename "$0") [--purge]

  --purge  also remove $DATA_DIR (models, whisper.cpp) and $CONFIG_DIR
EOF
}

while (($#)); do
    case "$1" in
        --purge)   PURGE=1 ;;
        -h|--help) usage; exit 0 ;;
        *)         usage >&2; exit 2 ;;
    esac
    shift
done

log "Stopping and disabling ${UNITS[*]}"
systemctl --user disable --now "${UNITS[@]}" 2>/dev/null || true
for unit in "${UNITS[@]}"; do
    rm -f "$UNIT_DIR/$unit"
done
systemctl --user daemon-reload
systemctl --user reset-failed "${UNITS[@]}" 2>/dev/null || true

# only our own symlink, never a file the user put there
if [[ -L "$BIN_LINK" && "$(readlink "$BIN_LINK")" == "$VENV_DIR/bin/local-stt" ]]; then
    log "Removing $BIN_LINK"
    rm -f "$BIN_LINK"
fi

log "Removing $VENV_DIR"
rm -rf "$VENV_DIR"
rm -rf "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/local-stt"

if ((PURGE)); then
    log "Removing $DATA_DIR and $CONFIG_DIR"
    rm -rf "$DATA_DIR" "$CONFIG_DIR"
else
    log "Kept $DATA_DIR (models, whisper.cpp) and $CONFIG_DIR; use --purge to remove them"
fi
log "Done"
