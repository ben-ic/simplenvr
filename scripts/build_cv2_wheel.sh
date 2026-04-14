#!/usr/bin/env bash
#
# Build an LGPL-clean opencv-python-headless wheel for the current host
# platform (macOS or Linux) and architecture. Windows is covered by
# scripts/build_cv2_wheel.ps1.
#
# Why this exists:
#   The stock opencv-python-headless wheel on PyPI bundles an FFmpeg
#   build that hard-links libx264 and libx265 (GPL-2.0+). cv2.abi3.so
#   pulls libavcodec in as a LC_LOAD_DYLIB, which the OS linker
#   resolves eagerly at `import cv2`, dragging the GPL deps into our
#   closed-source bundle. Building with `-DWITH_FFMPEG=OFF` removes
#   libavcodec from the wheel entirely; cv2's image algorithms
#   (MOG2, morphology, resize, matchTemplate, imencode) are
#   unaffected, because they're implemented in OpenCV proper and do
#   not touch FFmpeg. SimpleNVR never calls VideoCapture/VideoWriter,
#   so losing FFmpeg-backed video I/O costs nothing.
#
# Output:
#   vendor/cv2-wheels/opencv_python_headless-<version>-<tag>.whl
#   (one platform-specific wheel per run; dist/ is gitignored)
#
# Runtime: ~30-40 min on M-series mac, ~20-30 min on an 8-core Linux host.
# First run compiles OpenCV from source; subsequent runs skip clone if
# /tmp/opencv-selfbuild/opencv-python already exists at the pinned tag.
#
# Usage:
#   scripts/build_cv2_wheel.sh
#
# Verify the output:
#   pip install vendor/cv2-wheels/opencv_python_headless-*.whl --force-reinstall
#   python -c "import cv2; print(cv2.__file__)"
#   otool -L <that path>/cv2/.dylibs/*.dylib | grep -iE 'x264|x265|avcodec'
#     → should print nothing. Any hit = the build kept FFmpeg.
#
set -euo pipefail

# Pin: opencv-python uses unusual tag naming — the tag is the patch
# number alone, not the full version string. Tag "92" == release
# 4.13.0.92. Bump this intentionally; regenerate all platform wheels
# from the same tag so runtime behavior matches across OSes.
OPENCV_PYTHON_TAG="92"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
WORK_DIR="${TMPDIR:-/tmp}/opencv-selfbuild"
OUTPUT_DIR="${REPO_ROOT}/vendor/cv2-wheels"

# ── Prerequisites ───────────────────────────────────────────────────
for cmd in cmake git; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "ERROR: $cmd not found on PATH." >&2
        case "$(uname)" in
            Darwin) echo "  macOS: brew install cmake   (and xcode-select --install if missing)" >&2 ;;
            Linux)  echo "  Linux: apt-get install cmake git build-essential python3-dev" >&2 ;;
        esac
        exit 1
    fi
done

# Activate the repo venv so `pip wheel` uses the same Python the sidecar
# runs against — avoids a cross-version wheel that won't install back.
if [ -f "${REPO_ROOT}/.venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "${REPO_ROOT}/.venv/bin/activate"
else
    echo "ERROR: .venv not found at ${REPO_ROOT}/.venv — create it first." >&2
    exit 1
fi

# ── Fetch opencv-python source (cached by tag) ──────────────────────
mkdir -p "$WORK_DIR" "$OUTPUT_DIR"
cd "$WORK_DIR"

if [ ! -d opencv-python ] || [ "$(cd opencv-python && git describe --tags --exact-match 2>/dev/null || echo unknown)" != "$OPENCV_PYTHON_TAG" ]; then
    rm -rf opencv-python
    echo "Cloning opencv-python @ tag ${OPENCV_PYTHON_TAG}..."
    git clone --recursive --depth 1 --branch "$OPENCV_PYTHON_TAG" \
        https://github.com/opencv/opencv-python.git
else
    echo "Reusing existing checkout at ${WORK_DIR}/opencv-python (tag ${OPENCV_PYTHON_TAG})."
fi

cd opencv-python

# ── Install Python build deps into the repo venv ───────────────────
pip install --quiet --upgrade pip setuptools wheel scikit-build

# ── Build ──────────────────────────────────────────────────────────
case "$(uname)" in
    Darwin) NPROC=$(sysctl -n hw.ncpu) ;;
    Linux)  NPROC=$(nproc) ;;
    *)      NPROC=4 ;;
esac

echo
echo "Compiling with ${NPROC} parallel jobs..."
echo "  CMAKE_ARGS: -DWITH_FFMPEG=OFF -DWITH_GSTREAMER=OFF -DWITH_1394=OFF"
echo "  Output:     ${OUTPUT_DIR}"
echo

# ENABLE_HEADLESS=1 selects opencv-python-headless (no Qt/GTK deps);
# MAKEFLAGS propagates to the inner OpenCV make invocation.
ENABLE_HEADLESS=1 \
CMAKE_ARGS="-DWITH_FFMPEG=OFF -DWITH_GSTREAMER=OFF -DWITH_1394=OFF" \
MAKEFLAGS="-j${NPROC}" \
    pip wheel . -w "$OUTPUT_DIR" --verbose

echo
echo "Wheel built:"
ls -la "$OUTPUT_DIR"/opencv_python_headless-*.whl
