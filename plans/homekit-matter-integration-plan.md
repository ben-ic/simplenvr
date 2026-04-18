# Smart-home ecosystem integration (HomeKit → Matter)

> **Picking this up cold?** Read this top-to-bottom. The load-bearing product rule is in §Architecture: **integration is additive, never subtractive.** Publishing cameras to Apple Home or Google Home / Matter never changes how SimpleNVR records them locally, never routes recording through a vendor's cloud, never asks the user to pay a second party for features they already paid SimpleNVR for. Ecosystem views are a *publication* of the camera stream, not a replacement for the local NVR.

## Context

As of 2026-04-18 SimpleNVR records, detects, classifies, and surfaces per-camera events inside its own web UI and native mpv live tiles. There is no path for Linda to ask Siri "show me the driveway," no iPhone lock-screen widget, no Google Home grid tile, no "motion at the gate" notification in the phone UI that Linda already uses for her lights and lock.

Real households mix ecosystems. Linda has an iPhone and a HomePod mini. Her part-time helper Marcus has a Pixel and a Nest Hub. A commercial NVR that asks both users to open a dedicated browser tab to see their cameras is the loser to an out-of-the-box competitor that publishes to the Home app they already open every day.

**We integrate on the user's terms, not ours.** Linda's experience: cameras sit in Apple Home next to her lock and lights; Siri works; her Watch buzzes on motion. Marcus's experience: cameras sit in Google Home next to his Nest thermostat; "Hey Google, show me the back door on the kitchen display" works. Both experiences from the same SimpleNVR install, same cameras, same local recording — just two extra *publication* layers running alongside the existing pipeline.

We ship HomeKit first (mature stack, richer iPhone experience via HKSV-adjacent features, works today). We add Matter second to reach the *non-Apple* ecosystems in one swing: Google Home (Android / Nest Hub), Amazon Alexa (Echo Show / Echo Hub; Amazon was a founding CSA member and shipped Matter controller support in late 2022), and Samsung SmartThings. **Apple users are deliberately kept on the HomeKit path from Phase 1** — Matter technically also works in Apple Home, but offering an iPhone user two ways to pair the same cameras (HomeKit QR vs. Matter QR) is a confusion trap, and HomeKit gives them richer features than Matter does today anyway. Linda pairs via Apple Home, Marcus pairs via Google Home (or Alexa, or SmartThings), both experiences are native in their respective apps, and neither user is ever confronted with a protocol choice.

## Architecture

**The load-bearing rule:** *integration is additive, never subtractive.* Three specific anti-patterns this rule guards against:

1. **HKSV must not replace SimpleNVR's recording.** HomeKit Secure Video is Apple's proprietary 10-day iCloud-backed recording layer. If a user enables it on a camera, Apple nudges them to pay iCloud+ ($3/mo) for a worse recording experience than SimpleNVR already gives them locally at 30+ days. We advertise the bridge with HKSV *disabled* by default and surface it behind a developer-only env var while the product posture is evaluated. The user always sees SimpleNVR's browse/review UI as the authoritative recording store.

2. **Ecosystem view is always read-mostly.** Pairing cannot delete cameras, change credentials, alter recording paths, or disable detection. The only writes from the ecosystem side are user-facing cosmetics (accessory name sync, room assignment metadata). Everything else is publication out of the SimpleNVR camera objects.

3. **Local recording is the source of truth.** Neither HomeKit nor Matter gets to gate whether the recorder runs. A broken bridge, an expired pair, a Wi-Fi hiccup that takes the ecosystem layer offline — none of these change whether segments land on disk. The recorder's lifecycle is completely independent of both publication layers, with the integration sitting purely as an async subscriber to camera / motion / recording events on the existing event bus.

### Phase overview

| Phase | Scope | Reaches | Weeks | Dependencies |
|-------|-------|---------|-------|--------------|
| **0** | Research spike: library eval, legal posture, video-protocol validation | — | 1 | — |
| **1A** | HomeKit Bridge MVP: live streaming + motion sensors + setup QR | Apple Home | 4–5 | 0 |
| **1B** | HomeKit polish: per-camera enable, dynamic add/remove, QA matrix | Apple Home | 1–2 | 1A |
| **2A** | Matter research spike: library choice, controller-maturity snapshot across Google / Alexa / SmartThings | — | 1 | 1B |
| **2B** | Matter Bridge MVP: Wi-Fi Matter, Camera device type, cross-ecosystem commissioning | **Google Home + Alexa + SmartThings** | 4–6 | 2A |
| **2C** | Dual-protocol polish: unified settings UI, multi-pairing edge cases across the three Matter ecosystems | All | 1–2 | 2B |

Total: 12–17 weeks of focused work, end to end. Phase 1 can ship independently and deliver value to iPhone-only households before Phase 2 starts. **Phase 2's leverage is that one implementation reaches every *non-Apple* smart-home ecosystem** — Android owners via Google Home, Echo owners via Alexa, Samsung owners via SmartThings. Apple Home is already covered by Phase 1's HomeKit bridge, which is richer than Matter anyway (HKSV-adjacent features, mature controller support). We deliberately do **not** route Apple users through Matter: it would offer them a strictly worse experience than the HomeKit bridge they already have, and surfacing two pair paths to the same ecosystem is a UX trap.

### Phase 1: HomeKit Bridge

**Library choice:** `HAP-python` (Apache 2.0) — the most mature HomeKit accessory library for Python, used in production by thousands of Homebridge-adjacent bridges. Same Python runtime as SimpleNVR's sidecar, so no language bridge needed. LGPL/GPL-free. License posture matches the project's permitted list (see MEMORY.md `feedback_licensing`).

**Topology:** *bridge mode*, not standalone-accessory mode. One bridge accessory (SimpleNVR itself) enumerates N child accessories (one per camera). Bridge mode is load-bearing because:

- Cameras can be added/removed at runtime without unpairing the bridge. The user pairs SimpleNVR once; every camera they add through SimpleNVR afterward appears automatically in Apple Home.
- The 150-accessory-per-bridge cap is comfortably above our 32-camera limit.
- Bridge pairing keys live in one file, not N files — simpler state management.

**Per-camera accessory shape:** each camera advertises, at minimum:

- `BridgedAccessoryInformation` — manufacturer ("SimpleNVR"), model (the camera's actual make/model from our ONVIF interrogation), serial (our DB UUID), firmware.
- `CameraRTPStreamManagement` — the HAP video-streaming service. Expects SRTP-over-UDP video on demand. See §Video streaming below.
- `Microphone` — silenced by default; placeholder for future two-way or audio-classification event surfacing.
- `MotionSensor` — `MotionDetected` characteristic. Flipped by the existing `motion_started` / `motion_ended` event bus messages (emitted by detection-v2 after D-FINE + ByteTrack + the 8-layer FP filter have approved a track). This is how Apple Home fires push notifications to the user's iPhone / Watch. **The FP filter's work directly benefits the HomeKit UX**: we never forward a raw D-FINE hit, only an approved track, so users don't get buzzed by leaf-jiggle or heatmap-known FP cells. The bridge does zero detection work — it's a pure republisher of verdicts our pipeline already makes.
- `OperatingMode` — HomeKit's "camera off" toggle in Home app. When the user flips it, we honor that by suppressing the motion sensor and live stream in the HomeKit layer ONLY. The underlying SimpleNVR recorder keeps running (additive rule).

**Explicitly NOT advertised by default:**

- `RecordingManagement` (HKSV) — the competitive-trap feature. Guarded behind `SIMPLENVR_HOMEKIT_HKSV=1` env var. Until Ben has a clear product and legal posture on iCloud-duplicated recording, HKSV stays off.
- `Battery` — all our cameras are PoE or wired.
- `LightBulb` — irrelevant.

### Video streaming (the actually-hard part of Phase 1)

HAP doesn't speak RTSP or HLS. When an Apple Home client wants to view a camera, the flow is:

1. Client sends `setup-endpoints` to our `CameraRTPStreamManagement` — it supplies an SRTP key and a UDP destination port for video and audio.
2. We respond with our own SRTP keys and a UDP port we'll send video *from*.
3. When the client sends `selected-stream-configuration` with "start," we spawn or attach to a per-session ffmpeg that reads from go2rtc's RTSP loopback and outputs:
   - Video: H.264 Baseline or Main profile, specific size the client negotiated (typically 1280×720 or 1920×1080), encapsulated as SRTP-over-UDP, keyed with the session's SRTP key.
   - Audio: AAC-LC at 16 kHz, mono, also SRTP-keyed.
4. On "end," we tear down the per-session ffmpeg.

Critically, **this is NOT the recording ffmpeg.** It's a per-viewing-session live-transcode ffmpeg that reads the same go2rtc loopback. Multiple Apple Home viewers on different devices each get their own session. Resource cost: one extra ffmpeg per active HomeKit viewer, typically 2-5% CPU at 720p on the Snapdragon X Elite target.

`HAP-python` includes a camera-accessory base class with most of the SRTP plumbing. We subclass it, override the "start stream" hook to spawn our ffmpeg pointed at the camera's go2rtc loopback, and tear down on "stop."

### Phase 2: Matter Bridge

**Library choice (pending research spike):** two viable paths:

- **python-matter-server** (Apache 2.0) — Home Assistant's Matter shim. Best documented for controller-side use; its server-side (accessory-side) surface is younger. Pro: shares the async Python ergonomics we already use. Con: primarily a controller library, so advertising Matter accessories from it may require extensions.
- **CHIP Python bindings** (Apache 2.0) — upstream Connectivity Standards Alliance Python bindings. More spec-aligned. Con: tighter coupling to native code (chip-matter runtime), harder to bundle cleanly in PyInstaller.

Phase 2A is explicitly a 1-week spike to validate which library lets us advertise a Matter Camera device type today, with a concrete handshake test against the latest Google Home (Android) and Apple Home (iOS 18+) controllers.

**Topology:** Matter aggregator (logical equivalent of HomeKit's bridge). One endpoint per camera, each implementing the Matter *Camera* device type from the Cameras cluster (spec: Matter 1.2, approved late 2024).

**Transport:** Wi-Fi, not Thread. SimpleNVR runs on Windows/Mac/Linux hosts that don't have a 15.4 radio, so Thread is architecturally off the table. Wi-Fi Matter doesn't need a Thread Border Router, just a standard Matter controller hub in the home.

**The three Matter controllers we target (as of 2026):**

- **Google Home** — runs on Nest Hub, Nest Hub Max, Google TV Streamer, select Android phones acting as controllers. Commissions via Google Home app on Android / iOS.
- **Amazon Alexa** — runs on Echo (4th gen+), Echo Show (8 / 10 / 15 / 21), Echo Hub, Echo Dot (5th gen+). Commissions via Alexa app on Android / iOS. Amazon was a founding Matter contributor; Matter controller support landed in late 2022.
- **Samsung SmartThings** — runs on SmartThings Hub, SmartThings Station, newer Samsung TVs, newer Samsung Galaxy phones. Commissions via SmartThings app.

**Apple Home is deliberately excluded from the Matter UX.** Apple Home is technically a Matter controller (we could pair into it), but Apple users already have a richer experience via Phase 1's HomeKit bridge. Surfacing two separate pair paths — HomeKit QR vs. Matter QR — to the same iPhone user is a UX trap: they'd wonder which to scan, pair with the wrong one, and get an inferior experience. SimpleNVR's Matter Settings panel therefore lists only Google Home, Alexa, and SmartThings. Advanced users who insist on Matter-into-Apple-Home can still scan the Matter QR from the Apple Home app — the protocol works — but we don't prompt them to.

**One implementation, three ecosystems.** The strategic reason to do Phase 2: a single Matter bridge reaches every non-Apple household in one go, without writing three separate integrations.

**Commissioning:** the user commissions SimpleNVR as a Matter device from their chosen ecosystem's app. Every ecosystem's commissioning flow accepts the same Matter QR code and 11-digit setup code (that's the Matter spec promise). The QR is rendered in SimpleNVR's Settings UI alongside the HomeKit QR. Two separate pairings, two separate QR codes — HomeKit and Matter don't share commissioning state; but within Matter, *one* QR works for all three target ecosystems.

**Multi-admin (aka "Matter fabrics"):** Matter allows the same device to be commissioned into *multiple* ecosystems simultaneously. Marcus commissions SimpleNVR into Google Home from his Pixel; his wife commissions the same install into Alexa from her Echo Show in the kitchen; his brother-in-law commissions it into SmartThings from his Galaxy; all three fabrics run concurrently against the same SimpleNVR cameras. This is Matter's killer feature for mixed non-Apple households. (Apple users coexist in this picture via the separate HomeKit bridge from Phase 1 — they don't participate in the Matter fabric at all.)

**Video streaming in Matter Cameras cluster:** the Matter spec defines a `StreamProvider` cluster for on-demand H.264 / H.265 streaming. Less mature in controllers than HAP's SRTP path — Phase 2A must confirm controllers actually consume the stream, not just recognize the device. If they don't yet, Phase 2B may ship with device recognition + motion events but without live view until controllers catch up, which is still better than nothing (motion notifications are the 80% of daily value).

### Dual-protocol coexistence

One Python sidecar process runs both HAP-python and the Matter server as independent asyncio tasks. They don't share state above the camera object layer — the same `Camera` model gets published via both advertisement layers; per-ecosystem state (pairing keys, paired-client public keys, admin fabrics) lives in separate files under `DATA_DIR/ecosystem/{homekit,matter}/`.

**mDNS coexistence:** HAP advertises `_hap._tcp`, Matter advertises `_matter._tcp`. Different service names, different ports, no collision. Both run on the same host interface without mDNS interference.

**Event-bus subscribers:** both bridges subscribe to the same `camera_found` / `camera_updated` / `camera_deleted` / `motion_started` / `motion_ended` events. Camera add/remove triggers advertisement / removal in both layers simultaneously.

**Single settings surface:** one Settings page with two panels side by side — "Apple Home" (with HomeKit QR + setup code) and "Google Home / Matter" (with Matter QR + setup code). Both can be toggled independently. Zero-config default: both enabled, both QR codes visible, user scans whichever they need.

## Schema

Each protocol needs a small amount of persistent state; neither one needs database columns — file-based per-protocol state directories match how other Python libraries (HAP-python in particular) expect to own their state:

```
DATA_DIR/
├── ecosystem/
│   ├── homekit/
│   │   ├── accessory.state          # HAP-python bridge state (keys, paired clients)
│   │   ├── camera_ids.json          # bridge-accessory-id → SimpleNVR camera UUID map
│   │   └── hksv_keys/               # HKSV crypto keys (only written when HKSV env var set)
│   └── matter/
│       ├── fabric.state             # Matter fabric / node ID state
│       ├── paired_fabrics.json      # multi-admin fabric registry
│       └── commissioning.state      # setup code / QR payload / discriminator
```

A single new `settings` row records whether each ecosystem layer is enabled:

```sql
-- Idempotent migration in backend/db.py:
--   settings key: 'homekit_enabled'  value: 'true' | 'false'   (default 'true')
--   settings key: 'matter_enabled'   value: 'true' | 'false'   (default 'true')
```

No new `cameras` columns — per-camera HomeKit/Matter enable flags are just bits in the per-protocol state files, since they're publication-layer metadata and irrelevant if the user never pairs.

## Files to modify

**Phase 1 (HomeKit):**

- `backend/ecosystem/__init__.py` — new subsystem package.
- `backend/ecosystem/homekit/bridge.py` — the HAP-python bridge manager. Subscribes to camera/motion events, owns the bridge accessory lifecycle.
- `backend/ecosystem/homekit/camera_accessory.py` — per-camera HAP accessory. Subclass of HAP-python's `Camera` base class; overrides start-stream / stop-stream to spawn our per-session ffmpeg.
- `backend/ecosystem/homekit/stream_ffmpeg.py` — per-session ffmpeg lifecycle. Reads from go2rtc loopback, outputs SRTP-keyed H.264 + AAC.
- `backend/ecosystem/homekit/state.py` — persistence of bridge keys + paired clients in `DATA_DIR/ecosystem/homekit/`.
- `backend/ecosystem/homekit/qr.py` — setup-code → QR-payload → SVG rendering for the settings UI.
- `backend/api/ecosystem.py` — FastAPI endpoints: `GET /api/ecosystem/homekit/status`, `POST /api/ecosystem/homekit/reset-pairing`.
- `backend/main.py` — wire the bridge into the lifespan, start after RecordingManager, stop during shutdown.
- `backend/db.py` — `homekit_enabled` / `matter_enabled` setting keys with defaults.
- `frontend/src/components/Settings/EcosystemPanel.tsx` — new settings panel. Shows HomeKit QR, enable toggle, paired-client list, "reset pairing" button.
- `pyproject.toml` — add `HAP-python` dependency.
- `scripts/bundle_python.sh` — confirm HAP-python dependencies are PyInstaller-compatible; may need `--collect-all` spec entries.
- `docs/architecture.md` — add "Ecosystem publication" subsystem doc.
- `tests/test_homekit_bridge.py` — decision-boundary tests for the subscribe-add-remove cycle.

**Phase 2 (Matter):**

- `backend/ecosystem/matter/bridge.py` — Matter aggregator lifecycle.
- `backend/ecosystem/matter/camera_endpoint.py` — per-camera Matter Camera device type.
- `backend/ecosystem/matter/stream_provider.py` — Matter StreamProvider cluster implementation (spec-dependent; may be deferred if controller support lags).
- `backend/ecosystem/matter/state.py` — fabric / commissioning state persistence.
- `backend/api/ecosystem.py` — extended with Matter endpoints.
- `frontend/src/components/Settings/EcosystemPanel.tsx` — extended with Matter QR + toggle.
- `pyproject.toml` — Matter library (exact choice per Phase 2A spike).
- `tests/test_matter_bridge.py` — corresponding decision-boundary tests.

## Pseudocode

### HomeKit bridge — event-bus integration

```python
# backend/ecosystem/homekit/bridge.py
class HomeKitBridge:
    def __init__(self, event_bus, camera_store, settings, state_dir):
        self._bus = event_bus
        self._cameras = camera_store
        self._state_dir = state_dir
        self._bridge: pyhap.AccessoryDriver | None = None
        self._accessories: dict[str, CameraAccessory] = {}  # cam_id -> accessory

    async def start(self):
        self._bridge = pyhap.AccessoryDriver(
            persist_file=str(self._state_dir / "accessory.state"),
            port=51826,  # fixed HAP port; firewall opens this
        )
        bridge_accessory = pyhap.accessory.Bridge(self._bridge, "SimpleNVR")

        for camera in await self._cameras.get_online():
            accessory = CameraAccessory(self._bridge, camera, go2rtc_loopback_url_for(camera))
            bridge_accessory.add_accessory(accessory)
            self._accessories[camera.id] = accessory

        self._bridge.add_accessory(bridge_accessory)
        self._bus.subscribe("camera_found", self._on_camera_added)
        self._bus.subscribe("camera_deleted", self._on_camera_removed)
        self._bus.subscribe("motion_started", self._on_motion_start)
        self._bus.subscribe("motion_ended", self._on_motion_end)
        asyncio.create_task(self._bridge.async_start())

    async def _on_motion_start(self, event):
        cam_id = event["camera_id"]
        accessory = self._accessories.get(cam_id)
        if accessory:
            accessory.motion_sensor.get_characteristic("MotionDetected").set_value(True)

    async def _on_motion_end(self, event):
        cam_id = event["camera_id"]
        accessory = self._accessories.get(cam_id)
        if accessory:
            accessory.motion_sensor.get_characteristic("MotionDetected").set_value(False)
```

### HomeKit per-session streaming

```python
# backend/ecosystem/homekit/camera_accessory.py
class CameraAccessory(pyhap.camera.Camera):
    """Per-camera HAP accessory wired to a go2rtc RTSP loopback."""

    def __init__(self, driver, camera: Camera, rtsp_url: str):
        super().__init__(
            driver=driver,
            display_name=camera.name or f"Camera {camera.ip}",
            serial_number=camera.id,
            # ... configuration/serial plumbing ...
        )
        self._rtsp_url = rtsp_url
        self._camera = camera
        self._session_ffmpegs: dict[str, asyncio.subprocess.Process] = {}

    async def start_stream(self, session_info, stream_config):
        """Invoked by HAP-python when a paired client requests a live view.

        session_info carries the SRTP keys and UDP destinations negotiated
        during setup-endpoints. stream_config carries resolution, fps,
        and bitrate ceiling.
        """
        cmd = build_homekit_stream_cmd(
            rtsp_url=self._rtsp_url,
            srtp_key=session_info["srtp_key"],
            video_dst=(session_info["video_ip"], session_info["video_port"]),
            audio_dst=(session_info["audio_ip"], session_info["audio_port"]),
            width=stream_config["width"],
            height=stream_config["height"],
            fps=stream_config["fps"],
            bitrate=stream_config["bitrate"],
        )
        self._session_ffmpegs[session_info["id"]] = await spawn_proc(*cmd)

    async def stop_stream(self, session_info):
        proc = self._session_ffmpegs.pop(session_info["id"], None)
        if proc:
            proc.terminate()
            await proc.wait()
```

### Matter bridge (Phase 2, subject to library choice)

```python
# backend/ecosystem/matter/bridge.py — shape only; concrete API TBD in 2A spike.
class MatterBridge:
    async def start(self):
        self._server = MatterServer(
            fabric_store=self._state_dir / "fabric.state",
            port=5540,
            vendor_id=<pending_CSA_registration>,
            product_id=<pending>,
        )
        for camera in await self._cameras.get_online():
            endpoint = MatterCameraEndpoint(
                camera=camera,
                stream_provider=GoToRtcStreamProvider(camera),
            )
            self._server.add_endpoint(endpoint)
        self._bus.subscribe("camera_found", self._on_camera_added)
        # ... same pattern as HomeKit ...
        await self._server.start()
```

## UX touches

**Settings UI** (`EcosystemPanel.tsx`, new):

```
┌─ Apple Home ─────────────────────────────────┐    ┌─ Google Home · Alexa · SmartThings ──────────┐
│                                              │    │                                              │
│   [QR code]                                  │    │   [QR code]                                  │
│                                              │    │                                              │
│   Setup code: 123-45-678                     │    │   Setup code: 1234-5678-901                  │
│                                              │    │                                              │
│   Scan this QR from Apple Home → Add         │    │   Scan this QR from Google Home, Alexa, or   │
│   Accessory to pair SimpleNVR.               │    │   SmartThings → Add Device → Matter. The     │
│                                              │    │   same code works in all three.              │
│   Paired devices:                            │    │                                              │
│   • Linda's iPhone                           │    │   Paired apps:                               │
│   • Living Room HomePod mini                 │    │   • Google Home (Linda's account)            │
│                                              │    │   • Alexa (shop Echo Show)                   │
│   [Reset pairing]    [Advanced ▼]            │    │   • SmartThings (Marcus's Galaxy)            │
│                                              │    │                                              │
│                                              │    │   [Reset commissioning]   [Advanced ▼]       │
└──────────────────────────────────────────────┘    └──────────────────────────────────────────────┘
```

- Zero-config: both enabled by default on first launch. Users who just want local NVR never see this panel unless they scroll to it.
- QR codes are SVG rendered server-side (setup code + HAP service UUID encoded per HAP spec §4.2 / Matter spec §5.1).
- Paired devices / fabrics list surfaces what the user has actually paired — "Linda's iPhone" comes from the HAP client identifier; "Google Home" from the Matter fabric label.
- "Reset pairing" forcibly unpairs every client, regenerates the setup code, and re-advertises. Useful when the user is lost in a broken pair state.
- The "Advanced" disclosure hides per-camera enable toggles (power-user only — the zero-config default is "publish everything").

**Visual direction** adheres to `.impeccable.md` for typography, palette, and spacing. No shortcuts. The QR code cards are the only genuinely new visual element; everything else reuses existing settings-row primitives.

**Onboarding integration:** on first launch, after cameras are discovered and the user has authed at least one, show a subtle toast: "Your cameras are ready in Apple Home and Google Home — visit Settings → Ecosystem to pair." No blocking modal. Users who want the PWA-only experience ignore the toast and never return to the Settings page.

## Tests

All tests are decision-boundary / lifecycle tests against pure Python; no live ecosystem controllers required. Live-controller QA is done in §Verification.

**Phase 1 (HomeKit):**

1. `tests/test_homekit_bridge.py::test_publishes_every_online_camera_at_start` — given 3 online cameras in the store, bridge start advertises 3 accessories.
2. `test_camera_added_event_creates_new_accessory` — fire `camera_found` after start; assert accessory appears, bridge re-advertises.
3. `test_camera_deleted_event_removes_accessory` — fire `camera_deleted`; assert accessory is gone and bridge re-advertises.
4. `test_motion_event_flips_motion_sensor_characteristic` — fire `motion_started`, assert the accessory's motion characteristic value is True; `motion_ended` flips back to False.
5. `test_hksv_service_not_advertised_by_default` — without `SIMPLENVR_HOMEKIT_HKSV=1`, the `RecordingManagement` service is absent from accessory manifest.
6. `test_hksv_service_advertised_when_env_var_set` — regression guard for the env-var gate.
7. `test_bridge_state_persists_across_restart` — write bridge state, destroy instance, re-create with same state dir, assert paired clients list is intact.
8. `test_bridge_survives_camera_store_transient_failure` — if the camera store raises during a `camera_found` event, the bridge logs and continues, not crashes.

**Phase 2 (Matter):** same pattern, swap protocol specifics. Exact test shape depends on the library choice from Phase 2A.

## Verification

Live QA against real ecosystem controllers. Not scripted; checked by human per release.

**Phase 1 (HomeKit) — manual QA matrix:**

| Test | iPhone (iOS 17+) | iPad (iPadOS 17+) | Mac (macOS Sonoma+) | Apple Watch (watchOS 10+) | Apple TV 4K (tvOS 17+) |
|------|:-:|:-:|:-:|:-:|:-:|
| Pair via QR scan                            | ✓ required | ✓ required | ✓ required | N/A (pairs via Home app on phone) | N/A |
| View live feed                              | ✓ | ✓ | ✓ | thumbnail only | ✓ (full screen + screensaver) |
| Motion notification                         | ✓ | ✓ | ✓ | ✓ (glance + complication) | ✓ (toast) |
| Siri "Hey Siri, show me the <camera name>"  | ✓ | ✓ | ✓ | ✓ | ✓ |
| Home widget on lock screen                  | ✓ | ✓ | ✓ | N/A | N/A |
| Multi-user household sharing                | ✓ | ✓ | ✓ | ✓ | ✓ |
| Remote access via HomePod hub               | ✓ | ✓ | ✓ | ✓ | ✓ |
| Reset pairing and re-pair                   | ✓ | ✓ | ✓ | N/A | N/A |

Every cell must hold against at least two real physical devices per column. Failures get a ticket and block the phase completion; none of these are "nice to have."

**Cameras to test against:** Ben's fleet — Eufy 10.0.0.9, Reolinks 10.0.0.13/14, Tapos 10.0.0.46/63. Each must show up in Home app with correct name, stream live, and fire motion notifications. One additional synthetic-stream camera for the MJPEG transcode path (validates the codec-aware recorder + the HAP stream ffmpeg play nicely together).

**Phase 2 (Matter) — same matrix structure across Google Home / Alexa / SmartThings / Apple Home controllers**, plus an explicit "controller recognizes Matter Camera device type AND renders the feed" gate. If a controller recognizes the device but cannot render the feed (expected during the 2026 controller-maturity rollout), log it explicitly and ship with motion notifications working even if live view is controller-patchy.

## Known limitations

- **HAP video streaming is per-session and CPU-bearing.** Every active Apple Home viewer opens a dedicated transcode ffmpeg reading from the go2rtc loopback. At 720p this is 2-5% CPU per session on the Snapdragon X Elite target. A household with 6 viewers simultaneously across iPhone/iPad/Watch/TV could plausibly hit 30% CPU just for HomeKit streaming on top of the always-on recorder + detection workload. Mitigation: cap concurrent HAP streams per camera (two actively decoding clients per camera is a reasonable ceiling; most households will never hit it). Document the cap in Known Limitations on ship.

- **No ecosystem-driven authoring.** The user cannot add a new camera from Apple Home (pair-a-camera-from-Home-app is a different HAP pattern than bridging existing cameras). New cameras come in via SimpleNVR's scanner or manual-add, then appear in Home on next event bus tick. This matches user intuition ("SimpleNVR is where I add cameras") and is consistent with Homebridge's model.

- **First-pair-after-restart latency.** After a SimpleNVR process restart, HAP-python re-reads paired state from disk and re-advertises on mDNS; paired clients auto-reconnect, but the first reconnect can take 30-60s depending on mDNS propagation. Users should not see this unless they're actively looking at Home app during a restart. Not a bug; inherent to mDNS re-advertisement.

- **Matter Camera controller support is a moving target.** As of early 2026, camera support in Apple Home / Google Home Matter controllers is rolling out unevenly. Our Phase 2B ship will recognize-always, render-when-controller-supports. Phase 2A research spike quantifies the current controller matrix; Phase 2C polish folds in any known workarounds.

- **No in-app pairing UX for the user's controller app.** Users pair from Apple Home / Google Home directly; we surface the QR code but don't drive the external app. This is how every other bridge works and is not a defect.

- **HKSV is explicitly not a shipping feature.** If Ben decides later to enable HKSV properly (product decision, not technical), the env-var gate can become a user-facing toggle. Until then, HKSV is dev-only and the UX rule "ecosystem view never replaces SimpleNVR recording" stays enforced.

## Certification / legal caveats

**This section is a flag for Ben, not a recommendation. Resolve with counsel before commercial ship.**

- **Apple MFi program.** Apple's published HAP spec is free to implement, but commercial distribution of a HomeKit accessory technically requires Made-for-iPhone / HomeKit certification ($ per-product fees + NDA + Apple approval). The open-source HAP-python / HAP-NodeJS ecosystem has historically shipped uncertified bridges without Apple enforcement (Homebridge and its $-paid "Hoobs" appliance exist publicly), but the legal status for a paid commercial NVR is ambiguous. Plan: before paid-distribution cutover, Ben's counsel reviews whether our HomeKit integration sits inside Apple's tolerance envelope, and if not whether MFi certification is worth the cost.

- **CSA (Connectivity Standards Alliance) Matter certification.** Matter is royalty-free to *implement* but commercial products that advertise Matter compatibility must be certified by CSA ($ per-product, interop test suite). Open-source implementations can be distributed uncertified; a commercial ship claiming "Matter-compatible" needs certification. Same plan as MFi: resolve posture before paid launch. Using Matter internally without claiming certification is an acceptable interim state.

- **Trademark usage.** "Works with Apple Home," "Works with Google Home," "Matter" are trademarked marks that require usage-approval or certification. In Settings UI we use descriptive phrasing ("Pair with Apple Home," "Pair with Google Home / Matter") rather than certification-claim phrasing until posture resolves.

These aren't implementation blockers for Phase 1 / Phase 2 work on the branch. They're release-gate items before the integrations ship to paying customers. Document them in `docs/trademarks.md` alongside the existing brand-logo nominative fair use rationale.

## Out of scope (deliberate)

- **Separate Alexa Smart Home Skill.** Alexa is *in scope* — it's reached via Phase 2's Matter bridge at zero additional cost. What's out of scope is writing a dedicated "Alexa Smart Home Skill" through Amazon's legacy developer program (which would require Amazon's skill-certification review, an AWS Lambda endpoint for the skill, and separate OAuth setup for every user). Matter renders all that unnecessary since Alexa is a Matter controller.
- **Google Device Access / Nest partnership program.** Matter supersedes it. No paid Google-partnership work.
- **SmartThings Edge driver.** Same — Matter covers it.
- **Home Assistant integration.** Home Assistant consumes Matter / HomeKit natively. Explicit Home Assistant support via HA's custom-integration API would be a separate plan if demand materializes; not scoped here.
- **Two-way audio.** The Microphone service stays silenced in Phase 1. Two-way audio is a separate product decision and out of scope for this plan.
- **Doorbell device type (Apple Home doorbell chime, press-to-unlock).** Some of our cameras have doorbell buttons (not Ben's current fleet). Doorbell as a distinct HomeKit accessory type is a follow-on plan if doorbell cameras enter the fleet.
- **End-user camera add from Home app.** As described in Known Limitations; users add cameras in SimpleNVR.
- **Local network isolation modes (Matter fabric isolation, HomeKit SSID segmentation).** Ship with sensible defaults; bypass these only for advanced-users who know what they're asking for.
- **HKSV productization.** Env-var dev toggle for Phase 1; if it becomes a product, that's a separate plan that resolves the iCloud-duplication UX question head-on.

## UX touch (summary)

**First-run experience:** user installs SimpleNVR, completes camera discovery + auth, sees a one-line toast: "Your cameras are ready in Apple Home and Google Home — Settings → Ecosystem." Users who don't care never see the panel. Users who do get a two-column Settings page with two QR codes and a pair button. One tap on the QR → scan with phone → cameras appear in whichever Home app they use. Zero knobs, zero decisions, zero error messages in the happy path.

**The absence of UX:** when the integration is working, it is *invisible* to the user. No SimpleNVR app on their phone (for ecosystem flows), no custom notification chrome, no "SimpleNVR" watermark on the live feed in Apple Home. The integration's entire purpose is that SimpleNVR's cameras feel native in the user's existing smart-home app. Every design decision optimizes toward that invisibility.

**The one visible moment:** the Settings → Ecosystem panel with the two QR codes. This is the only place SimpleNVR asserts itself to the user in the context of their ecosystem. Make it look nothing like a technical-settings page; make it look like an invitation — "here's how to get your cameras everywhere you already are." Refer to `.impeccable.md` for tone, `docs/product.md` for the non-technical-user test.

## Resume prompt for fresh context

> Read `/Users/benjamincates/Dev/simplenvr/plans/homekit-matter-integration-plan.md` top-to-bottom first. The load-bearing rule is in §Architecture: **integration is additive, never subtractive** — publishing cameras to Apple Home or Google Home never changes local recording, never routes through a vendor cloud, never asks the user to pay for duplicative service. Phase 1 = HomeKit bridge via HAP-python (4-5 weeks MVP, 1-2 weeks polish). Phase 2 = Matter bridge (4-6 weeks MVP after a 1-week library-choice spike, 1-2 weeks polish). Both ship on the `homekit-integration` branch. Pre-shipping blockers (MFi + CSA certification, trademark posture) are flagged in §Certification and must resolve before paid distribution. Work sequence: Phase 0 spike → Phase 1A → 1B → Phase 2A spike → 2B → 2C. Verification is manual QA across a device matrix (§Verification) using Ben's 5 LAN cameras.
