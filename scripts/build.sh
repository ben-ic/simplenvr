#!/usr/bin/env bash
#
# Build the SimpleNVR installer end-to-end on macOS or Linux.
#
# Windows equivalent: scripts/build_windows.ps1.
#
# What it does:
#   1. Sanity-check that ./scripts/setup.sh has been run (venv + bundled
#      binaries present).
#   2. Sanity-check that an LGPL-clean cv2 wheel exists in
#      vendor/cv2-wheels/. If missing, fail loud with a clear pointer
#      to `./scripts/setup.sh --with-cv2` — better than a 4-minute
#      PyInstaller run that dies at the GPL scanner.
#   3. Rebundle the Python backend via PyInstaller.
#   4. Run `cargo tauri build` with the right bundle flag for the host
#      platform (--bundles dmg on macOS, --bundles deb on Linux).
#   5. Print the output path so you can grab the installer.
#
# Usage:
#   ./scripts/build.sh                 # host platform, default bundle
#   ./scripts/build.sh --bundles dmg   # override bundle format
#   ./scripts/build.sh --help
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

log()  { printf '[build] %s\n' "$*"; }
step() { printf '\n[build] === %s ===\n' "$*"; }
die()  { printf '[build] ERROR: %s\n' "$*" >&2; exit 1; }

# ── Arg parsing ──────────────────────────────────────────────────────
BUNDLES=""
for (( i=1; i<=$#; i++ )); do
    case "${!i}" in
        --bundles)
            next=$((i+1))
            BUNDLES="${!next:-}"
            [ -n "$BUNDLES" ] || die "--bundles requires a value (dmg|deb|app|...)"
            ;;
        -h|--help)
            sed -n '3,25p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
    esac
done

# ── Platform defaults ────────────────────────────────────────────────
case "$(uname)" in
    Darwin) PLATFORM="macos"; DEFAULT_BUNDLE="dmg" ;;
    Linux)  PLATFORM="linux"; DEFAULT_BUNDLE="deb" ;;
    *) die "unsupported platform $(uname). Use scripts/build_windows.ps1 on Windows." ;;
esac
BUNDLES="${BUNDLES:-$DEFAULT_BUNDLE}"
log "platform: $PLATFORM, bundle format: $BUNDLES"

# ── Prereq: setup.sh has been run ────────────────────────────────────
step "sanity checks"

[ -d .venv ] || die ".venv not found. Run ./scripts/setup.sh first."

BIN_DIR="src-tauri/binaries"
[ -d "$BIN_DIR" ]  || die "$BIN_DIR is missing. Run ./scripts/setup.sh first."

# Spot-check that the fetches ran — one artifact from each script.
case "$PLATFORM" in
    macos)
        triple="$(uname -m | sed 's/arm64/aarch64/; s/x86_64/x86_64/')-apple-darwin"
        ;;
    linux)
        case "$(uname -m)" in
            x86_64)  triple="x86_64-unknown-linux-gnu" ;;
            aarch64) triple="aarch64-unknown-linux-gnu" ;;
            *) die "unsupported Linux arch $(uname -m)." ;;
        esac
        ;;
esac

[ -f "$BIN_DIR/ffmpeg-$triple"  ] || die "ffmpeg-$triple not found. Run ./scripts/setup.sh."
[ -f "$BIN_DIR/go2rtc-$triple"  ] || die "go2rtc-$triple not found. Run ./scripts/setup.sh."
[ -f "$BIN_DIR/tether-$triple"  ] || die "tether-$triple not found. Run ./scripts/setup.sh."

# ── Prereq: cv2 wheel present ────────────────────────────────────────
# bundle_python.sh warns-and-continues when the wheel is missing, then
# explodes at the GPL scanner 4 minutes later. Fail faster and louder
# here so the first-time contributor sees the actionable message up
# front, not after a wasted compile.
case "$(uname)-$(uname -m)" in
    Darwin-arm64)  WHEEL_GLOB="opencv_python_headless-*-macosx_*_arm64.whl" ;;
    Darwin-x86_64) WHEEL_GLOB="opencv_python_headless-*-macosx_*_x86_64.whl" ;;
    Linux-x86_64)  WHEEL_GLOB="opencv_python_headless-*linux*_x86_64.whl" ;;
    Linux-aarch64) WHEEL_GLOB="opencv_python_headless-*linux*_aarch64.whl" ;;
    *)             WHEEL_GLOB="" ;;
esac

if [ -n "$WHEEL_GLOB" ]; then
    # shellcheck disable=SC2086
    if ! ls vendor/cv2-wheels/$WHEEL_GLOB >/dev/null 2>&1; then
        die "no LGPL-clean cv2 wheel found at vendor/cv2-wheels/$WHEEL_GLOB.

  Run:
      ./scripts/setup.sh --with-cv2

  This is a one-time ~30-40 min compile. Subsequent builds reuse
  the wheel until scripts/build_cv2_wheel.sh's OPENCV_PYTHON_TAG
  bumps. See docs/cv2-selfbuild.md for the full rationale."
    fi
    log "cv2 wheel found"
fi

log "all checks passed"

step "ensuring rtsp mosaic plugin checkout"
bash "$SCRIPT_DIR/ensure_rtsp_mosaic.sh"

# ── Bundle the Python backend ────────────────────────────────────────
step "bundling Python backend (PyInstaller)"
"$SCRIPT_DIR/bundle_python.sh"

# ── Tauri build ──────────────────────────────────────────────────────
step "cargo tauri build --bundles $BUNDLES"
cargo tauri build --bundles "$BUNDLES"

# ── Report output paths ──────────────────────────────────────────────
OUT_BASE="src-tauri/target/release/bundle"
echo ""
echo "============================================================"
echo "[build] Done."
echo ""
echo "  Installers land in $OUT_BASE/"
if [ -d "$OUT_BASE" ]; then
    # Don't predict the filename; just list what's there so a
    # version-number bump doesn't silently break this message.
    find "$OUT_BASE" -maxdepth 2 -type f \
        \( -name '*.dmg' -o -name '*.app.tar.gz' -o -name '*.deb' \
           -o -name '*.AppImage' -o -name '*.rpm' \) \
        -exec printf '  → %s\n' {} \; 2>/dev/null || true
fi
echo "============================================================"
