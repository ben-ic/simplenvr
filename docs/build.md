# Building SimpleNVR from source

SimpleNVR is a [Tauri](https://tauri.app) desktop app with a Rust shell and a Python sidecar, bundled with [ffmpeg](https://ffmpeg.org) and [go2rtc](https://github.com/AlexxIT/go2rtc). Building it requires all three toolchains.

> **Windows-specific runbook**: Windows has enough platform-specific setup (winget, MSVC Build Tools, Developer PowerShell, WebView2, Windows Defender exclusions) that it gets its own document. See [`build-windows.md`](build-windows.md) for the full Windows pipeline end-to-end. The sections below cover the cross-platform flow and the macOS / Linux specifics.

---

## Prerequisites

- **Rust** (stable) — install via [rustup](https://rustup.rs)
- **Node.js 20 LTS** with **npm**
- **Python 3.11** with `venv` support
- Platform build tools:
  - **macOS**: Xcode Command Line Tools (`xcode-select --install`)
  - **Windows**: MSVC build tools (Visual Studio 2022 Community covers it) — see [`build-windows.md`](build-windows.md) for the full step-by-step
  - **Linux**: `build-essential`, `libwebkit2gtk-4.1-dev`, `libayatana-appindicator3-dev`, `librsvg2-dev`, `libssl-dev`

---

## Clone and set up

```bash
git clone https://github.com/ben-ic/simplenvr.git
cd simplenvr

# Create the Python virtual environment the backend runs from.
python3.11 -m venv .venv
source .venv/bin/activate                         # Windows: .venv\Scripts\activate
pip install -r backend/requirements.txt

# Install frontend dependencies.
(cd frontend && npm install)

# Fetch and build the bundled binaries into src-tauri/binaries/.
# SimpleNVR ships self-contained — the final installer includes every
# binary below, and nothing is installed system-wide on the user's machine.
./scripts/fetch_ffmpeg.sh      # LGPL-clean FFmpeg + ffprobe for your target triple
./scripts/fetch_go2rtc.sh      # RTSP fan-out service (Go binary, MIT-licensed)
./scripts/fetch_dfine.sh       # Verifies the bundled D-FINE-N ONNX detector weights (~15 MB, Apache-2.0, committed in-tree at backend/classification/models/)
./scripts/fetch_yamnet.sh      # YAMNet ONNX weights — the bundled audio classifier for glass-break / siren / bark / etc. (Apache-2.0)
./scripts/build_tether.sh      # Compiles tether, the cross-platform parent-death supervisor (Rust, MIT)
./scripts/build_cv2_wheel.sh   # LGPL-clean opencv-python-headless wheel (one-time per release; ~30-40 min on M-series). See docs/cv2-selfbuild.md
./scripts/bundle_python.sh     # PyInstaller onedir bundle of the Python backend. Auto-picks up the cv2 wheel above; fails loud if GPL FFmpeg deps leak into the bundle.
```

On Windows, run the `.ps1` equivalents under `scripts/` in PowerShell instead.

---

## Run in development mode

From the repo root:

```bash
cargo tauri dev
```

**Important:** use `cargo tauri dev`, not `npm run tauri dev`. The npm command panics regardless of working directory; the cargo command is the supported path.

Dev mode gives you hot-reload on the frontend (Vite on `http://localhost:3000`), live backend logs in your terminal, and a crash stack trace if the Python side blows up.

---

## Editing backend Python code

The Python backend that `cargo tauri dev` runs is the **bundled** PyInstaller build from `src-tauri/binaries/simplenvr-backend-dir/`, not your `.py` files. If you edit anything under `backend/`, rerun `./scripts/bundle_python.sh` to rebuild the bundle, then restart `cargo tauri dev` for the changes to take effect.

---

## Build an installer

```bash
# macOS
cargo tauri build --bundles dmg

# Windows  (run from Windows; see build-windows.md)
cargo tauri build --bundles msi

# Linux
cargo tauri build --bundles deb
```

The finished installer lands in `src-tauri/target/release/bundle/`.
