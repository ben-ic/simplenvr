#!/usr/bin/env bash
# Build the tether supervisor binary and install it under Tauri sidecar
# naming (tether-<triple>[.exe]) into src-tauri/binaries/.
#
# tether is our cross-platform parent-death child supervisor. Source:
# src-tauri/tether/. See src-tauri/tether/src/main.rs for the
# per-platform mechanism (PDEATHSIG on Linux, Job Object on Windows,
# stdin-EOF watchdog on macOS).
#
# Usage:
#   scripts/build_tether.sh              # host triple
#   scripts/build_tether.sh --target <triple>

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN_DIR="${REPO_ROOT}/src-tauri/binaries"
mkdir -p "${BIN_DIR}"

TARGET=""
if [[ "${1:-}" == "--target" && -n "${2:-}" ]]; then
    TARGET="$2"
fi

detect_host_triple() {
    local rustc_out
    rustc_out="$(rustc -vV 2>/dev/null | awk '/host:/ {print $2}')"
    if [[ -z "${rustc_out}" ]]; then
        echo "error: rustc not found — install Rust toolchain first" >&2
        exit 1
    fi
    echo "${rustc_out}"
}

if [[ -z "${TARGET}" ]]; then
    TARGET="$(detect_host_triple)"
fi

echo "Building tether for ${TARGET}"

# Build tether outside the src-tauri workspace so app-only path
# dependencies (e.g. local plugin checkouts) don't block setup.
WORK_DIR="$(mktemp -d 2>/dev/null || mktemp -d -t simplenvr-tether)"
cleanup() {
    rm -rf "${WORK_DIR}" 2>/dev/null || true
}
trap cleanup EXIT

cp "${REPO_ROOT}/src-tauri/tether/Cargo.toml" "${WORK_DIR}/Cargo.toml"
cp -R "${REPO_ROOT}/src-tauri/tether/src" "${WORK_DIR}/src"

cd "${WORK_DIR}"

if [[ "${TARGET}" == "$(detect_host_triple)" ]]; then
    # Native build — use default target dir.
    cargo build --release
    SRC_BIN="${WORK_DIR}/target/release/tether"
else
    # Cross-compile — requires rustup target add <triple> beforehand.
    cargo build --release --target "${TARGET}"
    SRC_BIN="${WORK_DIR}/target/${TARGET}/release/tether"
fi

EXT=""
case "${TARGET}" in
    *windows*) EXT=".exe" ;;
esac

DEST="${BIN_DIR}/tether-${TARGET}${EXT}"
cp "${SRC_BIN}${EXT}" "${DEST}"
chmod +x "${DEST}" 2>/dev/null || true

echo "Installed: ${DEST}"
ls -lh "${DEST}"
