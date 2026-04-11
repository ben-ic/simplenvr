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
│  │ (Go, MIT, ~6 MB)    │        │ (Python, PyInstaller onedir)  │    │
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
- **`camera_recorder.py`** — per-camera `CameraRecorder`. Spawns ffmpeg via tether, monitors its stderr for progress heartbeat, restarts on failure with backoff, handles segment completion events. At spawn time, registers both the main and (if available) sub-stream with go2rtc and picks which loopback URL to hand ffmpeg based on the `record_substream_when_available` setting. See "Sub-stream recording for retention" under Key Design Decisions.
- **`codec.py`** — builds the unified ffmpeg command line. One ffmpeg instance, two outputs: (1) stream-copy to disk segments, (2) scene-filtered JPEG frames for motion detection (with an fps floor so quiet indoor scenes still produce frames). Live browser preview is NOT a third ffmpeg output — it's served by go2rtc directly via its WebRTC web component. See "Live preview path" below.
- **`frame_broadcaster.py`** — per-camera fan-out for the motion JPEG stream with a latest-frame cache. Bounded async queue per subscriber, drop-oldest semantics. Has a `close()` method that puts a `None` sentinel into every subscriber queue so consumers exit cleanly when the recorder is stopped.
- **`storage.py`** — retention math: `retention_days = budget / current_bitrate`, where `budget` is the user's configured storage limit, not free disk space. Sub-stream recording changes `current_bitrate` dramatically — typical Reolink main is ~6 Mbps vs sub at ~260 kbps (measured April 2026 on representative cameras), so retention multiplies by ~24× when the setting is on.
- **`janitor.py`** — periodic cleanup: delete expired segments, prune orphan files not in the DB, enforce storage budget.

**Settings propagation invariant (load-bearing, bit us once).** `CameraRecorder.__init__` captures `self._settings = settings` as a *reference*, not a snapshot. `RecordingManager.load_settings()` creates a **new** `Settings` instance and assigns it to `manager._settings` — it does not mutate in place. That means every existing `CameraRecorder` is pinned to the `Settings` instance that was current at its spawn moment, and the only way to propagate a settings change into live recorders is to **stop + restart them**. Consequently: any code path that changes settings must ensure `RecordingManager.apply_settings_change()` actually fires a restart, and the diff logic there must diff against a caller-supplied snapshot of the pre-change state. Before the POST `/api/settings` endpoint called `recorder.load_settings()` inline for response-consistency reasons, capturing "old" state *after* that reload silently produced `new vs new` comparisons and never triggered restarts — so the recorder flip was accidentally happening via the scanner's periodic `camera_updated` event several minutes later instead of via the intended apply-settings path. The current code snapshots `recorder.settings.model_copy()` + `recorder.recordings_dir` in `api/settings.py` **before** touching anything, then passes both into `apply_settings_change(old_settings, old_recordings_dir)` as explicit parameters. Future changes to settings handling must preserve that ordering or pre-existing toggles (fps, segment duration, path, enabled, sub-stream) stop taking effect.

### Motion — `backend/motion/`

Motion detection runs on top of the recording pipeline's scene-detect output, so there's no separate ffmpeg process just for motion.

- **`manager.py`** — subscribes to recorder events, attaches a motion detector to each active camera's frame broadcaster. Threads the classifier through to every detector it spawns.
- **`detector.py`** — additive MOG2 + IOU tracker layer over the scene-filtered JPEG stream. Decode JPEG → `cv2.BackgroundSubtractorMOG2` (history=500, varThreshold=25, shadows=off) → morphological open → `cv2.findContours` → filter below 200px area → feed bboxes to tracker. Green frame corruption guard skips H.264 macroblock corruption. Soft `_CV2_AVAILABLE` guard so missing OpenCV silently disables the spatial layer.
- **`tracker.py`** — pure-function IOU multi-object tracker. Greedy assignment, EMA bbox smoothing, promotion gate (2 frames), idle timeout (2s). Each promoted track → `tracked_events` DB row → classifier submission on close. 17 unit tests.

### Classification — `backend/classification/`

YOLOX-based object classification. One shared ORT session per backend process.

- **`classifier.py`** — `YoloxClassifier` owns the ONNX Runtime session. Scores full-resolution crops from recording MP4s (with preview fallback). Per-label max across anchors, median-of-track across frames. Three labels: person/vehicle/animal. Threshold 0.30 for full-res, 0.20 for preview fallback. Dual-threshold tuned from empirical calibration in April 2026.
- **`manager.py`** — `ClassificationManager` drains a bounded `asyncio.Queue(64)` with drop-oldest overflow. Writes per-track verdict to `tracked_events`, then runs multi-track collapse: highest-confidence labeled track wins → written to `motion_events.object_class`. Emits `motion_event_updated` on the event bus.
- **`capability_probe.py`** — cross-platform hardware probe (CoreML/QNN/DirectML/OpenVINO/CUDA/CPU). 20-inference warmup benchmark, bucket into strong/normal/modest/weak/disabled tiers. Fingerprint-cached. Five env escape hatches.
- **`labelmap.py`** — COCO-80 → {person, vehicle, animal, None} collapse.
- **`models/`** — `yolox_nano.onnx` (3.5 MB) and `yolox_s.onnx` (34 MB), gitignored, fetched by `scripts/fetch_yolox.sh`.

### Audio — `backend/audio/`

YAMNet-based audio classification. Runs 24/7 on every camera with an audio track. ~10ms/window on CPU, no hardware tiering needed.

- **`classifier.py`** — `YamnetClassifier` owns the ONNX Runtime session. Classifies 0.96s PCM windows (16kHz mono). Outputs one of ~10 labels in two priority tiers. Confidence thresholds: 0.50 (high-priority), 0.30 (low-priority).
- **`manager.py`** — `AudioManager` subscribes to per-camera AudioBroadcasters via `audio_available` events. High-priority sounds (glass_break, gunshot, scream, siren) create independent `motion_events` rows with `source='audio'`. Low-priority sounds (bark, car_horn, door_slam, doorbell, meow, footsteps) enrich recent vision events only. Per-camera per-label cooldown prevents flood.
- **`labelmap.py`** — AudioSet 521-class → {glass_break, gunshot, scream, siren, bark, car_horn, door_slam, doorbell, meow, footsteps, None} collapse. Trust hierarchy: high-priority classes fire alone, low-priority only enrich.

The audio ffmpeg is a separate lightweight process per camera (audio-only extraction from go2rtc loopback, no extra camera RTSP connection). If the camera has no audio track, the ffmpeg exits immediately and audio classification is skipped.

### Summarizer — not currently shipping

An on-device VLM summarizer (natural-language event descriptions) was prototyped and removed. The `motion_events.summary` and `motion_events.description` columns remain in the schema, and the classifier's `_summarizer` hook point remains, so a future summarizer can slot in without migration.

### Story — `backend/story/`

Template-based story compiler. Deterministic, instant, no cloud LLM needed.

- **`compiler.py`** — Groups events by camera + 5-min time window, collapses by class ("23 vehicles passed"), formats sentences from templates. Produces `StoryDigest` with per-camera summaries. 20 unit tests. Runs templates-only today; if a future summarizer lands, the compiler will consume its descriptions.

### API — `backend/api/`

FastAPI routes plus a WebSocket event bus.

- **`cameras.py`** — CRUD + auth + camera-delete endpoints
- **`streams.py`** — empty placeholder (live preview is served by go2rtc directly)
- **`recordings.py`** — recording list, segment download, playback
- **`motion.py`** — motion event endpoints:
  - `GET /today` — Today view: notable person events (cards) + per-camera vehicle/animal counts
  - `GET /motion_events/recent` — flat labeled event list (noise-gated, `?all=true` bypasses)
  - `GET /motion_events/search?q=` — LIKE search across summary + description
  - `GET /episodes/recent` — grouped episodes (same camera within 5 min)
  - `GET /story/today` — compiled story digest via template engine
  - `GET /motion_events/timeline` — per-camera timeline for recordings view
  - `GET /motion_events/{id}/thumbnail.jpg` — thumbnail with path containment check
- **`settings.py`** — user-visible settings (storage budget, recording path, declared brands, etc.)
- **`ws.py`** — WebSocket event bus for discovery updates, motion events, storage updates, and recorder health transitions. Snapshot payload includes `cameras`, `scan_status`, `recent_motion_events`, `go2rtc_base_url`, `story_enabled`, and hardware capability fields

---

## Data flow — a frame's life

```
1. Camera sends RTSP stream
         │
         ▼
2. go2rtc receives the upstream RTSP connection(s).
   Each camera has up to TWO go2rtc stream registrations:
     - <uuid>       → main RTSP URL (always)
     - <uuid>_sub   → sub-stream RTSP URL (when the
                      camera exposes one; Reolink,
                      TP-Link, most modern brands do)
   go2rtc is a lazy producer — registration costs nothing
   until a consumer attaches, so dual registration is
   free at rest. The onboarding compare UI uses this to
   render main + sub side-by-side without any backend
   coordination (see "Sub-stream recording for retention"
   under Key Design Decisions).
         │
         ├──► Tee A → ffmpeg recorder
         │   ffmpeg reads rtsp://127.0.0.1:58554/<uuid>
         │   OR rtsp://127.0.0.1:58554/<uuid>_sub depending
         │   on the record_substream_when_available setting
         │   (loopback, zero bandwidth cost)
         │     │
         │     ├──► Output 1: -c copy -f segment
         │     │    Stream-copy raw H.264 to .mp4 segments
         │     │    (zero CPU, zero re-encoding, zero MPEG-LA
         │     │    liability)
         │     │
         │     └──► Output 2: select(scene>0.04 OR fps_floor),
         │          scale=320, image2pipe mjpeg pipe:1
         │          JPEG frames on scene-change OR every 30 input
         │          frames (1fps floor for quiet indoor scenes)
         │          → frame_broadcaster → MotionDetector
         │            → MOG2 background subtraction
         │            → IOU multi-object tracker (per-blob identity)
         │            → promoted track closes after 2s idle
         │            → YOLOX classifier (full-res crop from recording)
         │              → person/vehicle/animal/null
         │              → multi-track collapse → motion_events.object_class
         │            → Today view: person cards + per-camera counts
         │
         ├──► Tee B (if camera has audio) → ffmpeg audio extractor
         │   Separate lightweight ffmpeg reads same go2rtc loopback
         │   -map 0:a → PCM s16le 16kHz mono → pipe:1
         │     → AudioBroadcaster (0.96s windows, 0.48s hop)
         │       → YAMNet classifier (~10ms/window on CPU)
         │         → high-priority (glass_break/gunshot/scream/siren)
         │           → independent motion_events row, source='audio'
         │         → low-priority (bark/car_horn/door_slam/etc)
         │           → enriches recent vision event with sound_class
         │
         └──► Tee C → go2rtc native WebRTC/MSE pipeline
             go2rtc decodes once and serves browser clients via its
             own video-rtc.js web component (vendored at
             frontend/src/vendor/go2rtc/). The frontend reaches it
             through a same-origin proxy path: in dev mode, Vite's
             /g2r rule at frontend/vite.config.ts forwards to
             127.0.0.1:58581 with an Origin header rewrite that
             bypasses go2rtc's strict Cross-Site WebSocket check.
             Tauri production needs an equivalent server-side
             proxy (tracked as a follow-up).
             → <video-stream> custom element → MSE → <video> tile
```

The two-output unified ffmpeg command pays the H.264 decode cost
once for motion detection, while go2rtc's separate decoder serves
the browser preview path. We could in principle wire the frontend
preview to go through ffmpeg instead, but go2rtc's WebRTC pipeline
is purpose-built for browser delivery (proper SPS/PPS in-band,
codec negotiation, MSE init segments, reconnect handling) and it
already exists in the stack as the RTSP fan-out service.

---

## Key design decisions

### One RTSP per camera (per stream), via go2rtc

Cheap IP cameras (Eufy, no-name Tapos) enforce strict concurrent-client limits — often just 1 or 2. If recording, motion, and preview each open their own RTSP connection, the camera flaps. We let go2rtc hold a single RTSP connection to each camera's main stream and fan out internally. Every downstream consumer reads from `rtsp://127.0.0.1:58554/<uuid>` instead. When the camera also exposes a sub-stream, go2rtc holds a second independent connection to the sub-stream RTSP endpoint (which is a distinct URL path on the same camera server — `/h264Preview_01_sub` on Reolink, `/stream2` on TP-Link, etc.) and fans it out as `rtsp://127.0.0.1:58554/<uuid>_sub`. That's still "one RTSP session per stream per camera" — the camera's concurrent-client limit applies per-endpoint, not per-device, and both Reolink and TP-Link are explicitly designed to serve main+sub simultaneously. Cheaper cameras that can't handle two sessions gracefully degrade: sub-stream registration fails, the recorder logs once and falls back to main, and the onboarding compare UI reports "no storage-friendly stream available" for that camera.

### Live preview path: go2rtc WebRTC, never our own muxing

Browser live preview is delivered by go2rtc's own native WebRTC/MSE pipeline via its `<video-stream>` web component (vendored at `frontend/src/vendor/go2rtc/`, MIT-licensed copy of go2rtc v1.9.14's `video-rtc.js` + `video-stream.js`). We do NOT roll our own MJPEG fan-out, HLS muxer, or fragmented MP4 streamer — every prior attempt this session ran into a different fundamental issue:

- **MJPEG over multipart/x-mixed-replace**: fragile browser parsers, random tile stalls with no recovery
- **Fragmented MP4 via `<video src>`**: go2rtc's `/api/stream.mp4` advertises a finite 3-second duration in the moov atom; browsers play to "end" and stop
- **HLS via `<video>` + hls.js**: go2rtc's HLS muxer produces TS segments without inline SPS/PPS NALs for many camera streams (verified via ffprobe — `non-existing PPS 0 referenced, decode_slice_header error`); the decoder can't initialize and freezes after the first frame

go2rtc's WebRTC path correctly handles all of this because it's go2rtc's primary use case — proper SPS/PPS handling, MSE init segments, codec negotiation, reconnect-on-network-hiccup, browser autoplay policy interactions. Recording continues to use ffmpeg-via-go2rtc's-RTSP-loopback because that path injects parameter sets correctly (which is why recording always worked, even when the HLS muxer was producing garbage segments).

The frontend reaches go2rtc through a same-origin proxy path (`/g2r`) so we can keep go2rtc's strict Cross-Site-WebSocket-Hijacking origin check enabled. In dev mode the proxy lives in `frontend/vite.config.ts` and rewrites the `Origin` header from `http://localhost:3000` to `http://127.0.0.1:58581` so go2rtc accepts the request as same-origin. In production (Tauri bundled mode), an equivalent server-side proxy is required and tracked as a follow-up.

### Dev-mode go2rtc auto-spawn

In Tauri/bundled mode, the Rust shell spawns go2rtc as a sidecar before Python and hands the URLs over via `SIMPLENVR_GO2RTC_URL` / `SIMPLENVR_GO2RTC_RTSP_URL` env vars. In bare-python dev mode (`python -m backend.main` from a terminal), no Tauri shell exists and those env vars are unset. The Python backend's `dev_go2rtc.py` module closes the gap: on startup, if `SIMPLENVR_DEV=1` and `SIMPLENVR_GO2RTC_URL` is unset, it locates a go2rtc binary in `src-tauri/target/debug` or `src-tauri/binaries/`, writes the matching YAML config, spawns go2rtc as a subprocess, waits for `/api/streams` readiness, and sets the env vars in-process so the rest of the backend code is identical to Tauri mode. Cleanup is handled by `atexit` plus the existing `kill_orphan_go2rtc` startup sweep.

### Stream-copy recording, no transcoding

Raw H.264 from the camera is copied byte-for-byte into segment files via `ffmpeg -c copy`. No re-encoding means:
- Zero CPU cost for recording
- No MPEG-LA patent liability (we are not an "encoder" or "decoder" in the patent sense, we are a storage service for someone else's already-encoded stream)
- Maximum quality (lossless, since we're not decoding-and-re-encoding)
- Smaller binary footprint (no libx264 in the ffmpeg build)

### Sub-stream recording for retention

The camera's own onboard ASIC already produces a second H.264 bitstream — the sub-stream — encoded directly from raw sensor data at a much lower bitrate (~260 kbps vs ~6 Mbps on Reolink; ~90 kbps vs ~4 Mbps on TP-Link). That's **not** a downsampled version of the main stream — it's an independent encode of the same sensor pixels. Recording it instead of the main stream:

- **Costs nothing extra**. Still stream-copy, still zero CPU, still zero patent liability.
- **Has no generation loss.** The sub-stream is the camera's first encode, not a decode-and-re-encode of our recording. That's what makes it fundamentally different from the broken "Video quality" transcode path in `codec.py` that burns CPU to produce a worse image.
- **Multiplies retention by roughly 20×–30×.** Measured on representative cameras in April 2026: Reolink main 6.29 Mbps → sub 262 kbps (24×); TP-Link main → sub ~45×. A 48-hour disk budget becomes a ~month-or-more budget on the exact same disk.
- **Drops archive resolution.** Reolink sub is 640×480, TP-Link sub is 640×360. That's a real product-level tradeoff: faraway license plates and faces stop being legible in the archive. Fine for "a homeowner keeping an eye on the yard," wrong for "farm supply cash register" — so the choice belongs to the user, not to the defaults.

**Gated behind `Settings.record_substream_when_available`, default `False`.** The default records the camera's full-resolution main stream, preserving the "max quality" promise. Flipping the setting on (from the API or future onboarding compare UI) causes every recorder to restart with the sub-stream loopback as its input. Cameras without a sub-stream (`substream_uri is None`) silently fall back to main regardless of the setting — toggling is always safe, the worst case for a sub-streamless camera is "same as today."

**Separation of registration from consumption.** go2rtc is a lazy producer, so we always register both streams when the camera exposes a sub, regardless of the user's setting. The setting only decides which loopback URL `camera_recorder.py` passes to ffmpeg as its `-i` argument. That separation is what enables the onboarding compare UI: the frontend can instantiate two `<video-stream>` custom elements pointed at `<uuid>` and `<uuid>_sub` simultaneously — without any backend coordination, without any mid-flow go2rtc reconfiguration, without any risk of the recorder losing its session. Live main vs sub on the same screen for ~30 seconds while the user picks; then the sub consumer disconnects and go2rtc's idle sub-stream quietly stops reading from the camera.

**YOLOX classification consequence (known, unhandled).** The classifier currently reads frames out of the recorded .mp4 after the fact. When sub-stream recording is on, those frames are 640×480 instead of 2560×1920, which hurts detection accuracy at distance (a person crossing the far end of the carport at 40 feet becomes ~30 pixels tall). The correct fix is teaching classification to read frames from the live go2rtc **main** stream regardless of what's being archived — the main stream is always registered, always available, and free to sample. Tracked as a follow-up; not blocking the sub-stream recording feature.

**TODO — surface the sub-stream choice in the UI.** The feature is fully wired backend-side: `Settings.record_substream_when_available` in `backend/models.py`, the POST `/api/settings` handler in `backend/api/settings.py`, the recorder's loopback-URL selection in `backend/recording/camera_recorder.py`, and `frontend/src/types.ts` already carries the field on the Settings type. What's missing is the **user-facing control** — today the only way to flip it is by crafting a raw POST to the settings endpoint, which violates "zero knobs, zero jargon" from `.impeccable.md` because the benefit (20–30× retention) is invisible to anyone who doesn't read the code. The plausible surfaces, in order of preference:

1. **Onboarding compare UI** — during first-run camera setup, show main and sub side-by-side for ~30s (both are already registered in go2rtc by this point — see "Separation of registration from consumption" above) and let the user pick. Frame it as *"Record in high quality (≈2 days of footage) or storage-friendly (≈50 days)?"* — outcome words, not bitrate numbers. Default highlight on high-quality so the user can click through without changing anything.
2. **Settings screen toggle** — the existing Settings modal gains one row: *"Record in storage-friendly mode"* with a one-line explanation of the tradeoff ("Lower-resolution archive, about 25× more days of history, no CPU cost"). Same outcome-focused copy. This is the fallback for users who skip onboarding or change their mind later.

Whichever surface lands first, the copy must never expose "sub-stream," "bitrate," "resolution," or "codec." The product test is "can a non-technical user flip this" — see `docs/product.md`. Both surfaces must handle `substream_uri is None` gracefully (camera has no sub-stream; skip it in the onboarding compare, or show the Settings toggle as disabled with *"This camera only supports high-quality recording"*). The recorder already no-ops correctly in that case, so the UI just needs to not lie about the option being available.

### tether — cross-platform parent-death supervisor

See `src-tauri/tether/src/main.rs`. A tiny supervisor binary that encapsulates per-OS process-lifecycle primitives behind a uniform interface. Linux uses `PR_SET_PDEATHSIG`, Windows uses Job Objects with `KILL_ON_JOB_CLOSE`, macOS uses a stdin-EOF watchdog thread (the only cross-platform primitive the macOS kernel exposes). Application code in Tauri and Python has zero per-OS branching around process lifecycle — they just spawn through tether and the right mechanism kicks in.

### Python for orchestration, not Rust

The orchestration layer (ONVIF, FastAPI, SQLite async, process supervision of ffmpegs) is written in Python. This is a deliberate trade-off:

- **Pro**: Python has the only mature ONVIF library ecosystem (`zeep`, `onvif-zeep`, `wsdiscovery`). Rewriting ONVIF parsing in Rust would be months of work with no user-visible benefit.
- **Pro**: FastAPI is genuinely pleasant for a small HTTP API. aiohttp-in-Rust equivalents exist but are more ceremony.
- **Pro**: Hot-reload during dev is faster with Python.
- **Con**: PyInstaller onedir bundle is ~100+ MB on disk. Accepted as a trade-off (onefile mode was smaller but added 2-10s extraction on every launch).
- **Con**: Onedir bundle is ~100+ MB vs ~5 MB for a Rust equivalent. Accepted — installed size is cheap; startup speed matters more.

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

FFmpeg, ffprobe, go2rtc, and tether are all bundled inside the app package. We do not depend on the user having ffmpeg installed. This is non-negotiable for the "it just works" promise — asking a non-technical user to `brew install ffmpeg` is a showstopper.

### Security model

SimpleNVR is a local-first app. The Python sidecar binds to `127.0.0.1` only and is never exposed to the public internet by design. The trust boundary is "whoever can read this user's application-data directory already owns the machine" — the same model any local-first desktop app relies on. We do **not** encrypt credentials at rest; that would add a key-management burden for a threat we don't defend against, and the real integration-security work (CORS, input validation, path containment) is what actually matters for a loopback HTTP service that a malicious web page in the user's own browser can reach.

**Credential handling — single source of truth.** The `cameras.rtsp_uri` and `cameras.substream_uri` columns store the URL *without* embedded `user:pass@` userinfo. Username and password live only in their own columns. At every use site that actually opens an RTSP connection — the recorder, go2rtc registration, URI verification probes — the authenticated URL is rebuilt on demand by `backend/rtsp_url.py::with_creds`. This keeps the secret in one place on disk, and — critically — means the credential-free URL is what flows through API responses and WebSocket events, while `Camera.password` is declared `Field(exclude=True)` in the Pydantic model so the secret is structurally unable to be serialized out through FastAPI or the event bus.

**Gotcha this creates, documented here so future-you doesn't re-introduce the bug:** because the event bus fans out the same payload to the WebSocket layer and to internal subscribers like the RecordingManager, subscribers must treat the event as a *notification* and re-fetch the camera from the DB by id (`db.get_camera`) rather than reconstructing it from the event payload with `Camera(**cam_data)` — the latter would see `password=None` (because of `Field(exclude=True)`) and hand FFmpeg a credential-free URL, which flaps in a restart loop. See `backend/recording/manager.py::_handle_event`.

**`authed_uri()` output is a raw camera URL, NOT a go2rtc source URL.** This is a load-bearing distinction that has bitten us once and will bite again if anyone assumes the two can be unified. `backend/rtsp_url.py::authed_uri` and `authed_substream_uri` return a clean authenticated RTSP URL with nothing appended — no query parameters, no transport hints, just `rtsp://user:pass@host:port/path`. Every caller that hands a URL to **ffprobe** or **ffmpeg** — directly or through the scanner's URI staleness probe — depends on that cleanliness, because libavformat does **not** strip query parameters from RTSP URLs. It forwards them verbatim in the `DESCRIBE` request line, and cameras that exact-path-match (Reolink and many generic IPC firmwares) return 404 on any path they don't recognize character-for-character. So a URL like `rtsp://.../h264Preview_01_main?rtsp_transport=tcp` gets DESCRIBEd as that full string, and the camera responds 404 to a path it's never served.

A previous attempt to force TCP transport for a specific camera family wrapped `authed_uri()` with a helper that appended `?rtsp_transport=tcp`. The helper was intended to affect only the go2rtc registration path, but `authed_uri()` is also called by the periodic `verify_rtsp_uri` probe in `backend/discovery/scanner.py::_probe_due_uris`, which runs every `URI_PROBE_INTERVAL` (5 minutes). Within one interval every Reolink flipped to `needs_auth` because libavformat DESCRIBEd the full query-containing URL and the camera 404'd on a path it had never served. The change was reverted.

Any future attempt at forcing a specific RTSP transport for a camera family must keep `?rtsp_transport=tcp` **out** of `authed_uri()`'s return value. The only paths allowed to append it are go2rtc sources (where it's a recognized go2rtc source option, parsed and stripped before the upstream DESCRIBE). ffmpeg's CLI already gets `-rtsp_transport tcp` as a flag in `backend/recording/codec.py::build_unified_cmd`, so the recorder's direct-camera fallback path does not need URL-level TCP forcing either. If a future fix adds a `go2rtc_source_url(camera)` helper that applies the query param, it must be a separate function — not a layer on top of `authed_uri` — and any test for it must assert that `authed_uri` output has an empty query string.

**CORS.** The FastAPI middleware uses an explicit origin allowlist: `tauri://localhost` and `https://tauri.localhost` for production Tauri builds, plus `http://localhost:{1420,5173}` when `SIMPLENVR_DEV=1` is set. Wildcard (`*`) would let any website the user visits in their browser make cross-origin requests against the loopback API — a "local bind" alone is not a trust boundary against same-host code.

**Input validation that matters at a loopback boundary.**
- `Settings.recording_fps` is a `Literal[...]` — it's interpolated into FFmpeg's `-vf` filter graph, and a crafted plain-string value could inject additional filter stages. Literal-at-the-model-layer enforces the allowlist on every boundary in one place.
- `_validate_recordings_path` in `backend/api/settings.py` resolves the user-supplied path *before* `mkdir`, so an invalid path is a pure validation error with no filesystem side effects.
- The recording and motion file-serve endpoints call `path.resolve().relative_to(active_directory)` to keep the `file_path`/`thumbnail_path` columns from ever becoming an arbitrary-file-read primitive if the DB is tampered with.
- `backend/db.py::_migrate_add_column` validates the `table`, `column`, and `decl` arguments against strict identifier regexes before interpolating them into DDL. SQLite has no parameterized DDL, so this is a latent-footgun guard for future migrations.

**What we do not defend against**: (a) an attacker with read access to the user's application-data directory — they already own the credentials; (b) a user willingly running SimpleNVR behind a reverse proxy exposed to the public internet — the readme, if we ever write one, will say "don't do that"; (c) supply-chain compromise of bundled binaries (ffmpeg, go2rtc) — covered by code signing, not by runtime checks.

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
  recording/              Per-camera recording + storage management + audio extraction
  audio/                  YAMNet audio classification + event firing
  motion/                 Motion detection (MOG2 + IOU tracker)
  classification/         YOLOX object classification + capability probe
    models/               ONNX weights (bundled into the PyInstaller
                          sidecar at build time by backend/main.spec;
                          gitignored in source, fetched by
                          scripts/fetch_yolox.sh before bundling)
  story/                  Template-based story compiler
  api/                    FastAPI routes + WebSocket event bus
  main.spec               PyInstaller spec (onedir mode)

src-tauri/                Tauri Rust shell + supervisor
  Cargo.toml              Workspace root
  src/lib.rs              Sidecar spawning, lifecycle, crash dialogs
  build.rs                Forwards TARGET_TRIPLE to rustc env
  tauri.conf.json         Tauri config (externalBin, resources, bundle settings)
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
  bundle_python.sh        PyInstaller onedir build
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
scripts/bundle_python.sh    # PyInstaller onedir → simplenvr-backend-dir/
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
