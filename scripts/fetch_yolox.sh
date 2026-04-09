#!/usr/bin/env bash
# Download pinned YOLOX ONNX weights from the Megvii release and drop
# them into backend/classification/models/ for bundling into the
# PyInstaller sidecar.
#
# Usage:
#   scripts/fetch_yolox.sh
#
# What it fetches:
#   - yolox_nano.onnx  — 3.5 MB, used by the capability probe for the
#                        calibration benchmark AND as the production
#                        model on the Modest/Weak hardware tiers.
#   - yolox_s.onnx     — 34 MB, the production model on Strong/Normal
#                        tiers. Quantization for Snapdragon NPU (QNN EP)
#                        is a separate x86_64-only CI step; this script
#                        only fetches the stock FP32 weights.
#
# License:
#   Both files are Apache-2.0 from
#   github.com/Megvii-BaseDetection/YOLOX release 0.1.1rc0. This script
#   writes a NOTICE.txt alongside them with the attribution text. See
#   docs/object-classification.md for the full license audit.
#
# Cross-platform:
#   YOLOX ONNX is platform-independent — same bytes work on
#   Windows/macOS/Linux, x64 and arm64. No per-triple builds needed.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST_DIR="${REPO_ROOT}/backend/classification/models"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

# --- Pinned release ---------------------------------------------------------

YOLOX_RELEASE="0.1.1rc0"
YOLOX_BASE="https://github.com/Megvii-BaseDetection/YOLOX/releases/download/${YOLOX_RELEASE}"

YOLOX_NANO_URL="${YOLOX_BASE}/yolox_nano.onnx"
# SHA256 pinned from the first successful fetch on 2026-04-09. If Megvii
# ever re-publishes the same filename with different bytes we want a
# loud mismatch, not a silent model swap — this script refuses to
# install any file that doesn't match.
YOLOX_NANO_SHA256="c789161ed43c8269fcd4e67c67eeeb4e80c622da2eb296a20bc6007bd18a0b7d"

YOLOX_S_URL="${YOLOX_BASE}/yolox_s.onnx"
YOLOX_S_SHA256="c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063"

# --- Helpers ----------------------------------------------------------------

log() { printf '[fetch_yolox] %s\n' "$*"; }
die() { printf '[fetch_yolox] ERROR: %s\n' "$*" >&2; exit 1; }

sha256_of() {
    if command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
    else
        sha256sum "$1" | awk '{print $1}'
    fi
}

verify_sha() {
    local file="$1" expected="$2" name="$3"
    local actual
    actual="$(sha256_of "${file}")"
    if [ "${expected}" = "__PIN_AFTER_FIRST_RUN__" ]; then
        log "WARNING: ${name} SHA256 not yet pinned. Computed: ${actual}"
        log "         Edit scripts/fetch_yolox.sh and pin this hash."
        return 0
    fi
    if [ "${actual}" != "${expected}" ]; then
        die "${name} SHA256 mismatch: expected ${expected}, got ${actual}"
    fi
    log "${name} SHA256 OK"
}

download() {
    local url="$1" dest="$2"
    log "downloading ${url}"
    curl -fSL --retry 3 -o "${dest}" "${url}"
}

fetch_one() {
    local name="$1" url="$2" expected_sha="$3"
    local tmp="${TMP_DIR}/${name}"
    download "${url}" "${tmp}"
    verify_sha "${tmp}" "${expected_sha}" "${name}"
    install -m 0644 "${tmp}" "${DEST_DIR}/${name}"
    log "installed ${DEST_DIR}/${name}"
}

# --- Main -------------------------------------------------------------------

mkdir -p "${DEST_DIR}"

fetch_one "yolox_nano.onnx" "${YOLOX_NANO_URL}" "${YOLOX_NANO_SHA256}"
fetch_one "yolox_s.onnx"    "${YOLOX_S_URL}"    "${YOLOX_S_SHA256}"

# Write the NOTICE file alongside the models. This is what the PyInstaller
# bundle ships to satisfy Apache-2.0 §4(d) attribution when we redistribute
# the weights in a commercial installer.
cat > "${DEST_DIR}/NOTICE.txt" <<'EOF'
YOLOX
Copyright (c) 2021-2024 Megvii Inc. All rights reserved.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

Source: https://github.com/Megvii-BaseDetection/YOLOX
Release: 0.1.1rc0
EOF

log "done. Models in ${DEST_DIR}:"
ls -la "${DEST_DIR}"
