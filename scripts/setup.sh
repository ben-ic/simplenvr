#!/usr/bin/env bash
#
# One-time dev machine setup for SimpleNVR on macOS / Linux.
#
# Windows has its own orchestrator pair (scripts/setup_windows.ps1 +
# scripts/build_windows.ps1). This is the macOS/Linux equivalent of
# setup_windows.ps1: it prepares a fresh clone so `./scripts/dev.sh`
# or `./scripts/build.sh` will work end-to-end.
#
# What it does (in order):
#   1. Checks required tools are on PATH (rust, node, python3.11+, cmake).
#      Fails with a single consolidated list if any are missing.
#   2. Creates .venv and installs backend Python dependencies.
#   3. Fetches pinned bundled binaries:
#        - FFmpeg + ffprobe (LGPL)
#        - go2rtc (MIT)
#        - libmpv (macOS only; LGPL self-contained dylib)
#        - D-FINE ONNX model (Apache-2.0, verify-only)
#        - YAMNet ONNX model (Apache-2.0, verify-only)
#   4. Builds the `tether` parent-death supervisor.
#   5. (--with-cv2 only) builds the LGPL-clean opencv-python-headless
#      wheel. Off by default — it's a 30-40 min one-time compile and
#      most dev iterations don't need a release-quality bundle. You'll
#      need it before the *first* `./scripts/build.sh`, and again
#      whenever scripts/build_cv2_wheel.sh's OPENCV_PYTHON_TAG changes.
#
# Usage:
#   ./scripts/setup.sh              # core setup, skip cv2 wheel
#   ./scripts/setup.sh --with-cv2   # include the 30-40 min cv2 wheel build
#   ./scripts/setup.sh --help
#
# Re-running is safe. Each step is idempotent:
#   - pip install is a no-op when deps are satisfied
#   - fetch_* scripts skip downloads when artifacts exist with matching SHA
#   - build_tether.sh rebuilds from cargo cache (fast)
#   - build_cv2_wheel.sh reuses the cached opencv-python checkout
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# ── Arg parsing ──────────────────────────────────────────────────────
WITH_CV2=0
for arg in "$@"; do
    case "$arg" in
        --with-cv2) WITH_CV2=1 ;;
        -h|--help)
            sed -n '3,40p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) echo "[setup] unknown argument: $arg" >&2; exit 1 ;;
    esac
done

log()  { printf '[setup] %s\n' "$*"; }
step() { printf '\n[setup] === %s ===\n' "$*"; }
die()  { printf '[setup] ERROR: %s\n' "$*" >&2; exit 1; }

# ── Platform detection ───────────────────────────────────────────────
case "$(uname)" in
    Darwin) PLATFORM="macos" ;;
    Linux)  PLATFORM="linux" ;;
    *) die "unsupported platform $(uname). Use scripts/setup_windows.ps1 on Windows." ;;
esac
log "platform: $PLATFORM"

# ── Prereq check ─────────────────────────────────────────────────────
# One consolidated list so you see everything that's missing on the
# first run, not one-at-a-time across five retries.
step "checking prerequisites"

missing=()

need() {
    local cmd="$1" hint="$2"
    if ! command -v "$cmd" >/dev/null 2>&1; then
        missing+=("  - $cmd  ($hint)")
    fi
}

need rustc   "install via https://rustup.rs"
need cargo   "install via https://rustup.rs"
need node    "install Node.js 20 LTS"
need npm     "comes with Node.js"
need python3 "install Python 3.11+"
need cmake   "macOS: brew install cmake  |  Linux: apt-get install cmake"
need git     "you shouldn't be reading this without it"
need curl    "macOS: preinstalled  |  Linux: apt-get install curl"
need unzip   "macOS: preinstalled  |  Linux: apt-get install unzip"

# cargo-tauri is installed via `cargo install`, not a distro package.
if ! cargo tauri --version >/dev/null 2>&1; then
    missing+=("  - cargo-tauri  (cargo install tauri-cli --version '^2.0')")
fi

# macOS-only: fetch_mpv.sh uses `gh release download` to pull the
# libmpv dylib from ben-ic/libmpv-macos.
if [ "$PLATFORM" = "macos" ]; then
    need gh "brew install gh  (then 'gh auth login' once)"
fi

# Linux-only: Tauri needs WebKitGTK + friends. We can't install them
# from here (needs sudo; distro-specific), but we can check.
if [ "$PLATFORM" = "linux" ]; then
    if ! pkg-config --exists webkit2gtk-4.1 2>/dev/null; then
        missing+=("  - libwebkit2gtk-4.1-dev  (apt-get install libwebkit2gtk-4.1-dev libayatana-appindicator3-dev librsvg2-dev libssl-dev build-essential)")
    fi
fi

# Python 3.11+ check. Newer Ubuntu ships python3=3.12, but Debian
# stable still serves 3.11 — both fine. We fail the check below 3.11
# because backend/requirements.txt pins onnxruntime/opencv versions
# whose wheels start at cp311.
if command -v python3 >/dev/null 2>&1; then
    if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
        pyver="$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')"
        missing+=("  - python3 >= 3.11  (found $pyver; install a newer Python)")
    fi
fi

if [ ${#missing[@]} -gt 0 ]; then
    echo ""
    echo "[setup] missing prerequisites:" >&2
    printf '%s\n' "${missing[@]}" >&2
    echo "" >&2
    echo "Install the tools above, reopen your shell so PATH updates," >&2
    echo "and rerun ./scripts/setup.sh." >&2
    exit 1
fi

log "all prerequisites present"

# ── Python venv ──────────────────────────────────────────────────────
step "python virtual environment"

if [ ! -d .venv ]; then
    log "creating .venv"
    python3 -m venv .venv
else
    log ".venv already exists"
fi

# shellcheck disable=SC1091
source .venv/bin/activate

log "installing backend/requirements.txt into .venv"
pip install --quiet --upgrade pip
pip install --quiet -r backend/requirements.txt

# ── Bundled binaries ─────────────────────────────────────────────────
step "fetching bundled binaries"

"$SCRIPT_DIR/fetch_ffmpeg.sh"
"$SCRIPT_DIR/fetch_go2rtc.sh"
"$SCRIPT_DIR/fetch_dfine.sh"
"$SCRIPT_DIR/fetch_yamnet.sh"

if [ "$PLATFORM" = "macos" ]; then
    "$SCRIPT_DIR/fetch_mpv.sh"
fi

# ── Tether supervisor ────────────────────────────────────────────────
step "building tether supervisor"
"$SCRIPT_DIR/build_tether.sh"

# ── Frontend deps ────────────────────────────────────────────────────
# Not strictly required for `cargo tauri dev` (Tauri's beforeDevCommand
# invokes `vite` which pulls node_modules on demand), but running it
# here surfaces any npm install failures before the first dev run.
step "ensuring rtsp mosaic plugin checkout"
bash "$SCRIPT_DIR/ensure_rtsp_mosaic.sh" --build-guest-js

step "installing frontend dependencies"
(cd frontend && npm install --silent)

# ── cv2 wheel (optional, slow) ───────────────────────────────────────
# Platform-specific wheel glob, matching bundle_python.sh's detection.
case "$(uname)-$(uname -m)" in
    Darwin-arm64)  CV2_WHEEL_GLOB="opencv_python_headless-*-macosx_*_arm64.whl" ;;
    Darwin-x86_64) CV2_WHEEL_GLOB="opencv_python_headless-*-macosx_*_x86_64.whl" ;;
    Linux-x86_64)  CV2_WHEEL_GLOB="opencv_python_headless-*linux*_x86_64.whl" ;;
    Linux-aarch64) CV2_WHEEL_GLOB="opencv_python_headless-*linux*_aarch64.whl" ;;
    *)             CV2_WHEEL_GLOB="" ;;
esac

cv2_wheel_present() {
    [ -n "$CV2_WHEEL_GLOB" ] || return 1
    # shellcheck disable=SC2086
    ls vendor/cv2-wheels/$CV2_WHEEL_GLOB >/dev/null 2>&1
}

if [ "$WITH_CV2" = "1" ]; then
    if cv2_wheel_present; then
        step "cv2 wheel: already present, skipping rebuild"
        # shellcheck disable=SC2086
        log "  $(ls -1 vendor/cv2-wheels/$CV2_WHEEL_GLOB | head -1)"
        log "  (bump OPENCV_PYTHON_TAG in build_cv2_wheel.sh to force a rebuild)"
    else
        step "building LGPL-clean cv2 wheel (30-40 min)"
        "$SCRIPT_DIR/build_cv2_wheel.sh"
    fi
elif cv2_wheel_present; then
    step "cv2 wheel: present in vendor/cv2-wheels/"
    log "you're ready for './scripts/build.sh'."
else
    step "cv2 wheel: not present (skipped, pass --with-cv2 to build)"
    log "'./scripts/dev.sh' will still work for iteration; './scripts/build.sh'"
    log "will fail loud until you run './scripts/setup.sh --with-cv2' once."
fi

# ── Done ─────────────────────────────────────────────────────────────
echo ""
echo "============================================================"
echo "[setup] Done."
echo ""
echo "  Next steps:"
echo "    ./scripts/dev.sh     — run SimpleNVR in dev mode (hot-reload)"
echo "    ./scripts/build.sh   — build the shippable installer"
if ! cv2_wheel_present; then
    echo ""
    echo "  Before the first './scripts/build.sh', run:"
    echo "    ./scripts/setup.sh --with-cv2"
    echo "  to produce the LGPL-clean opencv wheel. One-time, ~30-40 min."
fi
echo "============================================================"
