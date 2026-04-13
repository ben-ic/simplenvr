#!/usr/bin/env bash
#
# Ad-hoc codesign the .app bundle (deep) and produce a clean .zip
# suitable for GitHub Releases. Without this, macOS Gatekeeper
# reports the app as "corrupted" because PyInstaller's nested
# dylibs/SOs lack signatures.
#
# Users still need to right-click → Open on first launch (no Apple
# Developer notarization), but the app won't be flagged as damaged.
#
set -euo pipefail

APP="src-tauri/target/release/bundle/macos/SimpleNVR.app"

if [ ! -d "$APP" ]; then
  echo "Error: $APP not found. Run 'cargo tauri build' first." >&2
  exit 1
fi

# Copy bundled libmpv into Frameworks/ if not already there.
FRAMEWORKS="$APP/Contents/Frameworks"
mkdir -p "$FRAMEWORKS"
LIBMPV_SRC="src-tauri/Frameworks/libmpv.2.dylib"
if [ -f "$LIBMPV_SRC" ]; then
  echo "Bundling libmpv.2.dylib into Frameworks/..."
  cp -f "$LIBMPV_SRC" "$FRAMEWORKS/libmpv.2.dylib"
elif [ ! -f "$FRAMEWORKS/libmpv.2.dylib" ]; then
  echo "Warning: libmpv.2.dylib not found. Run scripts/fetch_mpv.sh first." >&2
fi

echo "Signing nested binaries..."

# Sign innermost binaries first (dylibs, .so files, executables inside
# the sidecar), then the outer app. codesign requires inside-out order.
# Use a for loop instead of piping to avoid pipefail issues.
while IFS= read -r -d '' bin; do
  codesign --force --sign - "$bin" 2>/dev/null || true
done < <(find "$APP/Contents/Resources" "$APP/Contents/Frameworks" \
  -type f \( -name "*.dylib" -o -name "*.so" -o -perm +111 \) \
  -print0 2>/dev/null || true)

echo "Signing main executable..."
codesign --force --sign - "$APP/Contents/MacOS/app"

echo "Signing app bundle..."
codesign --force --deep --sign - "$APP"

echo "Verifying..."
codesign --verify --deep --strict "$APP" 2>&1 && echo "Signature OK" || {
  echo "Warning: deep verification reported issues (may still work with right-click → Open)"
}

# Produce a zip without quarantine attributes. ditto is the macOS-native
# tool that preserves resource forks and doesn't inject xattrs.
VERSION="$(grep '"version"' src-tauri/tauri.conf.json | head -1 | sed 's/.*"\([0-9.]*\)".*/\1/')"
ZIP="src-tauri/target/release/bundle/macos/SimpleNVR_${VERSION}_macos_arm64.zip"
echo "Creating ${ZIP}..."
ditto -c -k --keepParent "$APP" "$ZIP"

echo "Done. Size: $(du -sh "$ZIP" | cut -f1)"
