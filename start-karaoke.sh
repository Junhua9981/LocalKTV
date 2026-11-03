#!/usr/bin/env bash
# LocalKTV launcher (Linux): prepares the environment on first run, then starts
# the server (FastAPI + AI worker) and prints the join QR code.
# Mirrors start-karaoke.ps1 — every argument is forwarded to server/main.py.
#
#   ./start-karaoke.sh              # first run builds .venv + web UI
#   ./start-karaoke.sh --port 9000  # any main.py flag works
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

VENV_PY="$ROOT/.venv/bin/python"
REQUIREMENTS="$ROOT/server/requirements.txt"
DIST="$ROOT/web/dist/index.html"

log()  { printf '[LocalKTV] %s\n' "$*"; }
warn() { printf '[LocalKTV] warning: %s\n' "$*" >&2; }
die()  { printf '[LocalKTV] error: %s\n' "$*" >&2; exit 1; }

# --- Python 3.10+ ------------------------------------------------------------
py_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; }

find_python() {
    local candidate
    for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
        if command -v "$candidate" >/dev/null 2>&1 && py_ok "$candidate"; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

if [[ ! -x "$VENV_PY" ]]; then
    log "creating virtualenv..."
    base="$(find_python)" || die "Python 3.10+ is required. Install it first (e.g. apt install python3 python3-venv)."
    if ! "$base" -m venv "$ROOT/.venv"; then
        die "venv creation failed — Debian/Ubuntu need the python3-venv package"
    fi
    "$VENV_PY" -m pip install -q --upgrade pip
    "$VENV_PY" -m pip install -q -r "$REQUIREMENTS"
fi

# --- web UI (built only when dist is missing) --------------------------------
if [[ ! -f "$DIST" ]]; then
    if command -v npm >/dev/null 2>&1; then
        log "building web UI (first run)..."
        (cd "$ROOT/web" && npm install --no-audit --no-fund && npm run build)
    else
        warn "npm not found — /display UI unavailable (install Node.js, then re-run)"
    fi
fi

# --- ffmpeg (transcode + duration probe + MV) --------------------------------
command -v "${KTV_FFMPEG:-ffmpeg}" >/dev/null 2>&1 ||
    warn "ffmpeg not found — downloads will fail to transcode (apt install ffmpeg or set KTV_FFMPEG)"
command -v "${KTV_FFPROBE:-ffprobe}" >/dev/null 2>&1 ||
    warn "ffprobe not found — duration checks will be skipped (apt install ffmpeg)"

# --- headless host: never try to open a browser ------------------------------
args=("$@")
if [[ -z "${DISPLAY:-}" && -z "${WAYLAND_DISPLAY:-}" ]]; then
    has_no_browser=0
    for arg in ${args[@]+"${args[@]}"}; do
        if [[ "$arg" == "--no-browser" ]]; then
            has_no_browser=1
        fi
    done
    if [[ "$has_no_browser" -eq 0 ]]; then
        args+=(--no-browser)
    fi
fi

exec "$VENV_PY" "$ROOT/server/main.py" ${args[@]+"${args[@]}"}
