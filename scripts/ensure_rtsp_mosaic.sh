#!/usr/bin/env bash
# Ensure the sibling tauri-plugin-rtsp-mosaic checkout exists where
# Cargo path deps and frontend file: deps expect it.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PLUGIN_DIR="${REPO_ROOT}/../tauri-plugin-rtsp-mosaic"
PLUGIN_URL="https://github.com/ben-ic/tauri-plugin-rtsp-mosaic.git"

log() {
    printf '[rtsp-mosaic] %s\n' "$*"
}

die() {
    printf '[rtsp-mosaic] ERROR: %s\n' "$*" >&2
    exit 1
}

if [ ! -d "$PLUGIN_DIR/.git" ]; then
    log "cloning plugin repo to ${PLUGIN_DIR}"
    git clone "$PLUGIN_URL" "$PLUGIN_DIR"
else
    log "plugin repo present at ${PLUGIN_DIR}"
fi

[ -f "$PLUGIN_DIR/Cargo.toml" ] || die "missing Cargo.toml in ${PLUGIN_DIR}"
[ -f "$PLUGIN_DIR/guest-js/package.json" ] || die "missing guest-js/package.json in ${PLUGIN_DIR}"

# The frontend consumes the guest-js package via a local file: dependency.
# Build once so TypeScript declarations/dist artifacts are present.
if [ "${1:-}" = "--build-guest-js" ]; then
    log "building guest-js package"
    (
        cd "$PLUGIN_DIR/guest-js"
        npm install --silent
        npm run build --silent
    )
fi
