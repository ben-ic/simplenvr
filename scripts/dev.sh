#!/usr/bin/env bash
#
# Run SimpleNVR in development mode (macOS / Linux).
#
# Eliminates the stale-PyInstaller-bundle trap: `cargo tauri dev`
# doesn't run your `backend/*.py` files directly — it runs the
# PyInstaller onedir bundle at src-tauri/binaries/simplenvr-backend-dir/.
# Editing `backend/` without rebundling means your change is invisible
# until the next rebuild. This wrapper computes a fingerprint of backend
# inputs and only rebundles when those inputs change.
#
# Flow:
#   1. scripts/bundle_python.sh       (≈20-60s on a warm cache, when needed)
#   2. cargo tauri dev                (frontend hot-reloads; Rust/Python
#                                      require restarting this wrapper)
#
# Usage:
#   ./scripts/dev.sh
#
# Skip the rebundle check entirely (handy when you only touched Rust/frontend):
#   ./scripts/dev.sh --skip-bundle
#
# Force a rebundle even when no backend input changed:
#   ./scripts/dev.sh --force-bundle
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

compute_backend_fingerprint() {
    python3 - <<'PY'
import hashlib
import pathlib
import platform

root = pathlib.Path.cwd()
paths = []

for p in sorted((root / "backend").rglob("*")):
    if p.is_file() and "__pycache__" not in p.parts:
        paths.append(p)

paths.append(root / "backend" / "requirements.txt")
paths.append(root / "backend" / "main.spec")
paths.append(root / "scripts" / "bundle_python.sh")

sysname = platform.system()
arch = platform.machine().lower()
wheel_glob = None
if sysname == "Darwin" and arch == "arm64":
    wheel_glob = "opencv_python_headless-*-macosx_*_arm64.whl"
elif sysname == "Darwin" and arch == "x86_64":
    wheel_glob = "opencv_python_headless-*-macosx_*_x86_64.whl"
elif sysname == "Linux" and arch == "x86_64":
    wheel_glob = "opencv_python_headless-*linux*_x86_64.whl"
elif sysname == "Linux" and arch == "aarch64":
    wheel_glob = "opencv_python_headless-*linux*_aarch64.whl"

wheel_name = ""
if wheel_glob:
    wheel_dir = root / "vendor" / "cv2-wheels"
    wheels = sorted(wheel_dir.glob(wheel_glob), key=lambda p: p.stat().st_mtime, reverse=True)
    if wheels:
        wheel = wheels[0]
        st = wheel.stat()
        wheel_name = f"{wheel.name}:{st.st_size}:{int(st.st_mtime)}"

h = hashlib.sha256()
for p in paths:
    if not p.exists() or not p.is_file():
        continue
    rel = p.relative_to(root).as_posix().encode("utf-8")
    h.update(rel)
    with p.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)

h.update(f"wheel={wheel_name}".encode("utf-8"))
print(h.hexdigest())
PY
}

SKIP_BUNDLE=0
FORCE_BUNDLE=0
for arg in "$@"; do
    case "$arg" in
        --skip-bundle) SKIP_BUNDLE=1 ;;
        --force-bundle) FORCE_BUNDLE=1 ;;
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
    STAMP_PATH="src-tauri/binaries/.backend_bundle_fingerprint"
    OUT_DIR="src-tauri/binaries/simplenvr-backend-dir"
    CUR_FP="$(compute_backend_fingerprint)"
    PREV_FP=""
    if [ -f "$STAMP_PATH" ]; then
        PREV_FP="$(cat "$STAMP_PATH")"
    fi

    if [ "$FORCE_BUNDLE" = "1" ]; then
        log "force rebundle requested (--force-bundle)"
        "$SCRIPT_DIR/bundle_python.sh"
        printf '%s\n' "$CUR_FP" > "$STAMP_PATH"
    elif [ ! -d "$OUT_DIR" ]; then
        log "backend bundle missing; bundling Python backend"
        "$SCRIPT_DIR/bundle_python.sh"
        printf '%s\n' "$CUR_FP" > "$STAMP_PATH"
    elif [ "$CUR_FP" != "$PREV_FP" ]; then
        log "backend inputs changed; rebundling Python backend"
        "$SCRIPT_DIR/bundle_python.sh"
        printf '%s\n' "$CUR_FP" > "$STAMP_PATH"
    else
        log "backend unchanged; skipping rebundle"
    fi
else
    log "skipping rebundle (--skip-bundle)"
fi

log "starting cargo tauri dev"
exec cargo tauri dev
