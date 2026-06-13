#!/usr/bin/env bash
#
# Provision libmpv.2.dylib for macOS (LGPL) from the local Homebrew install.
#
# Originally this pulled a pre-built arm64 dylib from ben-ic/libmpv-macos, but
# that repo is gone and the dylib it shipped was arm64-only — so Intel Macs
# (and anyone without access to that repo) couldn't link. libmpv from Homebrew
# is LGPL, matches the host architecture (x86_64 on Intel, arm64 on Apple
# Silicon), and is what tauri-plugin-rtsp-mosaic itself documents for macOS dev.
#
# What this does:
#   1. Locates libmpv.2.dylib from `brew --prefix mpv` (host arch).
#   2. Copies it into src-tauri/lib (link time) and src-tauri/Frameworks
#      (bundle time), matching what src-tauri/build.rs expects.
#   3. Rewrites the dylib's install name to @rpath/libmpv.2.dylib so the
#      app finds it via the rpath build.rs sets (@executable_path/../Frameworks).
#
# Note: the Homebrew dylib links its own dependencies (libass, ffmpeg, etc.)
# via absolute Homebrew paths, so the copied library is self-sufficient for
# DEV runs on this machine but is NOT relocatable for a shippable .app. A
# distributable build still needs a self-contained libmpv (vendored or built
# with bundled deps); see docs/build.md.
#
# Usage:
#   ./scripts/fetch_mpv.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

DYLIB_NAME="libmpv.2.dylib"

# Where cargo looks for the library at link time
LIB_DIR="$REPO_ROOT/src-tauri/lib"
# Where the dylib lives for bundling into the .app
FRAMEWORKS_DIR="$REPO_ROOT/src-tauri/Frameworks"

log() { printf '[fetch_mpv] %s\n' "$*"; }
die() { printf '[fetch_mpv] ERROR: %s\n' "$*" >&2; exit 1; }

command -v brew >/dev/null 2>&1 || die "Homebrew not found. Install it (https://brew.sh) then 'brew install mpv'."

# Resolve the Homebrew mpv prefix; install it if absent.
if ! MPV_PREFIX="$(brew --prefix mpv 2>/dev/null)" || [ ! -d "$MPV_PREFIX" ]; then
    log "mpv not installed via Homebrew; installing it now"
    brew install mpv
    MPV_PREFIX="$(brew --prefix mpv)"
fi

SRC="$MPV_PREFIX/lib/$DYLIB_NAME"
[ -f "$SRC" ] || SRC="$(/bin/ls "$MPV_PREFIX"/lib/libmpv.*.dylib 2>/dev/null | head -1 || true)"
[ -n "${SRC:-}" ] && [ -f "$SRC" ] || die "could not find libmpv in $MPV_PREFIX/lib. Try 'brew reinstall mpv'."

ARCH="$(uname -m)"
log "host arch: $ARCH"
log "source:    $SRC"
file "$SRC" | sed 's/^/[fetch_mpv]   /'

mkdir -p "$LIB_DIR" "$FRAMEWORKS_DIR"

install_copy() {
    local dest_dir="$1"
    local dest="$dest_dir/$DYLIB_NAME"
    cp -f "$SRC" "$dest"
    chmod u+w "$dest"
    # Make the bundled copy resolvable via the app's rpath. Harmless for dev
    # (absolute-path linking still works) but required for the .app layout.
    install_name_tool -id "@rpath/$DYLIB_NAME" "$dest" 2>/dev/null || true
    ln -sf "$DYLIB_NAME" "$dest_dir/libmpv.dylib"
}

install_copy "$LIB_DIR"
install_copy "$FRAMEWORKS_DIR"

log "installed $DYLIB_NAME -> $LIB_DIR and $FRAMEWORKS_DIR"
log "size: $(du -sh "$LIB_DIR/$DYLIB_NAME" | cut -f1)"
