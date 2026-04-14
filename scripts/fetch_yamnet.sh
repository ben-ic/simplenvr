#!/usr/bin/env bash
# Verify the pinned YAMNet ONNX model + AudioSet class map committed at
#   backend/classification/models/yamnet.onnx
#   backend/classification/models/yamnet_classes.txt
#
# Usage:
#   scripts/fetch_yamnet.sh
#
# What it does:
#   - Both files are committed in-tree — same strategy as D-FINE — so
#     build/CI pipelines have zero network dependency. This script's job
#     is to verify the committed files have the SHA256 we expect.
#   - If a file is missing (or its hash changes), the script fails loud
#     so model drift cannot enter the bundle silently.
#   - The model directory's top-level NOTICE.txt is written by
#     scripts/fetch_dfine.sh (which owns the heredoc covering D-FINE,
#     YAMNet, and the AudioSet class map). Keeping the NOTICE in one
#     place avoids races where one fetch script clobbers the other's
#     attribution block.
#
# Re-exporting the model (rare; only when rev'ing YAMNet):
#   1. pip install tf2onnx tensorflow tensorflow-hub
#   2. python -c "
#        import tensorflow as tf, tensorflow_hub as hub, tf2onnx
#        model = hub.load('https://tfhub.dev/google/yamnet/1')
#        tf2onnx.convert.from_function(
#            model.signatures['serving_default'],
#            input_signature=[tf.TensorSpec([15360], tf.float32, name='waveform')],
#            output_path='yamnet.onnx',
#        )"
#   3. Copy the resulting yamnet.onnx → backend/classification/models/yamnet.onnx
#   4. Re-fetch the AudioSet class map:
#        curl -fsSL https://raw.githubusercontent.com/tensorflow/models/master/research/audioset/yamnet/yamnet_class_map.csv \
#          | tail -n +2 | cut -d',' -f3 | sed 's/^"//; s/"$//' \
#          > backend/classification/models/yamnet_classes.txt
#   5. Update YAMNET_ONNX_SHA256 and YAMNET_CLASSES_SHA256 in this script
#      and scripts/fetch_yamnet.ps1.
#
# License:
#   YAMNet is Apache-2.0 (Google TensorFlow Model Garden,
#   https://github.com/tensorflow/models/tree/master/research/audioset/yamnet).
#   The AudioSet class map is CC-BY-4.0 (Google,
#   https://research.google.com/audioset/). The required NOTICE entries
#   live in backend/classification/models/NOTICE.txt (written by
#   scripts/fetch_dfine.sh).
#
# Cross-platform:
#   ONNX graph + plain-text class list are platform-independent — same
#   bytes work on Windows/macOS/Linux, x64 and arm64.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODEL_DIR="${REPO_ROOT}/backend/classification/models"
ONNX_FILE="${MODEL_DIR}/yamnet.onnx"
CLASSES_FILE="${MODEL_DIR}/yamnet_classes.txt"

# --- Pinned checksums -------------------------------------------------------

# SHA256 of the yamnet.onnx that ships in-tree. Pin from the first verified
# export (2026-04-14). If this ever drifts we want a loud mismatch, not a
# silent model swap.
YAMNET_ONNX_SHA256="22fca37f60dea142179487814c9faa9c2e535497f87a46f6fc0f6df1c20beef8"

# SHA256 of the committed AudioSet class map (521 display names, one per line,
# extracted from yamnet_class_map.csv column 3).
YAMNET_CLASSES_SHA256="a6984f0f8bba8c6f2bd24ee96aa9e488256bf36253c26b7f047c29c62728b026"

# --- Helpers ----------------------------------------------------------------

log() { printf '[fetch_yamnet] %s\n' "$*"; }
die() { printf '[fetch_yamnet] ERROR: %s\n' "$*" >&2; exit 1; }

sha256_of() {
    if command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
    else
        sha256sum "$1" | awk '{print $1}'
    fi
}

verify() {
    local path="$1" expected="$2" label="$3"
    if [ ! -f "${path}" ]; then
        die "${path} is missing. This file ships committed in-tree. If
            you hit this during a re-export, see the header of this script
            for the export procedure."
    fi
    local actual
    actual="$(sha256_of "${path}")"
    if [ "${actual}" != "${expected}" ]; then
        die "${label} SHA256 mismatch: expected ${expected}, got ${actual}.
            Either the committed file drifted or the pin is stale. Do NOT
            update the pin until you have re-validated detections on real
            audio (see experiments/ in the private repo for the harness)."
    fi
    log "${label} SHA256 OK"
}

# --- Main -------------------------------------------------------------------

mkdir -p "${MODEL_DIR}"

verify "${ONNX_FILE}"    "${YAMNET_ONNX_SHA256}"    "yamnet.onnx"
verify "${CLASSES_FILE}" "${YAMNET_CLASSES_SHA256}" "yamnet_classes.txt"

log "done. Models in ${MODEL_DIR}:"
ls -la "${MODEL_DIR}"
