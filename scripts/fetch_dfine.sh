#!/usr/bin/env bash
# Verify the pinned D-FINE-N ONNX model committed at
# backend/classification/models/dfine_n.onnx.
#
# Usage:
#   scripts/fetch_dfine.sh
#
# What it does:
#   - The .onnx (15.3 MB) is committed in-tree — same strategy as YAMNet
#     and (historically) YOLOX — so build/CI pipelines have zero network
#     dependency. This script's job is to verify the committed file has
#     the SHA256 we expect, and to write the NOTICE alongside it.
#   - If the file is missing (or its hash changes), the script fails loud
#     so model drift cannot enter the bundle silently.
#
# Re-exporting the model (rare; only when rev'ing D-FINE):
#   1. git clone https://github.com/Peterande/D-FINE (pinned below)
#   2. Download dfine_n_coco.pth (pinned SHA256 below)
#   3. From the clone, run:
#        python tools/deployment/export_onnx.py \
#          --config configs/dfine/dfine_hgnetv2_n_coco.yml \
#          --resume <path-to-pth> --check --simplify
#   4. Copy the resulting dfine_n_coco.onnx → backend/classification/models/dfine_n.onnx
#   5. Update DFINE_ONNX_SHA256 in this script + scripts/fetch_dfine.ps1.
#
# License:
#   D-FINE is Apache-2.0 (https://github.com/Peterande/D-FINE). This
#   script writes the required NOTICE.txt entry alongside the weights
#   so the PyInstaller bundle's classifier-model directory carries the
#   attribution required by §4(d) of the license.
#
# Cross-platform:
#   ONNX graph is platform-independent — same bytes work on
#   Windows/macOS/Linux, x64 and arm64.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODEL_DIR="${REPO_ROOT}/backend/classification/models"
ONNX_FILE="${MODEL_DIR}/dfine_n.onnx"

# --- Pinned upstream + checksums --------------------------------------------

# D-FINE release the .pth came from. Pinned so a future re-export is
# reproducible from a known checkpoint.
DFINE_RELEASE="dfinev1.0"
DFINE_PTH_URL="https://github.com/Peterande/storage/releases/download/${DFINE_RELEASE}/dfine_n_coco.pth"
DFINE_PTH_SHA256="41973938d2784d38a9836990d805b8392855ebf611aba55f0f7add90e110744c"

# SHA256 of the exported + onnxsim-simplified dfine_n.onnx that ships in-tree.
# Pin from the first verified export (2026-04-14 spike). If this ever drifts
# we want a loud mismatch, not a silent model swap.
DFINE_ONNX_SHA256="2bf2775b175b45ab582e4777f4fab75923a86cb5e2d3824f2735c2b25f1abc8d"

# --- Helpers ----------------------------------------------------------------

log() { printf '[fetch_dfine] %s\n' "$*"; }
die() { printf '[fetch_dfine] ERROR: %s\n' "$*" >&2; exit 1; }

sha256_of() {
    if command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
    else
        sha256sum "$1" | awk '{print $1}'
    fi
}

# --- Main -------------------------------------------------------------------

mkdir -p "${MODEL_DIR}"

if [ ! -f "${ONNX_FILE}" ]; then
    die "${ONNX_FILE} is missing. This model ships committed in-tree. If
        you hit this during a re-export, see the header of this script for
        the export procedure."
fi

actual="$(sha256_of "${ONNX_FILE}")"
if [ "${actual}" != "${DFINE_ONNX_SHA256}" ]; then
    die "dfine_n.onnx SHA256 mismatch: expected ${DFINE_ONNX_SHA256}, got ${actual}.
        Either the committed weights drifted or the pin is stale. Do NOT
        update the pin until you have re-run the eyeball test on real
        frames (see experiments/ in the private repo for the harness)."
fi
log "dfine_n.onnx SHA256 OK"

# Write/refresh the NOTICE file. YOLOX has been removed from this tree, so
# we own NOTICE.txt entirely now; yamnet.onnx gets its attribution in a
# separate NOTICE (written by scripts/fetch_yamnet.sh) — keeping the model
# dir's top-level NOTICE focused on the vision detector.
cat > "${MODEL_DIR}/NOTICE.txt" <<'EOF'
D-FINE
Copyright (c) 2024 Yansong Peng.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

Source: https://github.com/Peterande/D-FINE
Release: dfinev1.0
Checkpoint: dfine_n_coco.pth
Exported to ONNX with tools/deployment/export_onnx.py (--simplify).
EOF

log "done. Models in ${MODEL_DIR}:"
ls -la "${MODEL_DIR}"
