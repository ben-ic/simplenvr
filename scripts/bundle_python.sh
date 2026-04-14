#!/usr/bin/env bash
#
# Bundle the SimpleNVR Python backend into a single-file executable
# via PyInstaller, named with the rustc target triple to match Tauri's
# externalBin convention.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# Always use the project venv — never install PyInstaller globally.
# shellcheck disable=SC1091
source .venv/bin/activate

# Ensure backend runtime deps (onnxruntime, opencv, scipy, psutil, ...)
# are present in the venv before PyInstaller analyzes imports — a
# missing dep would be silently omitted from the bundle and bite at
# sidecar startup. Idempotent; quiet when already satisfied.
pip install --quiet -r backend/requirements.txt
pip install --quiet 'pyinstaller>=6.0'

# Replace stock opencv-python-headless with our self-built LGPL-clean wheel
# if one is present at vendor/cv2-wheels/. The stock PyPI wheel bundles
# libavcodec linked against libx264/libx265 (GPL-2.0+); the self-built
# wheel is compiled with -DWITH_FFMPEG=OFF so those deps don't exist.
# See docs/cv2-selfbuild.md and scripts/build_cv2_wheel.sh.
CV2_WHEEL_DIR="${REPO_ROOT:-$(pwd)}/vendor/cv2-wheels"
case "$(uname)-$(uname -m)" in
    Darwin-arm64)  WHEEL_GLOB="opencv_python_headless-*-macosx_*_arm64.whl" ;;
    Darwin-x86_64) WHEEL_GLOB="opencv_python_headless-*-macosx_*_x86_64.whl" ;;
    Linux-x86_64)  WHEEL_GLOB="opencv_python_headless-*linux*_x86_64.whl" ;;
    Linux-aarch64) WHEEL_GLOB="opencv_python_headless-*linux*_aarch64.whl" ;;
    *)             WHEEL_GLOB="" ;;
esac
if [ -n "$WHEEL_GLOB" ] && [ -d "$CV2_WHEEL_DIR" ]; then
    # shellcheck disable=SC2086
    LOCAL_WHEEL=$(ls -1t ${CV2_WHEEL_DIR}/${WHEEL_GLOB} 2>/dev/null | head -1 || true)
    if [ -n "$LOCAL_WHEEL" ]; then
        echo "Using self-built cv2 wheel: $(basename "$LOCAL_WHEEL")"
        pip install --quiet --force-reinstall --no-deps "$LOCAL_WHEEL"
    else
        echo "WARNING: no self-built cv2 wheel found at $CV2_WHEEL_DIR matching $WHEEL_GLOB"
        echo "         Bundle will likely contain GPL FFmpeg deps. Run"
        echo "         scripts/build_cv2_wheel.sh first for a license-clean build."
    fi
fi

# Verify the in-tree D-FINE weights before building so we fail before
# PyInstaller's analysis step rather than in the middle of it.
scripts/fetch_dfine.sh

OUT_ROOT="src-tauri/binaries"
# Fixed name (no triple suffix) — Tauri resources don't use the
# externalBin triple-suffix convention.
OUT_NAME="simplenvr-backend-dir"

mkdir -p "${OUT_ROOT}"
rm -rf build/pyinstaller \
       "${OUT_ROOT}/simplenvr-backend" \
       "${OUT_ROOT}/${OUT_NAME}"

pyinstaller backend/main.spec \
    --distpath "${OUT_ROOT}" \
    --workpath build/pyinstaller \
    --noconfirm

# Onedir output is a directory; rename to the fixed resource name.
mv "${OUT_ROOT}/simplenvr-backend" "${OUT_ROOT}/${OUT_NAME}"

# Post-bundle GPL scan — fails loud if any FFmpeg GPL-able dep
# (libavcodec/libavformat/libavutil/libswscale/libpostproc, libx264,
# libx265) slipped into the bundle. Catches: someone re-running
# `pip install --upgrade` between bundle builds, a new dep that pulls
# FFmpeg transitively, or a future pip resolver change. File-name scan
# (not dep analysis) because it's trivially cross-platform and covers
# the only realistic failure mode (the stock opencv-python-headless
# wheel shipping these as standalone dylibs/so/dlls).
echo "Scanning bundle for GPL FFmpeg deps..."
HITS=$(find "${OUT_ROOT}/${OUT_NAME}" -type f \
    \( -iname 'libx264*' -o -iname 'libx265*' \
    -o -iname 'libavcodec*' -o -iname 'libavformat*' \
    -o -iname 'libavutil*' -o -iname 'libswscale*' -o -iname 'libpostproc*' \
    -o -iname 'libavdevice*' -o -iname 'libavfilter*' \) 2>/dev/null || true)
if [ -n "$HITS" ]; then
    echo "ERROR: GPL FFmpeg deps found in the bundle:" >&2
    echo "$HITS" | sed 's|^|  |' >&2
    echo "" >&2
    echo "Stock opencv-python-headless was installed instead of the self-built" >&2
    echo "LGPL-clean wheel. Fix: run scripts/build_cv2_wheel.sh, then re-run" >&2
    echo "this script. See docs/cv2-selfbuild.md for details." >&2
    exit 1
fi
echo "  → no GPL FFmpeg deps found"

echo "Bundle ready at: ${OUT_ROOT}/${OUT_NAME}/"
