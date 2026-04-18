# Phase 1A — HomeKit Bridge MVP (execution plan)

> **Picking this up cold?** Read this file top-to-bottom, then skim `plans/homekit-matter-integration-plan.md` (strategy doc — Architecture, Phase 1, Pseudocode are still authoritative for *shape*; this file is authoritative for *execution*). Also read the three Phase 0 spike reports under `plans/phase-0-spike/`. The decisions below are locked — do not re-litigate.

## Context

Phase 1A ships the first live HomeKit integration: SimpleNVR advertises itself to Apple Home as a bridge, every online camera appears as a child accessory, motion events fire iOS / Watch push notifications, and tapping a camera in the Home app plays live video. Integration-is-additive — the local recorder, the event bus, and every existing subsystem operate identically whether the bridge is up, down, unpaired, or mid-pair. HKSV stays off.

This phase is **execution-grade**. The strategy doc answered "what" and "why"; this doc answers "in what order, with what exit criteria, measured how."

**Scope**: HomeKit only. Matter is Phase 2. Per-camera enable toggles, user-facing HKSV, and accessory-rename polish are Phase 1B.

## Phase 0 findings baked in

Three load-bearing conclusions from the spike reports (`hap-python-recon.md`, `mfi-risk-brief.md`, `matter-library-evaluation.md`) shape this phase:

1. **HAP-python's bundled ffmpeg command is a macOS webcam demo.** Do not use it. Port Home Assistant's `VIDEO_OUTPUT` SRTP template verbatim from `homeassistant/components/homekit/type_cameras.py`. HA runs it at scale against every RTSP-capable camera in the world and has solved every edge case — byte-for-byte copy, substitute SimpleNVR's session params.

2. **HomeKit audio requires `homekit-audio-proxy==1.2.1`.** ffmpeg's Opus RTP clock is hardcoded at 48 kHz; HAP negotiates 16/24 kHz. Without the proxy: video works, audio is silent. This is a hard pin, added to `requirements.txt` as part of this phase.

3. **`Camera.get_snapshot()` default returns a placeholder.** If we do not override it, every camera in Apple Home's Rooms view shows the same generic HAP-python JPEG instead of a live frame. UX hit. Override is a single ffmpeg single-frame grab from the go2rtc loopback; small code, meaningful UX.

Two non-technical findings:

4. **No Apple Developer Program enrollment is required for HomeKit.** HAP-python is a clean-room reimplementation; no Apple SDK is touched. The $99/yr Developer Program question is about macOS notarization and is orthogonal to this phase.

5. **Trademark copy matters more than cert posture.** Safe phrasing: "Pair in the Home app." Red: the "Works with Apple Home" badge, "HomeKit" in product-name position, "Apple Home Certified" claims. Applies to the Settings panel, onboarding toasts, and any marketing copy referencing the feature.

## Decisions (locked — do not re-litigate)

| Decision | Value | Why |
|---|---|---|
| HAP library | `HAP-python[qrcode]==5.0.0` | Only maintained Python HAP lib; HA co-maintains; ships to millions of installs |
| Topology | Bridge mode (one bridge + N child accessories) | Dynamic add/remove without re-pair; strategy doc §Phase 1 |
| RTSP source per session | go2rtc loopback (`rtsp://127.0.0.1:58554/<camera_id>`) | Single RTSP client per camera matches recorder architecture |
| ffmpeg template | Port HA's `VIDEO_OUTPUT` substitution template | Field-proven; don't rewrite |
| Audio proxy | `homekit-audio-proxy==1.2.1` pinned | Fixes Opus clock-rate mismatch; no alternative |
| Snapshot source | ffmpeg single-frame grab from go2rtc loopback | Matches existing infra; no new deps |
| Persistence | `DATA_DIR/ecosystem/homekit/accessory.state` | Per-protocol file, not DB; matches HAP-python expectations |
| Motion feed | `motion_started` / `motion_ended` from detection-v2 | FP filter already applied; never republish raw D-FINE hits |
| HKSV | `SIMPLENVR_HOMEKIT_HKSV=1` env-gated, default off | Integration-is-additive; iCloud duplicate-recording trap |
| Microphone service | Silenced placeholder | Two-way audio is out of scope for 1A |
| Enable flag | `settings` key `homekit_enabled` default `'true'` | Zero-config; user can disable without uninstalling |
| Concurrent stream cap | 2 per camera | Strategy doc §Known Limitations; prevents CPU storm |
| Test-camera list in M1 | **Enumerate live from camera store** | No hardcoded cameras — production-shape from day one |
| Trademark copy | "Pair in the Home app" | Per Track 2 findings; never "Works with Apple Home" |
| iOS device fleet for QA | iPhone + iPad + HomePod mini + Apple TV 4K | Ben's paired devices; matches strategy doc matrix |

## Architecture snapshot

```
backend/main.py lifespan:
  start go2rtc client
  start scanner
  start RecordingManager
  start MotionManager  ←─── motion_started / motion_ended publisher
  start AudioManager
  start HomeKitBridge  ←─── NEW: this phase
        │
        ├─ subscribes: camera_found / camera_updated / camera_deleted
        ├─ subscribes: motion_started / motion_ended
        └─ owns: accessory.state, per-session ffmpeg subprocesses
```

Bridge runs as an asyncio task inside the same Python sidecar. No extra process. mDNS advertisement is HAP-python's built-in `zeroconf` integration.

**Lifecycle invariant** (matches strategy doc): a crashed / unpaired / stopped bridge never changes whether the recorder writes segments. Tests enforce this.

## Milestones

Each milestone has a **goal**, **files**, and **exit criteria** (binary — tick or don't). The phase is complete when M7's exit criteria are all green against real Apple devices.

### M1 — Bridge skeleton + live pair across the live camera store

**Goal**: Bridge advertises N accessories (one per online camera from the live store) behind a single QR. Ben pairs from iPhone. Every camera appears in Apple Home. Tapping one of them plays live video.

This milestone absorbs Track 1 HAP-python validation — no throwaway spike.

**Files added**:
- `backend/ecosystem/__init__.py`
- `backend/ecosystem/homekit/__init__.py`
- `backend/ecosystem/homekit/bridge.py` — `HomeKitBridge` lifecycle + event-bus wiring (bare minimum here; expanded in M2/M3)
- `backend/ecosystem/homekit/camera_accessory.py` — `CameraAccessory` subclassing `pyhap.camera.Camera`
- `backend/ecosystem/homekit/stream_ffmpeg.py` — per-session ffmpeg spawn/teardown, ported from HA's `VIDEO_OUTPUT`
- `backend/ecosystem/homekit/state.py` — `DATA_DIR/ecosystem/homekit/` path resolution + state init

**Files modified**:
- `backend/main.py` — add `HomeKitBridge` to lifespan after `AudioManager`; graceful-shutdown hook before Python exits
- `backend/requirements.txt` — add `HAP-python[qrcode]==5.0.0` and `homekit-audio-proxy==1.2.1`

**Exit criteria**:
- [ ] Running SimpleNVR via `cargo tauri dev` shows no startup errors from the bridge
- [ ] `dns-sd -B _hap._tcp local.` (or `avahi-browse -r _hap._tcp` on Linux) shows the SimpleNVR bridge within 5s of process start
- [ ] QR code renders in server logs (or temp UI; real Settings surface lands in M5) and scans correctly from the iPhone Camera app
- [ ] Ben completes pair from Apple Home → bridge appears as "SimpleNVR" → all online cameras appear as child tiles
- [ ] Tapping any child camera in Apple Home renders live video within **5 seconds** of first tap
- [ ] `ps` during live view shows one extra ffmpeg child process per active viewer; it exits within **2s** of closing the Apple Home tile
- [ ] CPU measured on the Mac dev host per active 720p viewer and logged in commit message (target ≤ 5% per session; dev-Mac baseline proxies the Snapdragon X Elite target)
- [ ] Recorder continues writing segments uninterrupted throughout pair + live-view (integration-is-additive witness)

### M2 — Live event-bus integration (camera add / remove / update)

**Goal**: Adding or removing a camera in the SimpleNVR UI reflects in Apple Home without restarting the bridge.

**Files modified**:
- `backend/ecosystem/homekit/bridge.py` — subscribe to `camera_found`, `camera_deleted`, `camera_updated`

**Exit criteria**:
- [ ] Discovering a new camera in SimpleNVR UI → it appears in Apple Home within **10s** (no restart, no re-pair)
- [ ] Deleting a camera in SimpleNVR UI → it disappears from Apple Home
- [ ] Renaming a camera in SimpleNVR UI → the new name surfaces in Apple Home (may require user-side "refresh" per HAP spec; document exact behavior observed)
- [ ] Bridge survives a synthetic `camera_found` event that raises during `get_online()` — logs the error, continues serving existing accessories (never crashes the sidecar)

### M3 — Motion sensor characteristic

**Goal**: Motion events from detection-v2 fire iOS / Watch push notifications.

**Files modified**:
- `backend/ecosystem/homekit/camera_accessory.py` — add `MotionSensor` service + `MotionDetected` characteristic
- `backend/ecosystem/homekit/bridge.py` — subscribe to `motion_started` / `motion_ended`, fan out to correct accessory

**Exit criteria**:
- [ ] Waving at a camera's FOV causes Apple Home's motion tile to flip to true within **5s**
- [ ] When the motion track closes, the motion tile flips back to false within **5s**
- [ ] iPhone lock-screen push notification fires for at least one motion event during QA
- [ ] Apple Watch glance / complication reflects motion state
- [ ] No notifications for raw D-FINE hits that the 8-layer FP filter rejected (prove we're subscribing to approved-track events, not raw detections)

### M4 — State persistence + restart

**Goal**: Paired state survives process restart. The user pairs once and never has to scan the QR again unless they explicitly reset.

**Files modified**:
- `backend/ecosystem/homekit/state.py` — fsync paired-client keys + bridge keys on every write
- `backend/ecosystem/homekit/bridge.py` — load state at start; re-advertise with same identity; re-emit motion state on reconnect

**Exit criteria**:
- [ ] After M1 pair succeeds, `kill -TERM` the SimpleNVR process → restart → bridge re-advertises within **30s** → iPhone Apple Home auto-reconnects without prompt
- [ ] Per-camera live view works on first reconnect attempt (not just on second)
- [ ] Deleting `DATA_DIR/ecosystem/homekit/accessory.state` → restart → clean-slate QR (proves reset path)
- [ ] No leaked paired clients after 10 pair / reset / re-pair cycles (file integrity under churn)

### M5 — Settings UI + API endpoints

**Goal**: The user discovers the feature, scans the QR, manages pairing from SimpleNVR's Settings page. Copy is trademark-safe.

**Files added**:
- `frontend/src/components/Settings/EcosystemPanel.tsx` — left-column HomeKit card (Matter right-column card is a placeholder for Phase 2)
- `backend/api/ecosystem.py` — `GET /api/ecosystem/homekit/status`, `POST /api/ecosystem/homekit/reset-pairing`

**Files modified**:
- Settings page composition — include `<EcosystemPanel />`
- `backend/db.py` — add `homekit_enabled` settings key with default `'true'`

**Exit criteria**:
- [ ] Settings page renders the HomeKit QR at high contrast against `.impeccable.md` palette
- [ ] QR is readable from ~30cm with a hand-held iPhone (SVG resolution + contrast validated)
- [ ] Setup code displayed in the same card, copyable
- [ ] Paired-clients list shows real device labels from HAP client identifiers ("Linda's iPhone")
- [ ] "Reset pairing" button clears all paired clients, regenerates QR, re-advertises
- [ ] All visible copy uses "Pair in the Home app" / "Compatible with Apple Home" phrasing — no "Works with Apple Home" badge, no "HomeKit" in product-name position
- [ ] Zero-config: a fresh SimpleNVR install with `homekit_enabled='true'` surfaces the QR on first Settings visit without further setup

### M6 — Snapshot override + audio proxy

**Goal**: iOS preview thumbnails are live frames, not HAP-python's placeholder JPEG. HomeKit audio plays through iPhone / HomePod mini.

**Files added**:
- `backend/ecosystem/homekit/snapshot.py` — single-frame ffmpeg grab from go2rtc loopback, 640×360 JPEG, cached briefly to avoid thundering-herd on Rooms-view refresh

**Files modified**:
- `backend/ecosystem/homekit/camera_accessory.py` — override `get_snapshot(image_size)` to call `snapshot.py`
- `backend/ecosystem/homekit/stream_ffmpeg.py` — route audio through `homekit-audio-proxy` localhost port

**Exit criteria**:
- [ ] Every camera in Apple Home Rooms view shows a real current frame, not a placeholder
- [ ] Pull-to-refresh in Apple Home updates the thumbnail within **2s**
- [ ] Snapshot ffmpeg invocations complete within **1s** (watchdog log line; prevents hangs)
- [ ] Live stream audio plays through iPhone speaker and through a paired HomePod mini
- [ ] Silencing at the camera source results in silence at Apple Home (no stuck-noise regression)

### M7 — Tests + PyInstaller bundle + full QA matrix

**Goal**: Phase 1A is ready to merge to `main` and ship in the next bundled release.

**Files added**:
- `tests/test_homekit_bridge.py` — the 8 decision-boundary tests from strategy doc §Tests:
  1. `test_publishes_every_online_camera_at_start`
  2. `test_camera_added_event_creates_new_accessory`
  3. `test_camera_deleted_event_removes_accessory`
  4. `test_motion_event_flips_motion_sensor_characteristic`
  5. `test_hksv_service_not_advertised_by_default`
  6. `test_hksv_service_advertised_when_env_var_set`
  7. `test_bridge_state_persists_across_restart`
  8. `test_bridge_survives_camera_store_transient_failure`

**Files modified**:
- `scripts/bundle_python.sh` — ensure `HAP-python`, `homekit-audio-proxy`, and their transitive deps land in the PyInstaller bundle (may need `--collect-all pyhap` + `--collect-all homekit_audio_proxy` hidden-imports)
- `scripts/bundle_python.ps1` — same on Windows ARM64
- `docs/architecture.md` — add "Ecosystem publication" subsystem subsection pointing at this phase
- `backend/main.spec` — PyInstaller spec adjustments if needed

**Exit criteria**:
- [ ] All 8 tests pass in `.venv`
- [ ] `cargo tauri dev` boots SimpleNVR with the bridge green (not just bare `python -m backend.main`)
- [ ] Fresh bundled `.app` (macOS) unzips, runs, advertises over mDNS, pairs from a fresh Apple ID, streams live video, fires motion notifications — end-to-end without dev-machine artifacts
- [ ] Fresh bundled `.exe` (Windows ARM64) same matrix (run on Snapdragon X Elite hardware, not emulation)
- [ ] Apple Home QA matrix from strategy doc §Verification green across Ben's fleet: iPhone + iPad + Apple Watch + Apple TV 4K + HomePod mini
- [ ] Concurrent stream cap of 2 per camera is enforced under load (3rd simultaneous viewer sees degraded / refused behavior; document exact response)
- [ ] Recorder continues writing segments throughout QA pass on at least one camera (integration-is-additive witness at bundle scale)

## Dependencies to add

```
# backend/requirements.txt additions
HAP-python[qrcode]==5.0.0       # HAP bridge + QR code rendering
homekit-audio-proxy==1.2.1       # Opus clock-rate proxy — audio will be silent without this
```

Both pins at exact versions. Tracked by Home Assistant; their bump cadence is our bump cadence. Pinning avoids surprise regressions.

## Bundle concerns

**PyInstaller hidden imports.** HAP-python uses runtime imports for its zeroconf + cryptography layers. Verify:

```bash
pyinstaller --collect-all pyhap \
            --collect-all homekit_audio_proxy \
            --hidden-import zeroconf._handlers.answers \
            ...
```

Add the verify step to `scripts/bundle_python.sh` / `.ps1`: after bundle, run `python -c "from pyhap.accessory_driver import AccessoryDriver; AccessoryDriver(port=0)"` against the bundled interpreter. Fail-fast if imports are missing.

**mDNS on macOS sandboxed builds.** Bonjour works for unsigned local dev; if code signing / sandboxing tightens later, confirm the `.entitlements` file permits multicast. Not a blocker for 1A; note in phase retrospective.

**Windows firewall prompt.** First HAP advertisement on Windows pops a firewall dialog for TCP 51826. Document the expected user-visible behavior; consider pre-registering the rule during MSI install.

## Tests

Covered in M7. All decision-boundary, no live Apple controller required. Live-controller validation is the QA matrix at the end of M7 — not automated.

## Trademark-safe copy reference

| ✅ Use | ❌ Avoid |
|---|---|
| "Pair in the Home app" | "Works with Apple Home" (badge-protected) |
| "Compatible with Apple Home" | "Apple Home Certified" (implies MFi) |
| "Scan this QR code from the Home app on iPhone" | "HomeKit" in product-name position |
| "Your cameras in Apple Home" | Any Apple logos or badge assets |
| "Apple Home" as descriptor | "iPhone" + Apple logo lockups |

Per Track 2 / `mfi-risk-brief.md`. Applies to EcosystemPanel, onboarding toast, release notes, marketing page, App Store / Microsoft Store listings.

## Out of scope for 1A

- **HKSV productization** — env-var dev toggle only. User-facing HKSV is a separate plan that has to resolve the iCloud-duplicate-recording UX question.
- **Per-camera enable toggles** — Phase 1B polish.
- **Two-way audio (Microphone service)** — Phase 1B or later.
- **Doorbell device type** — no doorbell cameras in Ben's fleet; future plan.
- **In-app pair-from-Home-app flow** — users add cameras in SimpleNVR, not from Apple Home. Matches Homebridge's model.
- **Multi-user Home sharing UX** — Apple Home handles this; nothing for us to build.
- **Matter** — Phase 2.

## Risks & mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| HA's `VIDEO_OUTPUT` template substitution logic is more involved than a format-string copy | Medium | Port HA's full substitution helper, not just the string; cite commit hash in code comment |
| `homekit-audio-proxy` package missing from PyInstaller bundle | High if not explicit | M7 adds `--collect-all` for both packages + post-bundle import test |
| Concurrent stream-count breach under a household of 6 active viewers | Low at current fleet size | Cap enforced in `CameraAccessory`; document limitation; plan a metrics view for Phase 1B |
| iOS 19 / 2026 OS releases break pairing mid-phase | Low (HA absorbs this cost) | QA matrix includes latest released iOS / iPadOS / tvOS / watchOS; bump HAP-python pin if HA bumps it |
| go2rtc loopback URL schema changes (it hasn't, but) | Very low | `go2rtc_client.loopback_url_for` already abstracts it; bridge uses the helper, not the URL shape |
| PyInstaller fails to bundle `cryptography` native extension on Windows ARM64 | Medium | `cryptography` is already in `requirements.txt` for other deps; Windows ARM64 wheels are published; if blocked, cache a vendor wheel per `vendor/cv2-wheels/` precedent |
| Fresh-context sessions re-litigate HKSV decision | Medium (locked) | Decisions table top of file; refuse to touch HKSV-on-by-default outside a separate plan |

## Verification

The full cross-device QA matrix is in strategy doc §Verification. M7 validates against it; not duplicated here (per `feedback_smart_dry` — single source of truth).

## Phase-close definition

Phase 1A is done when:

1. All 7 milestones' exit criteria are green.
2. Tests pass in CI (or the manual equivalent — run in `.venv` before merge).
3. QA matrix green across Ben's Apple device fleet.
4. `homekit-integration` branch is merged to `main` with a single squash commit or coherent linear history.
5. A user session report is captured: install fresh, pair, live-view, motion notification, restart, re-pair-not-needed. Screenshots attached.

**Phase 1B follow-on plan** (not this doc): per-camera enable, accessory-rename live-sync, HKSV UX decision, household-of-6-viewers polish, two-way audio gate.
