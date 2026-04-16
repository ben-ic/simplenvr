# SimpleNVR — Architecture

A technical overview of how SimpleNVR is put together. For the *why* behind the product decisions, see `product.md`. This document describes the *how*.

---

## Overview

SimpleNVR is a Tauri desktop application with a Python backend sidecar, a Go (go2rtc) RTSP fan-out service, and a native video rendering layer powered by libmpv. It bundles everything it needs into a single installable `.app` / `.msi` / `.dmg` and runs entirely on the user's machine with no external services.

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
│  │ - Native video plugin (tauri-plugin-rtsp-mosaic + libmpv)     │   │
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
│  └──────────┬──────────┘        │ │ Motion detector           │ │    │
│             │                   │ │ Storage janitor           │ │    │
│             │                   │ │ SQLite state              │ │    │
│             │ one RTSP per      │ └────────────┬──────────────┘ │    │
│             │ camera             │              │                │    │
│             │                   │              ▼                │    │
│             │                   │  ┌─────────────────────────┐  │    │
│             │                   │  │ tether → ffmpeg × N      │  │    │
│             │                   │  │  per camera:             │  │    │
│             │                   │  │   • recorder (stream-copy│  │    │
│             │                   │  │     → .mp4 segments)     │  │    │
│             │                   │  │   • detect (640×auto@2fps│  │    │
│             │                   │  │     → RGB24 for motion + │  │    │
│             │                   │  │     D-FINE inline)       │  │    │
│             │                   │  └─────────────────────────┘  │    │
│             │                   └───────────────────────────────┘    │
│             │                                                         │
│  ┌──────────┴─────────┐     ┌─────────────────────────────��───────┐  │
│  │ Real IP cameras    │     │ libmpv (in-process, LGPL 2.1+)      │  │
│  │ on the LAN         │     │ Reads RTSP from go2rtc loopback    │  │
│  └────────────────────┘     │ Renders into native OS surfaces    │  │
│  ↑ exactly ONE RTSP         │ (NSView/HWND) below the webview    │  │
│    connection per camera    └─────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
```

---

## The process tree

Every parent-child relationship uses the `tether` supervisor for cross-platform parent-death cleanup. See `src-tauri/tether/src/main.rs` for the mechanism (`PR_SET_PDEATHSIG` on Linux, Job Objects on Windows, stdin-EOF watchdog on macOS).

```
Tauri shell
├── tether → go2rtc
├── libmpv (in-process, one instance per live camera tile)
└── tether → simplenvr-backend (Python)
    ├── tether → ffmpeg recorder (camera 1, stream-copy to segments)
    ├── tether → ffmpeg detect   (camera 1, 640×auto@2fps RGB24)
    ├── tether → ffmpeg audio    (camera 1, if audio track exists)
    ├── tether → ffmpeg recorder (camera 2) + detect + audio
    └── …one recorder + one detect + (optional) one audio per camera
```

Each camera gets up to three lightweight ffmpeg processes, all reading
from the go2rtc loopback and fanned out per-role so a failure in one
(detect crash, audio stall) can't kill recording. Motion/detection
runs inline in Python against the detect-role stream; see the
**Motion** subsystem below.

libmpv is not a child process — it runs in-process inside the Tauri shell via `tauri-plugin-rtsp-mosaic`. Each live camera tile creates one mpv render context that reads from go2rtc's RTSP loopback (`rtsp://127.0.0.1:58554/<uuid>`) and renders into a native OS surface positioned below the webview. The plugin manages tile creation, layout, and teardown through Tauri IPC commands exposed to the frontend as a `<rtsp-tile>` custom element.

### Lifecycle invariants

1. **Exactly one instance runs at a time.** `tauri-plugin-single-instance` uses a Unix socket (macOS/Linux) or named mutex (Windows). A second launch focuses the existing window.
2. **When the parent dies, all descendants die.** Every edge in the tree is tethered. No process scanning, no PID files, no cleanup heuristics.
3. **Graceful shutdown preserves recording integrity.** Tauri SIGTERMs Python through tether → Python runs its FastAPI lifespan shutdown → terminates each ffmpeg cleanly so the last segment's moov atom is written.
4. **Crashes recover without user intervention.** If an individual ffmpeg dies, Python restarts it with backoff. If Python dies, Tauri shows a crash dialog (rare — should not normally happen).
5. **Subsystem startup order is fixed: RecordingManager first, MotionManager second.** `RecordingManager` spawns recorders the moment cameras are authed so segments land on disk as early as possible; `MotionManager` comes up later because D-FINE model load and the per-camera detect-ffmpeg spawn are both slower. The consequence is that `CameraRecorder` can't receive `MotionManager` through its constructor — instead, `main.py` calls `recording_manager.attach_motion_manager(motion_manager)` once MotionManager is ready, which walks every live recorder and wires the split-brain witness in place. The scanner's `camera_added` handler also wires any new recorder that comes up after startup. The split-brain check degrades gracefully (does nothing) while the witness is absent, so the attach can race without breaking anything.

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

- **`manager.py`** — `RecordingManager` orchestrates per-camera recorders, reacts to camera state changes on the event bus, runs the storage janitor. `attach_motion_manager()` wires every live and future recorder to the shared `MotionManager` post-init — the split-brain watchdog needs a detect-ffmpeg witness, and MotionManager is constructed *after* RecordingManager in `main.py`'s startup sequence, so constructor injection isn't possible. See "Recording reliability" under Key Design Decisions.
- **`camera_recorder.py`** — per-camera `CameraRecorder`. Spawns ffmpeg via tether, monitors segment file growth for liveness (stderr is diagnostic only, not a heartbeat), restarts on failure with backoff, handles segment completion events. At spawn time, registers both the main and (if available) sub-stream with go2rtc and picks which loopback URL to hand ffmpeg based on the per-camera `recording_stream_override` (written by the chronic-failure breaker) taking precedence over the global `record_substream_when_available` setting. See "Sub-stream recording for retention" and "Recording reliability" under Key Design Decisions. Three pure module-level predicates at the top of the file encode the reliability-watchdog decisions:
  - `should_trip_split_brain(bytes_elapsed_s, recorder_uptime_s, detect_elapsed_s)` — fires when the record-ffmpeg has been bytes-stalled for >15 s *and* detect-ffmpeg decoded a frame in the last 10 s *and* the recorder has been up longer than the 15 s startup grace. Unambiguous Zone-B: go2rtc is healthy, only the recorder's muxer is stuck. 15 s ahead of the 30 s file-growth kill threshold.
  - `should_label_record_failing(new_health, detect_elapsed_s)` — rewrites the file-growth ladder's `"offline"` label to `"record_failing"` when detect-ffmpeg has decoded a frame in the last 10 s. Without a witness the conservative `"offline"` label is kept. Prevents the UX lie of an OFFLINE badge over a tile that is clearly rendering live video.
  - `prune_and_check_breaker_trip(restarts, now, window_s, threshold)` — generic sliding-window deque helper. `CameraRecorder` calls it with `CIRCUIT_BREAKER_WINDOW_S=600` / `CIRCUIT_BREAKER_THRESHOLD=3`; the breaker counts *both* split-brain kills and plain-stall kills and *both* fast-fail exits (`rc != 0` inside `_FAST_FAIL_THRESHOLD_S=5`) since all three collapse to the same user-facing remedy (drop to sub-stream).

  Covered by `tests/test_split_brain_rule.py`, `tests/test_record_failing_rule.py`, `tests/test_circuit_breaker.py` — decision-boundary tests against the pure predicates, no CameraRecorder instance required.
- **`codec.py`** — builds the unified ffmpeg command line. One ffmpeg instance, two outputs: (1) stream-copy to disk segments, (2) scene-filtered JPEG frames at ~1 fps that feed the recorder's `motion_broadcaster`. In v2 those JPEGs no longer drive detection — the AudioManager uses them for event thumbnails and `/api/cameras/{id}/snapshot` serves the `.latest` cache for live-view previews. See Motion + Detect-frames subsystems for the v2 detection path. Live preview is NOT an ffmpeg output — it's rendered natively by libmpv reading from go2rtc's RTSP loopback. See "Live preview path" below.
- **`frame_broadcaster.py`** — per-camera fan-out for the recorder's scene-filtered JPEG stream with a latest-frame cache. Bounded async queue per subscriber, drop-oldest semantics. Has a `close()` method that puts a `None` sentinel into every subscriber queue so consumers exit cleanly when the recorder is stopped.
- **`storage.py`** — retention math: `retention_days = budget / aggregate_bitrate`, where `budget` is the user's configured storage limit (not free disk space) and the aggregate bitrate denominator is `total_segment_bytes / wall_clock_seconds` over the last 20 segments. The wall-clock denominator (`max(ended_at) − min(started_at)`) is load-bearing: summing per-segment durations across N parallel recorders cancels the N out and reports the per-camera rate, which overstates retention by ~N× on a multi-camera install. Sub-stream recording still multiplies retention by ~20–30× because the per-camera bitrate drops that much; the wall-clock fix is orthogonal and corrects a separate bug. A minimum 60 s wall-clock window is required before the first estimate publishes, so the banner signals that the first value lands in about two minutes.
- **`janitor.py`** — periodic cleanup: delete expired segments, prune orphan files not in the DB, enforce storage budget.

**Settings propagation invariant (load-bearing, bit us once).** `CameraRecorder.__init__` captures `self._settings = settings` as a *reference*, not a snapshot. `RecordingManager.load_settings()` creates a **new** `Settings` instance and assigns it to `manager._settings` — it does not mutate in place. That means every existing `CameraRecorder` is pinned to the `Settings` instance that was current at its spawn moment, and the only way to propagate a settings change into live recorders is to **stop + restart them**. Consequently: any code path that changes settings must ensure `RecordingManager.apply_settings_change()` actually fires a restart, and the diff logic there must diff against a caller-supplied snapshot of the pre-change state. Before the POST `/api/settings` endpoint called `recorder.load_settings()` inline for response-consistency reasons, capturing "old" state *after* that reload silently produced `new vs new` comparisons and never triggered restarts — so the recorder flip was accidentally happening via the scanner's periodic `camera_updated` event several minutes later instead of via the intended apply-settings path. The current code snapshots `recorder.settings.model_copy()` + `recorder.recordings_dir` in `api/settings.py` **before** touching anything, then passes both into `apply_settings_change(old_settings, old_recordings_dir)` as explicit parameters. Future changes to settings handling must preserve that ordering or pre-existing toggles (fps, segment duration, path, enabled, sub-stream) stop taking effect.

### Motion — `backend/motion/`

Detection Pipeline v2 (built April 2026). Each camera gets its own
detect-role ffmpeg (640×auto@2fps RGB24 off the go2rtc loopback) and a
Python consumer that runs motion gating, object detection, multi-object
tracking, and an 8-layer false-positive filter in a single per-camera
asyncio task, with one D-FINE ORT session shared across every camera
in the process.

- **`manager.py`** — `MotionManager`. Owns the shared `DFineDetector`
  and a sync `sqlite3.Connection` for per-camera heatmap persistence.
  Subscribes to `recording_started` / `recording_stopped` /
  `camera_lost`, attaches a `MotionDetector` per active camera.
  Exposes `boost_detection(camera_id)` for the audio pipeline to
  trigger the 5 s high-alert window (see Audio subsystem), and
  `get_last_decoded_frame_dt(camera_id)` for the recorder's
  split-brain watchdog to read the detect-ffmpeg freshness witness
  (see "Recording reliability" under Key Design Decisions).
  `SIMPLENVR_CLASSIFIER=off` is a full kill switch — no D-FINE load,
  no detectors attached; the split-brain witness returns `None` and
  the file-growth watchdog still runs unchanged.
- **`detector.py`** — `MotionDetector`. Per-camera pipeline: newest RGB
  frame → grayscale running-average background subtraction with scene-
  change guard (3 σ over EWMA of global `|frame − B|` freezes `B` on
  IR-cut / auto-exposure spikes) → proposals via
  `scipy.ndimage.label` → motion-gated D-FINE inference (skipped on
  empty proposals) → ByteTrack → 8-layer FP filter → event
  lifecycle. The 8 layers:
  1. Geometric (min dim / aspect / max frame fraction)
  2. Min score (0.3)
  3. Init delay (`track.age >= 3`)
  4. Bayesian Beta confidence (→ `confidence.py`)
  5. Recoverable-doubt state machine (→ `confidence.py`; 0.70/0.35/0.50)
  6. Movement gate (drops stationary non-person; 10 s standing-person
     carveout)
  7. Stationary NCC — vehicle-only, 128×96 grayscale template match
     against learned parking spots, LRU-capped at 256/camera
  8. False-alarm heatmap weighting (→ `heatmap.py`)
- **`tracker.py`** — `ByteTracker`. Clean-room ByteTrack in ~500 lines
  (numpy + scipy only). 8-D constant-velocity Kalman filter
  (`scipy.linalg.cho_factor` / `cho_solve`), two-pass Hungarian
  association via `scipy.optimize.linear_sum_assignment`, single-class
  pool + additive class-mismatch cost penalty (+0.1). `TRACK_BUFFER=6`
  tuned for 2 fps detector cadence. 4 unit tests.
- **`confidence.py`** — `TrackConfidence`. Beta(α,β) posterior with
  exponential forgetting (α=0.98) and a three-state machine
  (`pending` → `confirmed` → `doubt` ↔ `confirmed`). Exposes
  `p_hat`, `p_hat_lo = p_hat − σ`, and `emittable`. 5 unit tests.
- **`heatmap.py`** — `HeatmapLayer`. Per-camera 16×12 = 192 Beta
  cells, persisted to SQLite table `detection_heatmap` (module-owned
  `CREATE TABLE IF NOT EXISTS`; `db.py` untouched). FP observation →
  β++ on dominant cell; TP → α++. Score multiplier `α/(α+β)` applied
  to `p_hat` when `α+β > 20`, else `1.0`. 5 unit tests.
- **`clip.py`** — unchanged. Spawned identically at event close to
  stitch per-event MP4 clips from the recorder's segments.

The v1 MOG2 + IOU-tracker + `ClassificationManager` + post-hoc YOLOX
pipeline was ripped during the rewrite; see git history on the
`detection-v2` branch.

### Detect-frames — `backend/detect_frames/`

Per-camera detect-role ffmpeg supervisor, sibling to `CameraRecorder`.

- **`ffmpeg_source.py`** — `DetectFfmpegSource`. One long-lived ffmpeg
  per camera reading `rtsp://127.0.0.1:58554/<camera_id>` at
  `-vf scale=640:-2,fps=<rate> -f rawvideo -pix_fmt rgb24 pipe:1`.
  Probes source dims once via ffprobe, computes output height as
  `round(src_h × 640 / src_w / 2) × 2`. Exponential-backoff restarts,
  stderr tail ring, clean SIGTERM/SIGKILL path. `set_fps(new_fps)`
  implemented as a process restart — the audio-boost window uses this
  to bump detection from 2 fps to 5 fps for 5 s.
- **`shm_ring.py`** — `NewestFrameSlot`. Single-slot drop-oldest async
  queue on top of `asyncio.Queue(maxsize=1)`. Filename is a leftover
  from the plan doc; no `multiprocessing.shared_memory` is used (same
  process, asyncio primitive is sufficient).

### Classification — `backend/classification/`

D-FINE-N object detection. One shared ORT session across the whole
backend process.

- **`dfine.py`** — `DFineDetector`. Loads `dfine_n.onnx` (Apache 2.0,
  15.3 MB, committed in-tree), runs letterboxed 640×640 RGB/255 NCHW
  inference with the two-input schema (`images` +
  `orig_target_sizes=[[640,640]]`). NMS-free — the decoder is in the
  graph. Returns `Detection(class_id, class_name, x1/y1/x2/y2, score)`
  in original-frame pixel coords. Single-threaded CPU EP v1;
  Snapdragon QNN EP + INT8 on ARM64 Windows is a follow-up.
- **`capability_probe.py`** — cross-platform hardware telemetry
  (OS / arch / RAM / CPU count / free disk / D-FINE warm-median
  latency). Single tier in v1 — everything runs CPU EP; the probe is
  now a recorder, not a gate. Cached via the fingerprint pattern so
  warm boots skip re-calibration. Kill switch:
  `SIMPLENVR_CLASSIFIER=off`.
- **`labelmap.py`** — COCO-80 → {person, vehicle, animal, None}
  collapse. Unchanged by v2 — the three-label product taxonomy is the
  seam between COCO's 80 classes and the UI's "Person at …" /
  "Vehicle at …" / "Animal at …" strings. `None` silently drops the
  track, which also acts as a free FP filter for surveillance-
  irrelevant classes (bicycle, motorcycle, airplane, train, boat,
  clocks, traffic lights).
- **`models/`** — `dfine_n.onnx` (15.3 MB) + `yamnet.onnx` (15 MB) +
  `yamnet_classes.txt` + `NOTICE.txt`, **all committed in-tree** so
  the build pipeline has zero network dependency.
  `scripts/fetch_dfine.{sh,ps1}` verifies the pinned SHA256 and
  refreshes NOTICE; it does not download.

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

- **`cameras.py`** — CRUD + auth + camera-delete endpoints, plus `POST /cameras/{id}/retry-main-stream` which clears `recording_stream_override` + `fallback_reason` and bounces the recorder (used by the "Try higher quality" button after a chronic-failure breaker trip — see "Recording reliability" under Key Design Decisions)
- **`streams.py`** — empty placeholder (live preview is rendered natively by libmpv via go2rtc's RTSP loopback)
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
- **`ws.py`** — WebSocket event bus for discovery updates, motion events, storage updates, and recorder health transitions. Snapshot payload includes `cameras`, `scan_status`, `recent_motion_events`, `go2rtc_base_url`, `story_enabled`, and hardware capability fields. `camera_health` events carry two distinct payload shapes the frontend must handle via a discriminated switch on `health`: normal transitions (`ok`/`stalled`/`offline`/`record_failing`) use `{camera_id, health, last_frame_at}`; chronic-failure transitions (`chronic_recording_failure`) use `{camera_id, health, reason}` with no `last_frame_at`. See "Recording reliability" under Key Design Decisions for why the asymmetry is load-bearing.

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
         │          → frame_broadcaster → motion_broadcaster.latest
         │          (used by AudioManager for event thumbnails and
         │           /api/cameras/{id}/snapshot for live-view previews;
         │           NOT the detection pipeline — that has its own
         │           ffmpeg, see Tee B)
         │
         ├──► Tee B → ffmpeg detect (Detection Pipeline v2)
         │   Separate low-rate ffmpeg reads the same go2rtc loopback
         │   -vf scale=640:-2,fps=2 -f rawvideo -pix_fmt rgb24 pipe:1
         │     → NewestFrameSlot (single-slot drop-oldest queue)
         │       → MotionDetector
         │         → running-average background + scene-change guard
         │         → proposals via scipy.ndimage.label
         │         → motion-gated D-FINE-N inference (~40-65 ms CPU)
         │           → ByteTrack (Kalman + 2-pass Hungarian)
         │             → 8-layer FP filter (geometric → min-score →
         │                init-delay → Bayesian Beta → doubt state →
         │                movement gate → stationary NCC →
         │                false-alarm heatmap)
         │               → emittable track → motion_events row +
         │                 boxed thumbnail + WS motion_started/updated
         │               → event close (MOTION_DEBOUNCE_SECONDS idle) →
         │                 motion_ended + spawn create_motion_clip
         │         → Today view: person cards + per-camera counts
         │
         ├──► Tee C (if camera has audio) → ffmpeg audio extractor
         │   Separate lightweight ffmpeg reads same go2rtc loopback
         │   -map 0:a → PCM s16le 16kHz mono → pipe:1
         │     → AudioBroadcaster (0.96s windows, 0.48s hop)
         │       → YAMNet classifier (~10ms/window on CPU)
         │         → high-priority (glass_break/gunshot/scream/siren)
         │           → calls MotionManager.boost_detection(camera_id)
         │             (detect ffmpeg jumps to 5 fps, D-FINE threshold
         │              relaxes to 0.25 for 5 s — plan §7)
         │           → independent motion_events row, source='audio'
         │         → low-priority (bark/car_horn/door_slam/etc)
         │           → enriches recent vision event with sound_class
         │
         └──► Tee D → go2rtc RTSP loopback → libmpv native render
             go2rtc fans out the camera's RTSP stream on its
             loopback address (rtsp://127.0.0.1:58554/<uuid>).
             libmpv, running in-process inside the Tauri shell via
             tauri-plugin-rtsp-mosaic, connects to this loopback
             as an RTSP client and renders each camera tile into a
             native OS surface (NSView on macOS, HWND on Windows)
             positioned below the transparent webview. Hardware
             decode (VideoToolbox on macOS, D3D11 on Windows) is
             used automatically. The frontend controls tile
             lifecycle through <rtsp-tile> custom elements — the
             plugin handles surface creation, layout via
             ResizeObserver, and teardown on disconnect. DOM
             overlays (camera name, REC badge, motion indicator,
             mute button) render in the webview above the native
             surface.
             → <rtsp-tile> custom element → Tauri IPC → libmpv
               → native surface → hardware-decoded video
```

The recorder ffmpeg stream-copies H.264 with zero decode cost; the
detect ffmpeg pays a full software decode only for the 640×auto@2fps
stream (motion-gating keeps D-FINE inference off idle frames). libmpv
handles the live preview path
independently — it opens its own RTSP connection to go2rtc's
loopback and decodes via OS hardware frameworks, completely
bypassing the browser media pipeline (no MSE, no WebRTC, no JS
frame handling). This eliminates the frame drops inherent to
browser-based video delivery.

---

## Key design decisions

### One RTSP per camera (per stream), via go2rtc

Cheap IP cameras (Eufy, no-name Tapos) enforce strict concurrent-client limits — often just 1 or 2. If recording, motion, and preview each open their own RTSP connection, the camera flaps. We let go2rtc hold a single RTSP connection to each camera's main stream and fan out internally. Every downstream consumer reads from `rtsp://127.0.0.1:58554/<uuid>` instead. When the camera also exposes a sub-stream, go2rtc holds a second independent connection to the sub-stream RTSP endpoint (which is a distinct URL path on the same camera server — `/h264Preview_01_sub` on Reolink, `/stream2` on TP-Link, etc.) and fans it out as `rtsp://127.0.0.1:58554/<uuid>_sub`. That's still "one RTSP session per stream per camera" — the camera's concurrent-client limit applies per-endpoint, not per-device, and both Reolink and TP-Link are explicitly designed to serve main+sub simultaneously. Cheaper cameras that can't handle two sessions gracefully degrade: sub-stream registration fails, the recorder logs once and falls back to main, and the onboarding compare UI reports "no storage-friendly stream available" for that camera.

### Live preview path: native rendering via libmpv, not browser media pipelines

Live camera preview is rendered natively via libmpv, completely bypassing the browser's media pipeline. The `tauri-plugin-rtsp-mosaic` plugin (MIT/Apache-2.0, open source at `github.com/ben-ic/tauri-plugin-rtsp-mosaic`) runs libmpv in-process inside the Tauri shell. Each camera tile:

1. Creates a native OS surface (NSView on macOS, HWND on Windows) positioned below the transparent webview
2. Initializes an mpv render context with `vo=libmpv` + OpenGL, binding mpv to the surface's GL context
3. Connects to go2rtc's RTSP loopback (`rtsp://127.0.0.1:58554/<uuid>`) as an RTSP client
4. Decodes via OS hardware frameworks (VideoToolbox on macOS, D3D11 on Windows) at zero CPU cost
5. Renders frames into the native surface via a condvar-driven render thread

The webview sits transparent on top. DOM elements (camera name labels, REC badge, motion indicator, mute button) render above the native surface as normal HTML — the user sees them composited over the video. Mouse events pass through the native tile to the webview, so click handlers work on the DOM overlays.

The frontend drives tile lifecycle through the `<rtsp-tile>` custom element provided by the plugin's JS API (`tauri-plugin-rtsp-mosaic-api`). `connectedCallback` creates the native tile, `ResizeObserver` keeps it positioned under the element's bounding rect, and `disconnectedCallback` tears it down. The `NativeCameraTile` React component (`frontend/src/components/NativeCameraTile.tsx`) is a thin wrapper that maps React props to element attributes.

**Why not browser-based video?** Every browser media delivery path we tried had fundamental issues:
- **go2rtc WebRTC/MSE**: works, but frame drops are inherent to the browser media pipeline — the same issue other NVRs have. JS-layer MSE decoding competes with the UI thread.
- **MJPEG over multipart/x-mixed-replace**: fragile browser parsers, random tile stalls with no recovery.
- **Fragmented MP4 via `<video src>`**: go2rtc advertises a finite 3s duration in the moov atom; browsers play to "end" and stop.
- **HLS via hls.js**: go2rtc's HLS muxer omits inline SPS/PPS NALs for many camera streams; the decoder can't initialize.

libmpv eliminates the browser media pipeline entirely. The result is zero frame drops, full hardware decode, and correct handling of every H.264 profile that cameras actually ship — including Reolink High 4.1 streams that choke hls.js's PassThroughRemuxer.

**libmpv render API, not `wid`.** mpv's `gpu` VO always initializes Cocoa windowing when used as a library on macOS — it creates its own window regardless of the `wid` option. The correct embedding approach is `vo=libmpv` + `RenderContext`, which bypasses Cocoa entirely and renders into the host app's OpenGL framebuffer. This is how IINA and all production mpv embedders work. Never use `wid` or `vo=gpu` with libmpv on macOS.

**License**: libmpv is LGPL 2.1+ (built with `-Dgpl=false`). The plugin links against it directly, which is acceptable because the plugin itself is open source (MIT/Apache-2.0). On Windows, `mpv-2.dll` is bundled alongside the exe; on macOS, libmpv is loaded from the Homebrew path in dev mode and bundled in the `.app` for production.

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

**Separation of registration from consumption.** go2rtc is a lazy producer, so we always register both streams when the camera exposes a sub, regardless of the user's setting. The setting only decides which loopback URL `camera_recorder.py` passes to ffmpeg as its `-i` argument. That separation is what enables the onboarding compare UI: the frontend can instantiate two `<rtsp-tile>` elements pointed at `<uuid>` and `<uuid>_sub` simultaneously — without any backend coordination, without any mid-flow go2rtc reconfiguration, without any risk of the recorder losing its session. Live main vs sub on the same screen for ~30 seconds while the user picks; then the sub consumer disconnects and go2rtc's idle sub-stream quietly stops reading from the camera.

**Detection is now independent of sub-stream recording.** In Detection Pipeline v2 the object detector no longer reads frames out of the recorded .mp4 — it runs inline on the per-camera detect-role ffmpeg, which always decodes the **main** go2rtc stream at 640×auto@2fps regardless of what the recorder archives. Flipping `record_substream_when_available` on therefore has zero effect on detection accuracy; archive resolution is decoupled from inference resolution. This closes a v1 gotcha where enabling sub-stream recording silently hurt classification accuracy at distance.

**TODO — surface the sub-stream choice in the UI.** The feature is fully wired backend-side: `Settings.record_substream_when_available` in `backend/models.py`, the POST `/api/settings` handler in `backend/api/settings.py`, the recorder's loopback-URL selection in `backend/recording/camera_recorder.py`, and `frontend/src/types.ts` already carries the field on the Settings type. What's missing is the **user-facing control** — today the only way to flip it is by crafting a raw POST to the settings endpoint, which violates "zero knobs, zero jargon" from `.impeccable.md` because the benefit (20–30× retention) is invisible to anyone who doesn't read the code. The plausible surfaces, in order of preference:

1. **Onboarding compare UI** — during first-run camera setup, show main and sub side-by-side for ~30s (both are already registered in go2rtc by this point — see "Separation of registration from consumption" above) and let the user pick. Frame it as *"Record in high quality (≈2 days of footage) or storage-friendly (≈50 days)?"* — outcome words, not bitrate numbers. Default highlight on high-quality so the user can click through without changing anything.
2. **Settings screen toggle** — the existing Settings modal gains one row: *"Record in storage-friendly mode"* with a one-line explanation of the tradeoff ("Lower-resolution archive, about 25× more days of history, no CPU cost"). Same outcome-focused copy. This is the fallback for users who skip onboarding or change their mind later.

Whichever surface lands first, the copy must never expose "sub-stream," "bitrate," "resolution," or "codec." The product test is "can a non-technical user flip this" — see `docs/product.md`. Both surfaces must handle `substream_uri is None` gracefully (camera has no sub-stream; skip it in the onboarding compare, or show the Settings toggle as disabled with *"This camera only supports high-quality recording"*). The recorder already no-ops correctly in that case, so the UI just needs to not lie about the option being available.

### Recording reliability: cross-pipeline split-brain detection + chronic-failure breaker

Cheap cameras occasionally fail in a shape where ffmpeg keeps running and keeps emitting stderr chatter (`Non-monotonic DTS`, `RTP: missed N packets`) while the muxer silently stops writing bytes. Observed repeatedly on Eufy's fragmented-MP4 pipeline; other brands have the same failure class. The pre-reliability watchdog used stderr as a liveness heartbeat and the recorder would sit "not recording" for minutes at a time with the UI badge still reading RECORDING — tile rendering (via libmpv reading the same go2rtc loopback) kept working the whole time, which made the silent-failure mode especially misleading to the user.

Three mechanisms, built around the fact that the go2rtc loopback architecture means **the recorder and the detector share a producer**, so each can witness the other's liveness.

**Architecture — the shared producer that makes the witness cheap.** Diagrams below use Mermaid; GitHub renders them inline.

```mermaid
flowchart LR
    cam([IP camera])
    go2rtc["go2rtc loopback<br/>rtsp://127.0.0.1:58554/&lt;uuid&gt;"]
    rec["record-ffmpeg<br/>stream-copy → .mp4"]
    det["detect-ffmpeg<br/>640×auto @ 2 fps rgb24"]

    cam -->|single RTSP connection| go2rtc
    go2rtc -->|loopback| rec
    go2rtc -->|loopback| det

    progress(["_last_progress_ts<br/>updated by watchdog<br/>from segment file stat"])
    witness(["get_last_decoded_frame_dt<br/>stamped by DetectFfmpegSource<br/>on every decoded frame"])

    rec -. bytes written .-> progress
    det -. decode-time .-> witness

    progress --> WD{{"_staleness_watchdog"}}
    witness --> WD
```

**Watchdog flow — how the two signals combine into three decisions, the breaker, and the respawn.** `_staleness_watchdog` ticks every `STALE_CHECK_INTERVAL_S=5` seconds; `_process_monitor` reacts when ffmpeg exits.

```mermaid
flowchart TD
    tick([watchdog tick · every 5 s])
    ladder["derive health from bytes-stale ladder<br/>&lt; 10 s ok · &lt; 30 s stalled · ≥ 30 s offline"]
    labelChk{"offline AND<br/>detect &lt; 10 s?"}
    relabel["rewrite label to<br/>record_failing"]
    emit[["emit camera_health<br/>on transition only"]]
    sbChk{"split-brain?<br/>bytes &gt; 15 s AND<br/>detect &lt; 10 s AND<br/>uptime &gt; 15 s grace"}
    psChk{"plain-stall kill<br/>bytes &gt; 30 s?"}
    kill["SIGTERM ffmpeg<br/>+ register restart"]

    tick --> ladder --> labelChk
    labelChk -- yes --> relabel --> emit
    labelChk -- no --> emit
    emit --> sbChk
    sbChk -- yes --> kill
    sbChk -- no --> psChk
    psChk -- yes --> kill
    psChk -- no --> tick

    exitEv([ffmpeg exits])
    ffChk{"fast-fail?<br/>rc ≠ 0 within<br/>5 s of spawn"}
    ffReg["register restart"]
    bridge[["emit camera_health = offline<br/>(bridges respawn gap)"]]
    backoff["sleep backoff"]
    spawn["_spawn reads<br/>camera.recording_stream_override<br/>→ picks main or sub loopback"]

    kill --> exitEv
    exitEv --> ffChk
    ffChk -- yes --> ffReg --> bridge
    ffChk -- no --> bridge
    bridge --> backoff --> spawn
    spawn --> tick

    deque[("sliding deque<br/>window 600 s · threshold 3")]
    trip{"≥ 3 in window?"}
    chronic["_handle_chronic_failure<br/>1. emit chronic_recording_failure<br/>2. db.set_stream_override_if_not_set('sub')<br/>   (WHERE override IS NULL OR = 'main')<br/>3. refresh self.camera · clear deque<br/>4. SIGTERM ffmpeg"]

    kill -. register .-> deque
    ffReg -. register .-> deque
    deque --> trip
    trip -- yes --> chronic
    chronic --> exitEv

    retry["POST /api/cameras/{id}/retry-main-stream<br/>clears override + fallback_reason · bounces recorder"]
    retry -. .-> exitEv
```



1. **File-growth is the sole recorder liveness signal.** Stderr is retained only for the post-mortem tail buffer and the `Opening '<path>.mp4' for writing` regex that dispatches new-segment events. If the on-disk segment hasn't grown in `STALE_FRAME_THRESHOLD_S=30` seconds, ffmpeg gets killed and respawned with backoff. A camera that never writes any bytes (truly dead upstream) is caught by ffmpeg's own `-timeout 30000000` socket-I/O deadline at the RTSP demuxer, not by this watchdog.

2. **Cross-pipeline split-brain detection** gets ahead of the 30 s kill by 15 s when the failure is scoped to the recorder. `DetectFfmpegSource` stamps `_last_frame_decoded_at` on every successful rawvideo read — decode-time, not emission-time, because empty-scene cameras never fire downstream events but still decode frames and those are exactly the cameras we need to witness. `MotionManager.get_last_decoded_frame_dt(camera_id)` returns `None` cleanly when detection is disabled (`SIMPLENVR_CLASSIFIER=off`) or the detector isn't attached for that camera. `CameraRecorder._staleness_watchdog` polls the witness timestamp every `STALE_CHECK_INTERVAL_S=5` and trips the split-brain branch when `should_trip_split_brain` returns true: record bytes stale > 15 s, detect fresh < 10 s, recorder uptime > 15 s grace. SIGTERM the ffmpeg, `_process_monitor` respawns it. No surveyed open-source NVR does this because no surveyed NVR has our go2rtc-loopback-shared-producer architecture — without that architecture there is no cheap detect-witness signal to consult.

3. **Chronic-failure circuit breaker + sub-stream fallback.** When any watchdog-initiated restart fires, `_register_recording_failure_restart()` appends the monotonic timestamp to a per-recorder sliding-window deque. `prune_and_check_breaker_trip` drops entries older than `CIRCUIT_BREAKER_WINDOW_S=600` and returns true at `CIRCUIT_BREAKER_THRESHOLD=3`. All three failure shapes feed the same deque: split-brain kills, plain-stall kills (both record AND detect stalled — typically a flapping upstream starving the loopback), and fast-fail exits (process dies with `rc != 0` inside `_FAST_FAIL_THRESHOLD_S=5` — characteristic of RTSP/codec/auth negotiation loops that never write a byte). All three collapse to the same remedy: drop to the lower-bitrate sub-stream so *something* keeps recording while the user investigates. When the breaker trips, `_handle_chronic_failure` emits `camera_health = "chronic_recording_failure"`, calls `db.set_stream_override_if_not_set(conn, camera_id, override='sub', reason=...)`, refreshes `self.camera`, and terminates the ffmpeg — the normal respawn path consults the new override in `_spawn()` and brings up the sub-stream. The helper's `WHERE id = ? AND (recording_stream_override IS NULL OR recording_stream_override = 'main')` guard is effectively an idempotency check today: it skips rows already at `'sub'` so re-trips don't churn the column. The `OR = 'main'` branch is forward-compat — no current code path writes `'main'`; the only writes to the column are `'sub'` from this helper and `NULL` from `clear_stream_override`. A future explicit "always use main" user picker will slot into the `'main'` value and the guard will already be shaped to treat it as non-explicit (the breaker *would* flip `'main' → 'sub'` on a chronic failure today if anything ever wrote it). `POST /api/cameras/{camera_id}/retry-main-stream` calls `clear_stream_override` (sets the column back to NULL) and bounces the recorder; the frontend surfaces a "Try higher quality" button when `recording_stream_override = 'sub'` is set.

**Two camera columns carry the breaker state:** `recording_stream_override` (`NULL | "main" | "sub"`) and `fallback_reason` (free-text diagnostic, never shown except as an optional tooltip). `_spawn()` consults the override ahead of the global `record_substream_when_available` setting, so a post-breaker recorder stays on sub across process restarts until the user explicitly retries.

**Health labels are the user-visible surface.** The file-growth ladder alone emits `"ok"` / `"stalled"` / `"offline"`. `should_label_record_failing` rewrites `"offline"` to `"record_failing"` when detect-ffmpeg is fresh, because the file-growth watchdog can't otherwise distinguish "camera unreachable" from "camera reachable but muxer stuck" — both look like 30 s of no-bytes-written. Precedence on the tile badge: `offline` > `record_failing` > `stalled` > `chronic_recording_failure` (degraded) > `recording`. The topbar aggregates each state into a separate pill — `"N offline"`, `"N unable to record"`, `"N recording in lower quality"` — with label + position carrying the meaning, not color alone (see `.impeccable.md` Principle 5).

**`camera_health` WS event shape is asymmetric by design.** Normal transitions carry `{camera_id, health, last_frame_at}`; chronic-failure events carry `{camera_id, health, reason}` instead. The frontend uses a discriminated switch on `health` so chronic events don't clobber `last_frame_at` with undefined. This bit frontend code once during wiring — preserve the asymmetry; don't try to unify the payload shape.

**Kill switch.** `SIMPLENVR_CLASSIFIER=off` disables detection entirely, which makes the detect-witness `None` everywhere. `should_trip_split_brain` and `should_label_record_failing` both degrade gracefully: no split-brain trips, and the record_failing rewrite is skipped so the label stays at the conservative `"offline"`. The file-growth watchdog and the chronic-failure breaker still run unchanged — the reliability floor doesn't depend on the classifier being on.

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

### Hub devices (Reolink NVR, TP-Link Tapo Hub, etc.)

Some brands organize cameras behind a hub or NVR that's the only thing on the network. The cameras themselves may be battery-powered and sleep between motion events — they don't have their own LAN presence. The hub is always on, has its own IP and hostname, and serves RTSP streams for the cameras behind it on different paths.

SimpleNVR models this with three device types:

- **`camera`** — a direct IP camera with its own LAN presence
- **`hub`** — a gateway device (Reolink NVR, TP-Link Tapo H200, etc.)
- **`hub_camera`** — a camera that lives behind a hub; has no direct LAN address

Discovery finds hubs the same way it finds cameras (ONVIF WS-Discovery, reverse DNS, MAC OUI). After the user authenticates to a hub, we enumerate the cameras behind it by probing the known RTSP path patterns. Each hub-camera gets its own entry in the `cameras` table with `parent_hub_id` set to the hub.

**Hub RTSP support by brand:**

| Brand | Hub models | RTSP | Path pattern | Notes |
|-------|-----------|------|-------------|-------|
| **Reolink** | RLN8-410, RLN16-410, Home Hub | Yes | `/h264Preview_01_main`, `_02_main` ... `_16_main` (channel per camera) | ONVIF also works. Same URL structure as standalone cameras with a channel index. Up to 16 channels. |
| **TP-Link (Tapo)** | H200 hub, NVR models | Yes | Standard Tapo paths per channel | Same RTSP protocol as standalone Tapo cameras. |
| **Eufy** | HomeBase 2 (T8010) | Yes | `/live0`, `/live1`, `/live2` ... | Requires manual RTSP enable per-camera in the Eufy Security app (Device Settings → Network → RTSP Stream). Stream comes from the HomeBase IP, not the camera. One RTSP client limit (go2rtc handles this). Battery cameras stream only during motion — packets arrive in bursts with >60s quiet windows. |
| **Eufy** | HomeBase 3 (T8030) | **No** | — | Anker deliberately removed RTSP to push cloud subscriptions. No firmware setting, no hidden endpoint, no ONVIF. Community reverse-engineering (eufy-security-ws) exists but breaks with firmware updates and is legally gray. **Out of scope permanently** — same category as Ring, Blink, Nest. |
| **Arlo** | SmartHub (VMB series) | **No** | — | Cloud-only. Out of scope. |

**Implementation status (Phase 2, not yet built):** The data model, fingerprints, and RTSP probe patterns exist in the codebase. What's missing is the hub enumeration logic in the scanner — the step where, after discovering and authenticating to a hub, we probe the per-channel RTSP paths and create `hub_camera` entries. The build order is: Reolink hubs first (they have ONVIF + clean RTSP, largest user base), then TP-Link, then Eufy HomeBase 2 (requires the user to manually enable RTSP in the Eufy app first, which complicates the zero-config story).

**Sleep handling is a UX concern, not a technical failure.** Sleeping hub-cameras (battery cams, doorbells) are marked `status: "asleep"` rather than `"offline"` or `"error"`. The dashboard tile shows a distinct "asleep, will wake on motion" state with the last-motion timestamp. When the camera wakes and the RTSP stream starts delivering frames again, the tile transitions to live view automatically. We never show "Can't connect" for a camera that's in its designed sleep state — that would be a user-facing lie. File-growth is a strictly stronger liveness signal than packet timing for these cameras — Eufy's RTSP server emits packets in bursts, and a 60s silence is normal, not an outage.

### SQLite for state

Zero-config, file-based, adequate for 32 cameras. Lives in the user's app data directory. WAL mode for concurrent reads during writes.

### Bundled binaries, not dependencies on the host

FFmpeg, ffprobe, go2rtc, tether, and libmpv are all bundled inside the app package. We do not depend on the user having anything installed. This is non-negotiable for the "it just works" promise — asking a non-technical user to `brew install ffmpeg` is a showstopper. On Windows, `mpv-2.dll` is fetched by `scripts/fetch_mpv.ps1` from a pinned build at `github.com/ben-ic/libmpv-win64` (SHA256-verified) and placed in `src-tauri/binaries/` so it ships alongside the exe. On macOS, libmpv is bundled in the `.app`.

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
  detect_frames/          Per-camera detect-role ffmpeg (640x@2fps RGB24)
                          + single-slot newest-frame queue
  motion/                 Detection Pipeline v2 (D-FINE + ByteTrack +
                          8-layer FP filter + heatmap)
    detector.py             MotionDetector: bg sub + scene guard +
                            D-FINE + tracker + FP chain + events
    manager.py              MotionManager: shared ORT session,
                            per-camera lifecycle, audio-boost entry
    tracker.py              ByteTracker: Kalman + Hungarian 2-pass
    confidence.py           Beta(α,β) + recoverable-doubt state machine
    heatmap.py              16×12 per-camera false-alarm grid
                            (SQLite: detection_heatmap table)
    clip.py                 Unchanged: per-event MP4 clip stitcher
  classification/         D-FINE-N object detection + capability probe
    dfine.py                DFineDetector: one shared ORT session
    capability_probe.py     Hardware telemetry (OS/arch/RAM/latency)
    labelmap.py             COCO-80 → {person, vehicle, animal, None}
    models/                 dfine_n.onnx (15.3 MB) + yamnet.onnx
                            (15 MB) + yamnet_classes.txt + NOTICE.txt,
                            all committed in-tree so the build pipeline
                            has zero network dependency. PyInstaller
                            copies them into the bundle via
                            backend/main.spec; scripts/fetch_dfine.sh
                            verifies the pinned SHA256.
  story/                  Template-based story compiler
  api/                    FastAPI routes + WebSocket event bus
  main.spec               PyInstaller spec (onedir mode)

src-tauri/                Tauri Rust shell + supervisor
  Cargo.toml              Workspace root (depends on tauri-plugin-rtsp-mosaic)
  src/lib.rs              Sidecar spawning, lifecycle, crash dialogs, plugin init
  build.rs                Forwards TARGET_TRIPLE to rustc env
  tauri.conf.json         Tauri config (externalBin, resources, bundle settings)
  capabilities/           Tauri shell allow-lists (includes rtsp-mosaic:default)
  tether/                 Cross-platform parent-death supervisor (see below)
    Cargo.toml
    src/main.rs
  binaries/               Bundled binaries (gitignored, fetched by scripts)
                          Includes mpv-2.dll on Windows
  resources/              License attribution files

tauri-plugin-rtsp-mosaic  Native video rendering plugin (external repo,
                          git dep at github.com/ben-ic/tauri-plugin-rtsp-mosaic)
  - Rust: libmpv render context, NSView/HWND surface management,
    tile create/destroy/layout IPC commands, health event emitter
  - JS API: <rtsp-tile> custom element, onTileEvent listener,
    setAllVisible/setAllMuted bulk controls

frontend/                 React + TypeScript + Vite
  src/components/         Dashboard, Discovery, Playback, Settings, etc.
    NativeCameraTile.tsx   React wrapper around <rtsp-tile> custom element
    Home.tsx               Live grid uses NativeCameraTile for each camera
  src/hooks/              useStorage, useDiscovery
    useTileEvents.ts       Subscribes to native tile health events (first_frame,
                           stalled, restarting, failed, stats) for per-tile badges
  src/api/client.ts       Backend REST client
  src/lib/backend.ts      apiUrl / apiFetch / wsUrl helpers
  vite.config.ts          Dev server proxy to backend

scripts/                  Build helpers (bash + PowerShell variants)
  fetch_ffmpeg.sh         Download LGPL-clean FFmpeg for target triple
  fetch_go2rtc.sh         Download pinned go2rtc release
  fetch_mpv.ps1           Download pre-built libmpv for Windows (SHA256-verified)
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

# Windows only: fetch pre-built libmpv (mpv.lib + mpv-2.dll)
scripts/fetch_mpv.ps1       # mpv.lib → src-tauri/, mpv-2.dll → src-tauri/binaries/

# macOS: libmpv is loaded from Homebrew (brew install mpv) in dev mode.
# The .app bundle embeds libmpv.dylib via the Tauri resource pipeline.

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
