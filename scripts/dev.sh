#!/usr/bin/env bash
#
# Run SimpleNVR in development mode (macOS / Linux).
#
# Eliminates the stale-PyInstaller-bundle trap: `cargo tauri dev`
# doesn't run your `backend/*.py` files directly — it runs the
# PyInstaller onedir bundle at src-tauri/binaries/simplenvr-backend-dir/.
# Editing `backend/` without rebundling means your change is invisible
# until the next rebuild. This wrapper rebundles first, every time.
#
# Flow:
#   1. scripts/bundle_python.sh       (≈20-60s on a warm cache)
#   2. cargo tauri dev                (frontend hot-reloads; Rust/Python
#                                      require restarting this wrapper)
#
# Usage:
#   ./scripts/dev.sh
#
# Skip the rebundle (handy when you only touched Rust or frontend code):
#   ./scripts/dev.sh --skip-bundle
#
# Tips:
#   - Frontend changes (React / CSS / TS) hot-reload with no action.
#   - Rust changes to src-tauri/ trigger an automatic recompile in
#     place; you don't need to restart this wrapper.
#   - Python changes to backend/ require Ctrl-C then rerun
#     `./scripts/dev.sh` (which rebundles, then relaunches).
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

log() { printf '[dev] %s\n' "$*"; }
die() { printf '[dev] ERROR: %s\n' "$*" >&2; exit 1; }

SKIP_BUNDLE=0
for arg in "$@"; do
    case "$arg" in
        --skip-bundle) SKIP_BUNDLE=1 ;;
        -h|--help)
            sed -n '3,28p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) die "unknown argument: $arg" ;;
    esac
done

[ -d .venv ] || die ".venv not found. Run ./scripts/setup.sh first."

log "ensuring rtsp mosaic plugin checkout"
bash "$SCRIPT_DIR/ensure_rtsp_mosaic.sh"

log "checking libmpv prerequisites"
bash "$SCRIPT_DIR/ensure_libmpv.sh"

if [ "$SKIP_BUNDLE" = "0" ]; then
    log "rebundling Python backend"
    "$SCRIPT_DIR/bundle_python.sh"
else
    log "skipping rebundle (--skip-bundle)"
fi

log "starting cargo tauri dev"
exec cargo tauri dev
