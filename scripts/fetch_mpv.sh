#!/usr/bin/env bash
#
# Download pre-built libmpv.2.dylib for macOS ARM64 (LGPL).
#
# Source: https://github.com/ben-ic/libmpv-macos
# Self-contained — all deps statically linked, only system frameworks needed.
#
# Usage:
#   ./scripts/fetch_mpv.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

TAG="v0.41.0-lgpl-arm64"
REPO="ben-ic/libmpv-macos"
DYLIB_NAME="libmpv.2.dylib"

# Where cargo looks for the library at link time
LIB_DIR="$REPO_ROOT/src-tauri/lib"
# Where the dylib lives for bundling into the .app
FRAMEWORKS_DIR="$REPO_ROOT/src-tauri/Frameworks"

mkdir -p "$LIB_DIR" "$FRAMEWORKS_DIR"

DEST="$LIB_DIR/$DYLIB_NAME"

if [ -f "$DEST" ]; then
  echo "[fetch_mpv] $DYLIB_NAME already present, skipping download"
else
  echo "[fetch_mpv] Downloading $DYLIB_NAME from $REPO@$TAG..."
  gh release download "$TAG" --repo "$REPO" --pattern "$DYLIB_NAME" --dir "$LIB_DIR"
  echo "[fetch_mpv] Downloaded to $DEST"
fi

# Create unversioned symlink for -lmpv
ln -sf "$DYLIB_NAME" "$LIB_DIR/libmpv.dylib"

# Copy to Frameworks dir for bundling
cp -f "$DEST" "$FRAMEWORKS_DIR/$DYLIB_NAME"
ln -sf "$DYLIB_NAME" "$FRAMEWORKS_DIR/libmpv.dylib"

echo "[fetch_mpv] Ready. lib=$LIB_DIR, frameworks=$FRAMEWORKS_DIR"
echo "[fetch_mpv] Size: $(du -sh "$DEST" | cut -f1)"
