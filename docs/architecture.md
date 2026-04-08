# SimpleNVR — Architecture

A technical overview of how SimpleNVR is put together. For the *why* behind the product decisions, see `product.md`. This document describes the *how*.

---

## Overview

SimpleNVR is a Tauri desktop application with a Python backend sidecar and a Go (go2rtc) RTSP fan-out service. It bundles everything it needs into a single installable `.app` / `.msi` / `.dmg` and runs entirely on the user's machine with no external services.

```
┌──────────────────────────────────────────────────────────────────────┐
│ SimpleNVR.app / SimpleNVR.exe                                        │
│                                                                      │
│  ┌───────────────────────────────────────────────────────────────┐   │
│  │ Tauri Rust shell (src-tauri/src/lib.rs)                       │   │
│  │ - Window management, native menus, crash dialogs              │   │
│  │ - Single-instance lock (tauri-plugin-single-instance)         │   │
│  │ - Spawns sidecars through tether (parent-death supervisor)    │   │
│  │ - Graceful shutdown on window close                           │   │
│  └──────────┬──────────────────────────────────┬─────────────────┘   │
│             │                                  │                    │
│             ▼                                  ▼                    │
│  ┌─────────────────────┐        ┌───────────────────────────────┐    │
│  │ tether → go2rtc     │        │ tether → simplenvr-backend    │    │
│  │ (Go, MIT, ~6 MB)    │        │ (Python, PyInstaller ~22 MB)  │    │
│  │ ┌─────────────────┐ │        │ ┌───────────────────────────┐ │    │
│  │ │ HTTP admin :1984│ │        │ │ FastAPI HTTP + WebSocket  │ │    │
│  │ │ RTSP :8554      │ │        │ │ Discovery (ONVIF etc)     │ │    │
│  │ └─────────────────┘ │        │ │ Recorder manager          │ │    │
│  └─────────────────────┘        │ │ Motion detector           │ │    │
│             ▲                   │ │ Storage janitor           │ │    │
│             │                   │ │ SQLite state              │ │    │
│             │ one RTSP per      │ └────────────┬──────────────┘ │    │
│             │ camera             │              │                │    │
│             │                   │              ▼                │    │
│             │                   │  ┌─────────────────────────┐  │    │
│             │                   │  │ tether → ffmpeg × N      │  │    │
│             │                   │  │ (one per camera, unified │  │    │
│             │                   │  │  pipeline with 3 outputs)│  │    │
│             │                   │  └─────────────────────────┘  │    │
│             │                   └───────────────────────────────┘    │
│             │                                                         │
│  ┌──────────┴─────────┐                                              │
│  │ Real IP cameras    │  ← exactly ONE RTSP connection per camera    │
│  │ on the LAN         │                                              │
│  └────────────────────┘                                              │
└──────────────────────────────────────────────────────────────────────┘
```

---

## The process tree

Every parent-child relationship uses the `tether` supervisor for cross-platform parent-death cleanup. See `src-tauri/tether/src/main.rs` for the mechanism (`PR_SET_PDEATHSIG` on Linux, Job Objects on Windows, stdin-EOF watchdog on macOS).

```
Tauri shell
├── tether → go2rtc
└── tether → simplenvr-backend (Python)
    ├── tether → ffmpeg (camera 1)
    ├── tether → ffmpeg (camera 2)
    └── tether → ffmpeg (camera N)
```

### Lifecycle invariants

1. **Exactly one instance runs at a time.** `tauri-plugin-single-instance` uses a Unix socket (macOS/Linux) or named mutex (Windows). A second launch focuses the existing window.
2. **When the parent dies, all descendants die.** Every edge in the tree is tethered. No process scanning, no PID files, no cleanup heuristics.
3. **Graceful shutdown preserves recording integrity.** Tauri SIGTERMs Python through tether → Python runs its FastAPI lifespan shutdown → terminates each ffmpeg cleanly so the last segment's moov atom is written.
4. **Crashes recover without user intervention.** If an individual ffmpeg dies, Python restarts it with backoff. If Python dies, Tauri shows a crash dialog (rare — should not normally happen).

---

## Subsystems

### Discovery — `backend/discovery/`

Finds IP cameras on the LAN without user input.

- **`scanner.py`** — the discovery loop. Runs periodic ONVIF WS-Discovery probes, tracks camera state, fires events on the backend event bus.
- **`ws_discovery.py`** — low-level ONVIF WS-Discovery (multicast UDP).
- **`onvif_client.py`** — authenticated ONVIF queries (device info, profiles, RTSP URIs). Runs once the user provides credentials.
- **`fingerprints.py`** — (planned) static database of brand identification signals: DHCP hostname patterns, MAC OUI prefixes, ONVIF scope patterns, HTTP header patterns, RTSP URL patterns.
- **`identifier.py`** — (planned) multi-signal scoring function. Combines reverse DNS, MAC lookup, ONVIF scopes, HTTP probe, and the user's declared-brands setting into a confidence-scored brand identification.
- **`rtsp_probe.py`** — verifies an RTSP URI is actually reachable. Used for stale-camera detection.
- **`mac_lookup.py`** — ARP table lookup + OUI database for brand from MAC address.
- **`auth_backoff.py`** — per-camera auth retry backoff so we don't hammer cameras with bad credentials.

### Recording — `backend/recording/`

One process per camera, one segment file per recording period.

- **`manager.py`** — `RecordingManager` orchestrates per-camera recorders, reacts to camera state changes on the event bus, runs the storage janitor.
- **`camera_recorder.py`** — per-camera `CameraRecorder`. Spawns ffmpeg via tether, monitors its stderr for progress heartbeat, restarts on failure with backoff, handles segment completion events.
- **`codec.py`** — builds the unified ffmpeg command line. One ffmpeg instance, three outputs: (1) stream-copy to disk segments, (2) scene-detection JPEG frames for motion, (3) fps-limited MJPEG for preview fan-out.
- **`frame_broadcaster.py`** — per-camera MJPEG fan-out with a latest-frame cache. Bounded async queue per subscriber, drop-oldest semantics.
- **`storage.py`** — retention math: `retention_days = budget / current_bitrate`, where `budget` is the user's configured storage limit, not free disk space.
- **`janitor.py`** — periodic cleanup: delete expired segments, prune orphan files not in the DB, enforce storage budget.

### Motion — `backend/motion/`

Motion detection runs on top of the recording pipeline's scene-detect output, so there's no separate ffmpeg process just for motion.

- **`manager.py`** — subscribes to recorder events, attaches a motion detector to each active camera's frame broadcaster.
- **`detector.py`** — pure JPEG consumer: reads frames, computes simple pixel-diff motion scores, writes events to SQLite.

### API — `backend/api/`

FastAPI routes plus a WebSocket event bus.

- **`cameras.py`** — CRUD + auth + preview endpoints
- **`streams.py`** — MJPEG preview fan-out endpoint
- **`recordings.py`** — recording list, segment download, playback
- **`settings.py`** — user-visible settings (storage budget, recording path, etc.)
- **`ws.py`** — WebSocket event bus for discovery updates, motion events, storage updates

---

## Data flow — a frame's life

```
1. Camera sends RTSP stream
         │
         ▼
2. go2rtc receives the single RTSP connection
   and buffers the packets
         │
         ▼
3. ffmpeg reads rtsp://127.0.0.1:8554/<uuid>
   (loopback, zero bandwidth cost)
         │
         ├──► Output 1: -c copy -f segment
         │    Stream-copy raw H.264 to .mp4 segments on disk
         │    (zero CPU, zero re-encoding, zero MPEG-LA liability)
         │
         ├──► Output 2: scene filter + scale + image2pipe
         │    JPEG frames at motion-scene changes
         │    → frame_broadcaster → motion detector
         │
         └──► Output 3: fps=10 + scale + fifo (TCP loopback)
              MJPEG stream on 127.0.0.1:<port>
              → frame_broadcaster → HTTP MJPEG endpoint → UI <img>
```

The three-output unified pipeline means we pay the RTSP decode cost once, not three times. Each output is independently tuned for its consumer.

---

## Key design decisions

### One RTSP per camera, via go2rtc

Cheap IP cameras (Eufy, no-name Tapos) enforce strict concurrent-client limits — often just 1 or 2. If recording, motion, and preview each open their own RTSP connection, the camera flaps. We let go2rtc hold a single RTSP connection to each camera and fan out the stream internally. Every downstream consumer reads from `rtsp://127.0.0.1:8554/<uuid>` instead.

### Stream-copy recording, no transcoding

Raw H.264 from the camera is copied byte-for-byte into segment files via `ffmpeg -c copy`. No re-encoding means:
- Zero CPU cost for recording
- No MPEG-LA patent liability (we are not an "encoder" or "decoder" in the patent sense, we are a storage service for someone else's already-encoded stream)
- Maximum quality (lossless, since we're not decoding-and-re-encoding)
- Smaller binary footprint (no libx264 in the ffmpeg build)

### tether — cross-platform parent-death supervisor

See `src-tauri/tether/src/main.rs`. A tiny supervisor binary that encapsulates per-OS process-lifecycle primitives behind a uniform interface. Linux uses `PR_SET_PDEATHSIG`, Windows uses Job Objects with `KILL_ON_JOB_CLOSE`, macOS uses a stdin-EOF watchdog thread (the only cross-platform primitive the macOS kernel exposes). Application code in Tauri and Python has zero per-OS branching around process lifecycle — they just spawn through tether and the right mechanism kicks in.

### Python for orchestration, not Rust

The orchestration layer (ONVIF, FastAPI, SQLite async, process supervision of ffmpegs) is written in Python. This is a deliberate trade-off:

- **Pro**: Python has the only mature ONVIF library ecosystem (`zeep`, `onvif-zeep`, `wsdiscovery`). Rewriting ONVIF parsing in Rust would be months of work with no user-visible benefit.
- **Pro**: FastAPI is genuinely pleasant for a small HTTP API. aiohttp-in-Rust equivalents exist but are more ceremony.
- **Pro**: Hot-reload during dev is faster with Python.
- **Con**: PyInstaller cold-start takes ~10 seconds. Accepted as a trade-off.
- **Con**: Binary is 22 MB vs ~5 MB for a Rust equivalent. Also accepted.

The Rust layer (Tauri shell + tether) handles the things Rust is best at: native windowing, filesystem permissions, process lifecycle, signing/notarization.

### Hub devices (Eufy HomeBase, Reolink NVR, etc.)

Some brands organize cameras behind a hub that's the only thing on the network. The cameras themselves may be battery-powered and sleep between motion events — they don't have their own LAN presence. The hub is always on, has its own IP and hostname, and serves RTSP streams for the cameras behind it on different paths.

SimpleNVR models this with two device types:

- **`camera`** — a direct IP camera with its own LAN presence
- **`hub`** — a gateway device (Eufy HomeBase, Reolink Home Hub, Arlo SmartHub)
- **`hub_camera`** — a camera that lives behind a hub; has no direct LAN address

Discovery finds hubs the same way it finds cameras (ONVIF WS-Discovery, reverse DNS, MAC OUI). After the user authenticates to a hub, we enumerate the cameras behind it either via the brand's API or by probing the known RTSP path patterns (`/live0`, `/live1`, etc. for Eufy; channel-per-path for Reolink). Each hub-camera gets its own entry in the `cameras` table with `parent_hub_id` set to the hub.

**Sleep handling is a UX concern, not a technical failure.** Sleeping hub-cameras are marked `status: "asleep"` rather than `"offline"` or `"error"`. The dashboard tile shows a distinct "asleep, will wake on motion" state with the last-motion timestamp. When the camera wakes and the RTSP stream starts delivering frames again, the tile transitions to live view automatically. We never show "Can't connect" for a camera that's in its designed sleep state — that would be a user-facing lie.

### SQLite for state

Zero-config, file-based, adequate for 32 cameras. Lives in the user's app data directory. WAL mode for concurrent reads during writes.

### Bundled binaries, not dependencies on the host

FFmpeg, ffprobe, go2rtc, and tether are all bundled inside the app package. We do not depend on the user having ffmpeg installed. This is non-negotiable for the "it just works" promise — asking a a non-technical user to `brew install ffmpeg` is a showstopper.

---

## Where things live

```
backend/                  Python sidecar
  main.py                 FastAPI app + lifespan + stdin watchdog
  config.py               Environment-variable config resolution
  port_finder.py          TOCTOU-safe port picker
  ffmpeg_path.py          Resolves bundled ffmpeg/ffprobe paths
  go2rtc_client.py        Async HTTP client for go2rtc admin API
  process_cleanup.py      Legacy orphan killers (deprecated, kept for reference)
  db.py                   aiosqlite + migration helpers
  models.py               Pydantic dataclasses (Camera, Settings, etc.)
  discovery/              Camera discovery + identification
  recording/              Per-camera recording + storage management
  motion/                 Motion detection
  api/                    FastAPI routes + WebSocket event bus
  main.spec               PyInstaller spec

src-tauri/                Tauri Rust shell + supervisor
  Cargo.toml              Workspace root
  src/lib.rs              Sidecar spawning, lifecycle, crash dialogs
  build.rs                Forwards TARGET_TRIPLE to rustc env
  tauri.conf.json         Tauri config (externalBin, bundle settings)
  capabilities/           Tauri shell allow-lists
  tether/                 Cross-platform parent-death supervisor (see below)
    Cargo.toml
    src/main.rs
  binaries/               Bundled binaries (gitignored, fetched by scripts)
  resources/              License attribution files

frontend/                 React + TypeScript + Vite
  src/components/         Dashboard, Discovery, Playback, Settings, etc.
  src/hooks/              useStorage, useDiscovery
  src/api/client.ts       Backend REST client
  src/lib/backend.ts      apiUrl / apiFetch / wsUrl helpers
  vite.config.ts          Dev server proxy to backend

scripts/                  Build helpers (bash + PowerShell variants)
  fetch_ffmpeg.sh         Download LGPL-clean FFmpeg for target triple
  fetch_go2rtc.sh         Download pinned go2rtc release
  bundle_python.sh        PyInstaller onefile build
  build_tether.sh         Compile and install tether for target triple

docs/                     This documentation
  product.md              Why SimpleNVR exists and who it's for
  architecture.md         This file
```

---

## Build pipeline

Three ordered steps produce a shippable bundle:

```bash
# 1. Fetch or build each bundled binary into src-tauri/binaries/
scripts/fetch_ffmpeg.sh
scripts/fetch_go2rtc.sh
scripts/bundle_python.sh    # PyInstaller onefile → simplenvr-backend
scripts/build_tether.sh     # cargo build -p tether → tether binary

# 2. Build the Tauri bundle (from repo root, NOT from src-tauri/)
cargo tauri build --bundles app       # macOS .app
cargo tauri build --bundles dmg       # macOS .dmg
cargo tauri build --bundles msi       # Windows
cargo tauri build --bundles deb       # Linux
```

`beforeBuildCommand` in `tauri.conf.json` is `cd ../frontend && npm run build`. This path is resolved relative to the `cargo tauri` invocation cwd (**not** `tauri.conf.json`'s location) — so `cargo tauri build` must be run from the repo root for the frontend build to find its directory.

---

## Supported platforms

- **Primary target**: Windows ARM64 (Snapdragon X Elite / Copilot+ PCs). This is our deployment goal.
- **First-class target**: macOS Apple Silicon (aarch64-apple-darwin). Primary development host.
- **Supported**: macOS Intel (x86_64-apple-darwin), Windows x86_64 (x86_64-pc-windows-msvc).
- **Best-effort**: Linux x86_64 (x86_64-unknown-linux-gnu). Runs, but we don't ship installers for it in the main release flow.

Cross-compilation is supported for FFmpeg (via BtbN pre-built binaries) and tether (via `cargo build --target`). The Python sidecar must be built on the target platform because PyInstaller doesn't cross-compile cleanly.
