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

pip install --quiet 'pyinstaller>=6.0'

TARGET_TRIPLE=$(rustc -vV | grep '^host:' | awk '{print $2}')
OUT_ROOT="src-tauri/binaries"
OUT_NAME="simplenvr-backend-${TARGET_TRIPLE}"

mkdir -p "${OUT_ROOT}"
rm -rf build/pyinstaller \
       "${OUT_ROOT}/simplenvr-backend" \
       "${OUT_ROOT}/${OUT_NAME}"

pyinstaller backend/main.spec \
    --distpath "${OUT_ROOT}" \
    --workpath build/pyinstaller \
    --noconfirm

# Onefile output is a single file; rename to triple-suffixed name.
mv "${OUT_ROOT}/simplenvr-backend" "${OUT_ROOT}/${OUT_NAME}"

echo "Bundle ready at: ${OUT_ROOT}/${OUT_NAME}"
