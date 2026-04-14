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

echo "Bundle ready at: ${OUT_ROOT}/${OUT_NAME}/"
