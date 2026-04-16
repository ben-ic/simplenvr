#!/usr/bin/env bash
# Verify libmpv development files are available for linking.

set -euo pipefail

log() {
    printf '[libmpv] %s\n' "$*"
}

die() {
    printf '[libmpv] ERROR: %s\n' "$*" >&2
    exit 1
}

case "$(uname)" in
    Linux)
        if pkg-config --exists mpv 2>/dev/null; then
            ver="$(pkg-config --modversion mpv 2>/dev/null || echo unknown)"
            log "found system libmpv via pkg-config (version: ${ver})"
            exit 0
        fi
        die "libmpv development files not found. Install them and rerun:

  sudo apt-get update
  sudo apt-get install -y libmpv-dev

If you're on WSL, run this inside your Linux distro shell, not in Windows PowerShell."
        ;;
    Darwin|MINGW*|MSYS*|CYGWIN*)
        # macOS/Windows use project-specific fetch scripts.
        exit 0
        ;;
    *)
        exit 0
        ;;
esac
