# Camera-Health Labeling & Coverage Plan

> **Picking this up cold?** Read this file top-to-bottom, then skim `CLAUDE.md` and `docs/architecture.md`. The decisions below were made during a live-debugging session on 2026-04-16 after a Tapo at 10.0.0.63 demonstrated a confusing "OFFLINE badge over live video" state. Don't re-litigate the "why not just use the existing offline state" question — section 2 explains.

## 0. State at the moment this plan was written

**Already shipped / pushed to origin/main:**

- simplenvr `45ca194` — recording: expand breaker to count plain-stall kills
- simplenvr `8a75868` — chronic failure: surface "Recording in lower quality" in UI
- tauri-plugin-rtsp-mosaic `11561b0` — macOS: 1Hz timer so timestamp overlay doesn't freeze
- tauri-plugin-rtsp-mosaic `0477b4a` — overlay: add "degraded" amber status badge

**What remains open in the recording-reliability / health area:** this plan.

## 1. Problem

During live testing on 2026-04-16, the Street Tapo (10.0.0.63) showed:
- Tile badge: `OFFLINE` (red)
- Embedded camera OSD timestamp: current time
- MPV-rendered video: live-ish (heavy artifacts but moving)
- Driveway tile (same time): `RECORDING`

The user's reaction was correct: *"OFFLINE on a tile that's clearly showing live video is confusing and wrong."*

Root cause: the recorder's `_staleness_watchdog` classifies any 30s+ of no-bytes-written as `health="offline"`. In practice this conflates two very different states:

1. The camera is **genuinely unreachable** (cable out, IP moved, PSU cycling)
2. The camera is **reachable** (MPV rendering, OSD live) but the **recorder's muxer is stuck** — typical Zone-B or flapping-upstream pathology

Both look identical from the recorder's single-signal perspective (file not growing for 30s), so the UI has no way to tell them apart today.

## 2. Decision (locked — don't re-litigate)

**Distinguish "unable to record" from "offline" using detect-ffmpeg as a witness.**

Detect-ffmpeg consumes from the same go2rtc loopback that MPV consumes. When detect-ffmpeg is successfully decoding frames, the camera is reachable — and MPV is almost certainly rendering. When detect-ffmpeg is stalled or has never produced a frame, we have no evidence the camera is reachable.

Mapping:

| Record-ffmpeg | Detect-ffmpeg | Label |
|---|---|---|
| Writing bytes | — | (none — RECORDING) |
| Stalled 10–30s | — | `"Reconnecting"` |
| Stalled 30s+ | Fresh (frame within SPLIT_BRAIN_DETECT_FRESH_S, 10s) | **`"Unable to record"`** (NEW) |
| Stalled 30s+ | Stalled OR never produced a frame | `"Offline"` |
| Breaker tripped, on sub | — | `"Recording in lower quality"` |

Why this is semantically correct:

- "Unable to record" implies *visibility of the camera* + *recorder broken*. Accurate: we can see the camera via the detect pipeline.
- "Offline" implies *we can't see the camera at all*. Accurate when detect-ffmpeg is also starved: we have no proxy for whether MPV is up.

**Edge case we can't cover with the backend-only approach:** MPV bypasses go2rtc and connects direct-to-camera, detect-ffmpeg is stalled (broken go2rtc loopback), but MPV is happily rendering. The backend would call this "Offline" when the user sees live video. This is rare (depends on how the plugin configures mpv's input URL — needs one-line verification before ruling out). If it becomes a real-world problem, plumb MPV render state from the plugin back to the backend via Tauri IPC (~60 lines, separate pass).

## 3. Implementation — in commit order

### Commit 1 — "recording: distinguish record_failing from offline via detect witness"

**`backend/recording/camera_recorder.py`**

Inside `_staleness_watchdog`, after the existing `"ok"/"stalled"/"offline"` ladder computes `new_health`:

```python
if new_health == "offline":
    # Distinguish "recorder stuck" from "camera unreachable" using
    # detect-ffmpeg as a witness. Detect consumes from the same go2rtc
    # loopback as MPV, so detect freshness strongly implies MPV is
    # rendering — the user sees live video and saying OFFLINE is wrong.
    detect_dt = (
        self._motion_manager.get_last_decoded_frame_dt(self.camera.id)
        if self._motion_manager is not None
        else None
    )
    if detect_dt is not None:
        detect_elapsed_s = (
            datetime.now(timezone.utc) - detect_dt
        ).total_seconds()
        if detect_elapsed_s < SPLIT_BRAIN_DETECT_FRESH_S:
            new_health = "record_failing"
```

Notes:

- Reuses `SPLIT_BRAIN_DETECT_FRESH_S` (10s) as the freshness threshold. Same "detect is alive" signal the split-brain watchdog uses — one source of truth.
- `None` detect_dt means detect is disabled (classifier off) OR detector not yet attached. Fall through to `"offline"` because we have no visibility into the camera at all.
- `_process_monitor`'s offline-bridge emit (line ~1319) continues to emit `"offline"` during respawn gaps. That's correct: during the gap, detect-ffmpeg is ALSO being restarted, so we can't use it as a witness.

**Verification:**

- Unit-test with a mocked motion_manager returning various `(record_stalled_s, detect_elapsed_s)` combinations across the decision boundary. Reuse the fixture shape from `tests/test_split_brain_rule.py`.
- With `SIMPLENVR_CLASSIFIER=off`, confirm the check skips and the camera labels as `"offline"` (existing behavior preserved).

### Commit 2 — "chronic failure UI: surface record_failing as Unable to record"

**`frontend/src/types.ts`**

Add `'record_failing'` to the `CameraHealth` union.

**`frontend/src/hooks/useDiscovery.ts`**

In the `camera_health` WS handler, pass through `'record_failing'` the same way as `'stalled'` / `'offline'`. Preserve `last_frame_at` exactly like other transition events (it's present on this payload, unlike the chronic event).

**`frontend/src/components/NativeCameraTile.tsx`**

In `badgeStatus`: map `health === 'record_failing'` to the native status string `"unable to record"`. Precedence: `record_failing` wins over `degraded` (recorder is actively failing now, more urgent than "resolved on sub"). Offline still wins over `record_failing` (kept as the most-severe "we can't see the camera" state).

**`frontend/src/components/Home.tsx`**

Add an indicator next to `offlineCount` and `degradedCount`:
- `recordFailingCount` — number of cameras with `health === 'record_failing'`
- Label: "`N unable to record`"
- Color: red (same severity as offline)
- Route: Camera Setup (like the other indicators)

Order in the topbar (left to right): offline → unable to record → recording in lower quality. Severity-descending.

**`tauri-plugin-rtsp-mosaic/src/overlay.rs`**

Add:
```rust
/// Status badge background when `status == "unable to record"` — red-orange.
/// Same urgency as offline, but distinguished so macOS can render a label
/// shorter and different hue if desired. Reusing COLOR_BADGE_DEFAULT would
/// be semantically wrong (this is NOT an unknown state, it's a specific
/// known failure). We pick red-orange sitting between amber (degraded)
/// and pure red (offline).
pub const COLOR_BADGE_FAILING: Rgba = [0.93, 0.41, 0.15, 1.0];
```

Add a `"unable to record"` arm in `badge_color_for_status` returning `COLOR_BADGE_FAILING`.

**Note on Windows:** the `set_overlay` call is currently a no-op on Windows (`platform/windows.rs:594-598`). The new status string is harmless there — it just doesn't render. When Windows native overlays ship (section 4.1), the shared color table will Just Work.

### Commit message style

Match existing:
- simplenvr: `recording: <description>`
- plugin: `<area>: <description>`

## 4. What's next in the camera-health area

Listed roughly in priority order. All are deferred from this plan unless otherwise noted.

### 4.1 — Windows native overlays (in progress at the time this plan was written)

**Status:** Ben had asked for this mid-session, I got as far as adding `Win32_System_SystemInformation` to `Cargo.toml` before pivoting. The Cargo.toml line was reverted before commit so the plugin has NO Windows overlay work yet.

**Scope** (est. ~200 lines):
- New child HWNDs per tile: `badge_hwnd` (top-LEFT on Windows — top-right is claimed by fs_hwnd) + `time_hwnd` (bottom-LEFT — bottom-right is claimed by mute_hwnd)
- Label wndproc handling `WM_PAINT` (colored bg from shared table + text via `DrawTextW`) and `WM_TIMER` (`InvalidateRect` for timestamp refresh)
- `register_badge_class` / `register_time_class`
- Update `TileEntry` with two new HWNDs
- Update `position_tile` to move them + add HWND visibility-tracking map so set_overlay-driven visibility is preserved across parent moves
- Implement `set_overlay` properly on `WindowsSurface`
- Drop / `destroy` cleanup

**Dependency add:** `Win32_System_SystemInformation` feature for `GetLocalTime` + `SYSTEMTIME`.

**Design notes already decided:**
- Badge goes top-LEFT (Windows-specific, because fs occupies top-right by existing muscle memory). Document the asymmetry with macOS in a code comment.
- Time goes bottom-LEFT for the same reason (mute occupies bottom-right).
- One label wndproc handles both badge and time; per-hwnd map stores label + color for badge, time wndproc computes `GetLocalTime` inline in `WM_PAINT` (no map needed).
- Skip name + motion for now — ship badge + time only. Parity work for name/motion is later.

### 4.2 — MPV-state plumbing as a second witness

**Scope:** add a callback from `tauri-plugin-rtsp-mosaic` when mpv's render loop has produced a frame; Tauri-IPC the timestamp back; expose as a setter the backend CameraRecorder can read in the watchdog alongside detect-ffmpeg.

**Why defer:** detect-ffmpeg is an adequate proxy for 95% of setups. Only needed if we observe a real case of MPV-bypasses-go2rtc causing wrong labels. Expected ~60 lines across the plugin + backend; rough plan only.

### 4.3 — Fast-fail loop (Path #3) breaker coverage

**Current gap:** if ffmpeg exits rc≠0 before any byte writes (`_last_progress_ts == 0.0`), the watchdog bails early at `camera_recorder.py:821` and only `_process_monitor`'s respawn loop handles it. The new breaker expansion (`45ca194`) doesn't count these restarts, so an RTSP/codec/auth-negotiation loop on a broken main stream never trips chronic-failure.

**Evidence Ben has hit this:** NONE as of 2026-04-16. `health="offline"` during the Tapo observation implies `_last_progress_ts > 0`, which rules out Path #3. This is a theoretical coverage gap, not a real-user problem.

**If pursued:** add breaker call in the `_process_monitor`'s fast-fail branch (lines ~1283-1293), where `fast_fail` is already computed. Reuse `_register_recording_failure_restart`. ~5 line change. Add a test case for the three-fast-fails-trip-breaker boundary.

### 4.4 — codec.py -timeout value judgment call

**Open flag** from the original plan (memory note `project_recording_reliability.md`):

> `codec.py -timeout` left at 30s instead of the plan's 10s. Existing rationale: Tapo/Reolink spiky firmware chains flaps through a 10s budget. Decision not locked in the plan's Decisions table, so it was a judgment call.

Revisit only if a specific failure trace shows `-timeout` as the missed catch. Not driven by user observation as of 2026-04-16.

### 4.5 — Eufy smoke test on live hardware

The original recording-reliability plan called for a 1-2h Eufy smoke test to verify split-brain detection + breaker trip on the camera that motivated the whole effort. Eufy was offline during the 2026-04-16 session. Still pending. Agent can't do this — needs the camera physically present on Ben's network.

### 4.6 — Chronic state "Retry main stream" UX polish (not necessarily health)

Already shipped (commit `8a75868`). Any polish is follow-up based on usage feedback. No planned work.

## 5. Out of scope permanently

- Frontend-side mpv-state inference from per-tile visual signal (too fragile).
- Labeling camera as `"offline"` when the camera is off-network but MPV has a cached frame. Cached frames aren't live; `"offline"` is correct.
- Adaptive thresholds (make `SPLIT_BRAIN_DETECT_FRESH_S` camera-specific). Premature.
- "camera healthy but MOTION disabled" as a distinct state. That's a settings question, not a health question.

## 6. Resume prompt for fresh context

Copy-paste the following into a new Claude Code session after `/clear` to pick up this work with full context:

> Read `/Users/benjamincates/Dev/simplenvr/CLAUDE.md`, then the memory index at `~/.claude/projects/-Users-benjamincates-Dev-simplenvr/memory/MEMORY.md` and specifically these four entries (read all of them — they form the complete history of the stream-health story):
> - `project_recording_reliability.md` — the original Zone-B split-brain plan (shipped 2026-04-16 as `530f2e3` / `35b4570` / `62a0704` / `f42cdfd`)
> - `project_chronic_recording_ui.md` — frontend wiring of the new chronic-failure state (shipped 2026-04-16 as `8a75868`)
> - `project_detection_v2.md` and `project_detection_v2_qa.md` — context for how detect-ffmpeg relates to the recorder; important because `record_failing` uses detect-ffmpeg as its witness
> - `feedback_review_triage.md` — reminder to trace any change through bare-Python, `cargo tauri dev`, AND the PyInstaller bundle
>
> Then read the two plans, in this order:
> 1. `/Users/benjamincates/Dev/simplenvr/plans/recording-reliability-plan.md` — the original split-brain plan. **DONE** (all 3 commits + the post-hoc predicate-extraction + the plain-stall breaker expansion). Read it for background on Zone-A vs Zone-B vs Zone-C and why detect-ffmpeg's `_last_frame_decoded_at` is the witness field. Don't re-litigate its Decisions table.
> 2. `/Users/benjamincates/Dev/simplenvr/plans/health-labeling-plan.md` (this file) top-to-bottom. This is the active plan. Don't re-litigate §2 (Decision).
>
> **What shipped 2026-04-16 that you should understand before editing:**
>
> In `simplenvr`:
> - `530f2e3` — stop using stderr as liveness, tighten stall thresholds
> - `35b4570` — cross-pipeline split-brain detection (detect-ffmpeg → recorder witness wiring)
> - `62a0704` — circuit breaker + sub-stream fallback (chronic_recording_failure event)
> - `f42cdfd` — extract watchdog predicates + unit tests (the `should_trip_split_brain` + `prune_and_check_breaker_trip` pure helpers + tests/test_split_brain_rule.py + tests/test_circuit_breaker.py)
> - `8a75868` — frontend UI for chronic_recording_failure (DEGRADED badge + topbar "N recording in lower quality" + per-camera retry callout)
> - `45ca194` — breaker now counts plain-stall kills as well as split-brain kills (closes the Zone-C-adjacent coverage hole; motivated by the 2026-04-16 Tapo observation)
>
> In `tauri-plugin-rtsp-mosaic`:
> - `0477b4a` — add "degraded" amber badge color to the shared overlay table
> - `11561b0` — 1Hz NSTimer refresh so the macOS timestamp overlay doesn't freeze
>
> **State at session start:**
> - `simplenvr` main at `45ca194`, pushed to origin.
> - `tauri-plugin-rtsp-mosaic` main at `11561b0`, pushed to origin.
> - Both repos clean except for untracked files (ignore `.DS_Store`, the two `*-plan.md` files).
> - No uncommitted code anywhere.
>
> **The collective story so far:** Recording has three failure zones from the original plan (Zone A = camera→go2rtc path broken; Zone B = go2rtc→record-ffmpeg broken but detect-ffmpeg fine; Zone C = both consumers stalled from the same starved upstream). Zone A + B + C restarts all now count toward the 3-in-10-min breaker; once tripped, the camera auto-flips to sub-stream and the UI shows "Recording in lower quality" with a Retry button in Camera Setup. **What's still wrong in the UI:** during the 30–300s window between "recorder stops writing bytes" and "breaker trips," the badge says OFFLINE even when MPV is clearly rendering live video. That's the label the user sees and it's misleading. This plan fixes it.
>
> **Task for this session:** implement §3 — two commits in order:
> 1. Backend: distinguish `record_failing` from `offline` via detect-ffmpeg witness. Include a unit test using the same predicate-test pattern as `tests/test_split_brain_rule.py`. ~15 lines of code, ~40 lines of test.
> 2. Frontend + plugin: surface `record_failing` as "Unable to record" in UI. Simplenvr: `frontend/src/types.ts`, `frontend/src/hooks/useDiscovery.ts`, `frontend/src/components/NativeCameraTile.tsx`, `frontend/src/components/Home.tsx`. Plugin: `src/overlay.rs`.
>
> **When done:** push both repos. **Don't** start §4 items (Windows overlays, MPV plumbing, Path #3 fast-fail, codec.py -timeout, Eufy smoke test) unless Ben asks — this session is scoped to the label fix. §4.1 in particular is half-planned and scope-ready; Ben may ask for it next.
>
> Start by reading `backend/recording/camera_recorder.py::_staleness_watchdog` in full to confirm the ladder hasn't shifted, then look at the `should_trip_split_brain` pure helper near the top of that file — its input shape is the template for how to test the new check. Do Commit 1 + test + verify test passes before touching the frontend.
