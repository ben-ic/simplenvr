# SimpleNVR

A desktop app that finds the IP cameras on your network and records them. No accounts, no cloud, no subscriptions. Install it, give it your camera passwords, watch it work.

---

## Download

Pre-built installers for the current release:

- **Windows** — _[installer link]_
- **macOS** — _[installer link]_
- **Linux** — _[installer link]_

The macOS build is notarized, the Windows build is signed. Linux is provided as a `.deb` for Debian-family distros and a tarball for everything else.

---

## Installing

### Windows

Download the `.msi`, double-click, and click through the installer. SimpleNVR appears in the Start menu. Launch it like any other app — the first time you open it, Windows may ask for permission to allow it through the firewall. Say yes; SimpleNVR needs to reach your cameras on the local network.

### macOS

Open the `.dmg` and drag SimpleNVR to Applications. Launch it from Launchpad. On first launch macOS asks permission to access the local network — this is so SimpleNVR can discover your cameras. Say yes.

### Linux (Debian, Ubuntu, Pop!_OS)

```bash
sudo apt install ./simplenvr_*.deb
```

SimpleNVR appears in your application menu.

---

## What to expect on first launch

SimpleNVR walks you through a short first-run flow and then gets out of your way:

1. **It scans your network** and shows you every camera it can see, identified by brand where possible.
2. **It asks for your camera usernames and passwords.** We show you the factory defaults for common brands as a starting point. If your network has four Reolinks and the username is `admin` on all of them, you type it once.
3. **It starts recording.** As soon as a camera is authenticated, it starts writing video to disk immediately — you don't need to click a "begin recording" button.

After that, the home screen is a live grid of your cameras with a timeline of the day's motion events. There's nothing else to configure. SimpleNVR manages storage on its own — set a disk budget in Settings and it keeps a rolling buffer of footage up to that size. 30 days. 60 days. Whatever fits.

If a camera goes offline, a network blip reconnects automatically, ffmpeg crashes for any reason — SimpleNVR recovers silently in the background. You only see a notification when something actually needs you.

---

## What it does

- **Records 24/7** from any camera that speaks RTSP. Reolink, Amcrest, Annke, Eufy (with HomeBase), TP-Link Tapo, UniFi, and most generic ONVIF cameras are supported out of the box.
- **Detects motion** on every camera using OpenCV background subtraction, with multi-object tracking so one person walking across the yard is one event, not twenty.
- **Identifies people, vehicles, and animals** on-device using a small bundled AI model. No cloud, no subscription, no data leaving your machine.
- **Listens for sounds that matter** (glass breaking, alarms, dogs barking) on cameras that have microphones.
- **Plays back any moment** with a day-scrubber across all cameras. Click a time, see what was happening.
- **Works for up to 32 cameras** on a normal laptop or desktop. Beyond that you want something different — and we'll be honest and tell you so.

Everything runs locally. SimpleNVR does not send your video anywhere, does not require an account, and does not talk to any servers we run.

---

## Supported platforms

- **Windows 11 on ARM64** (Snapdragon X Elite, Copilot+ PCs) — primary deployment target
- **macOS** on Apple Silicon (M1 and newer) — first-class support
- **Windows 11 on x86_64** (Intel/AMD) — supported
- **macOS Intel** (2016+) — supported
- **Linux x86_64** — best-effort; runs fine, not part of the regular release flow

System requirements:

- Any modern CPU from the last 5 years (4 cores or more recommended)
- 8 GB of RAM for up to 8 cameras, 16 GB for 8–32 cameras
- A dedicated drive (or a folder on a large drive) with enough space for the retention you want — plan for roughly 50 GB per camera per day at full quality, or a lot less if you enable the storage-friendly recording mode

---

## Building from source

SimpleNVR is a [Tauri](https://tauri.app) desktop app with a Rust shell and a Python sidecar, bundled with [ffmpeg](https://ffmpeg.org) and [go2rtc](https://github.com/AlexxIT/go2rtc). Building it requires all three toolchains.

### Prerequisites

- **Rust** (stable) — install via [rustup](https://rustup.rs)
- **Node.js 20 LTS** with **npm**
- **Python 3.11** with `venv` support
- Platform build tools:
  - **macOS**: Xcode Command Line Tools (`xcode-select --install`)
  - **Windows**: MSVC build tools (Visual Studio 2022 Community covers it) — see `BUILD-WINDOWS.md` for the full step-by-step
  - **Linux**: `build-essential`, `libwebkit2gtk-4.1-dev`, `libayatana-appindicator3-dev`, `librsvg2-dev`, `libssl-dev`

### Clone and set up

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
./scripts/fetch_ffmpeg.sh
./scripts/fetch_go2rtc.sh
./scripts/fetch_yolox.sh
./scripts/fetch_yamnet.sh
./scripts/build_tether.sh
./scripts/bundle_python.sh
```

On Windows, run the `.ps1` equivalents under `scripts/` in PowerShell instead.

### Run in development mode

From the repo root:

```bash
cargo tauri dev
```

**Important:** use `cargo tauri dev`, not `npm run tauri dev`. The npm command panics regardless of working directory; the cargo command is the supported path.

Dev mode gives you hot-reload on the frontend (Vite on `http://localhost:3000`), live backend logs in your terminal, and a crash stack trace if the Python side blows up.

### Editing backend Python code

The Python backend that `cargo tauri dev` runs is the **bundled** PyInstaller build from `src-tauri/binaries/simplenvr-backend-dir/`, not your `.py` files. If you edit anything under `backend/`, rerun `./scripts/bundle_python.sh` to rebuild the bundle, then restart `cargo tauri dev` for the changes to take effect.

### Build an installer

```bash
# macOS
cargo tauri build --bundles dmg

# Windows  (run from Windows; see BUILD-WINDOWS.md)
cargo tauri build --bundles msi

# Linux
cargo tauri build --bundles deb
```

The finished installer lands in `src-tauri/target/release/bundle/`.

---

## How it all fits together

Four moving pieces:

- **Tauri Rust shell** (`src-tauri/`) — the window, the process supervisor, the installer
- **Python backend** (`backend/`) — camera discovery, recording management, motion detection, object classification, audio classification, HTTP API, WebSocket event bus
- **React + TypeScript frontend** (`frontend/`) — the UI, built with Vite
- **Bundled native binaries** (`src-tauri/binaries/`) — ffmpeg for recording, go2rtc for RTSP fan-out, tether for cross-platform parent-death supervision

For the full architectural picture, see [`docs/architecture.md`](docs/architecture.md). For the product philosophy — who SimpleNVR is for, what we deliberately don't build, and the "can a non-technical user do this?" test every feature passes — see [`docs/product.md`](docs/product.md).

---

## Not supported (by design)

SimpleNVR records what's on your network. Cameras that stream exclusively through the manufacturer's cloud — **Ring, Blink, Google Nest, Amazon, stock Wyze, TP-Link Kasa, Arlo without a local hub, Xiaomi** — don't expose a local video feed and therefore can't be recorded by any local NVR. This is a vendor architecture choice, not a SimpleNVR limitation we intend to fix. Keep using the manufacturer's own app for those cameras.

Battery-powered motion-wake cameras work when paired with their local hub (Eufy HomeBase, Reolink Home Hub, Arlo SmartHub), and they appear as "asleep" between wake events rather than as "offline."

---

## Questions and issues

Found a bug or have a question? [github.com/ben-ic/simplenvr/issues](https://github.com/ben-ic/simplenvr/issues)
