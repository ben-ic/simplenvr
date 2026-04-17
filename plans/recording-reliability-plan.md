# Recording Reliability — Stream-Health Fix Plan

> **Picking this up cold?** Read this file top-to-bottom, then skim `CLAUDE.md` and `docs/architecture.md`. The decisions below were made across a multi-turn research + architect-review session and should not be re-litigated. If a decision feels wrong, check the **Decisions** table's "why" column first — it probably addresses your objection.

## Problem

A Eufy camera was visibly live in the mpv mosaic tile while the recorder wrote zero bytes for an extended period. Silent data loss.

Root cause: `backend/recording/camera_recorder.py:600-602` updates `_last_progress_ts` on every ffmpeg stderr line. ffmpeg emits continuous stderr warnings (`Non-monotonic DTS`, `RTP: missed N packets`) while its muxer is broken and writing nothing. The stderr heartbeat keeps the watchdog happy; the 120s stall threshold never fires.

This is a **Zone B** failure: the camera → go2rtc path is healthy (proven by mpv receiving frames from the same go2rtc producer in parallel), but the go2rtc → record-ffmpeg consumer is silently broken.

## Research verdict (don't re-investigate)

12 open-source NVRs surveyed (Frigate, Viseron, Kerberos.io, motion, MotionPlus, Bluecherry, Home Assistant stream, ZoneMinder, Shinobi, MediaMTX, go2rtc, iSpy).

- **Unanimous**: every project except SimpleNVR treats ffmpeg stderr as diagnostic, not liveness. Signals used: decoded-frame counter on stdout pipe (Frigate, Viseron), RTP packet counter (Kerberos), `av_read_frame` return codes (Bluecherry, HA), segment file growth (Frigate record-role).
- **Nobody uses FPS-rate thresholding** — false positives on B-frames, long GOPs, bursty cameras.
- **Nobody does cross-pipeline witness.** Frigate has both detect and record ffmpeg per camera but doesn't cross-check. The cross-witness pattern below is SimpleNVR-specific, enabled by the go2rtc-loopback architecture.

## Decisions (locked — don't re-litigate)

| Decision | Value | Why |
|---|---|---|
| Remove stderr as liveness signal | File-growth only | Unanimous across 12 surveyed projects |
| Stall threshold | 30s (was 120s) | Matches motion/HA baseline; 120s was tuned for stderr-OR semantics that no longer exist |
| Split-brain bytes-stale threshold | 15s | Catches Zone B 15s earlier than file-growth alone |
| Split-brain detect-fresh gate | 10s | ~20 frames at 2fps = ample jitter margin |
| Startup grace period | 15s after recorder spawn | Prevents false positives during concurrent recorder+detect spawn (detect-ffmpeg needs 2-3s for first frame) |
| Circuit breaker | 3 split-brain restarts in 10min | Simple sliding-window, prevents restart storm |
| Circuit breaker state location | Per-`CameraRecorder`, not `RecordingManager` | Auto-freed on camera removal; no manager cleanup code |
| Sub-stream fallback | **Option (i)**: permanent switch with UI affordance to retry main | Zero-knob audience; adaptive probation would flicker amber on permanently-broken main streams |
| `_last_frame_decoded_at` field | **New field**, `datetime \| None`, on `DetectFfmpegSource` | Existing `_last_frame_at` on `MotionDetector:308` is emission-time (set only in `_observe_emittable:902`); reusing it would suppress split-brain on empty-scene cameras — verified correctness issue |
| Timestamp type | `datetime`, not monotonic float | Matches existing convention; no parallel clocks |
| WHERE guard on sub-stream DB write | `WHERE recording_stream_override IS NULL OR = 'main'` | Prevents breaker clobbering user's explicit "main" choice |
| End-of-segment sanity check | Behind `validate_segments` flag, **default off** | Defer to future storage-diagnostics work; low priority |
| Zone C (go2rtc frozen relay) detection | Skip v1 | Rare, no surveyed project does it; graceful degradation to file-growth watchdog is fine (detect-ffmpeg also stalls, split-brain gate's "detect fresh" becomes false, falls through) |
| FPS-rate thresholding | Skip | Zero projects use it; false positives unacceptable |

## Architecture snapshot

```
  camera ──(A)──► go2rtc producer ──(B)──► { record-ffmpeg, detect-ffmpeg, mpv }
           │                          │
      pre-go2rtc               post-go2rtc (per-consumer) — Zone B is our target
```

Two Python managers own per-camera subprocesses:

- `RecordingManager` (`backend/recording/manager.py`) → `CameraRecorder` → `record-ffmpeg` (`-c copy` → MP4 segments)
- `MotionManager` (`backend/motion/manager.py`) → `MotionDetector` → `DetectFfmpegSource` → `detect-ffmpeg` (raw YUV @ 2fps)

Today the managers don't talk. This plan adds a **one-directional read-only coupling**: `CameraRecorder` queries `MotionManager` for a per-camera "is detect-ffmpeg receiving decoded frames?" signal, which serves as a witness that the go2rtc source is alive.

**Startup order gotcha**: `backend/main.py:127` constructs `RecordingManager` *before* `MotionManager` at line 175, and `MotionManager` already takes the recorder as a parameter. We cannot flip this. Use a **post-init setter** `recorder.attach_motion_manager(motion)` called immediately after `MotionManager` construction.

---

## Implementation, in commit order

### Commit 1 — "recording: stop using stderr as liveness, tighten stall thresholds"

**Scope**: mandatory fix. Small, trivially verifiable. Ships the core safety alone so the Eufy bug is fixed even if commits 2–3 slip.

**`backend/recording/camera_recorder.py`**:

1. **DELETE** lines 600-602 (the stderr heartbeat update and its comment):
   ```python
   # Any stderr line means FFmpeg is alive and talking —
   # the watchdog treats this as a progress heartbeat.
   self._last_progress_ts = time.monotonic()
   ```

2. **REWRITE** the file-level docstring comment at lines 54-66. It currently justifies the OR-logic that we're removing and specifically cites Eufy at 10.0.0.9 as the reason for the stderr heartbeat — that reasoning is inverted by this fix. Replace with a short comment explaining the **Zone B failure mode**: "File-growth is the sole liveness signal. stderr is kept only for diagnostics (tail buffer for post-mortem on unexpected exit, segment-open regex parsing). This catches the pathology where ffmpeg keeps running and emitting stderr warnings but its muxer produces zero bytes — observed on Eufy cameras with fragmented-MP4 muxer state corruption."

3. **REWRITE** the comment block at lines 647-651 inside `_staleness_watchdog` that calls file-growth the "secondary" signal — it's now primary/sole. Short rewrite: "Sole liveness signal: segment file size growth. stderr chatter was previously treated as a heartbeat but that allowed the Zone B split-brain pathology — ffmpeg alive and talking but muxer writing zero bytes."

4. **UPDATE** constants at/near line 67:
   ```python
   STALE_FRAME_THRESHOLD_S = 30.0        # was 120.0
   HEALTH_STALLED_THRESHOLD_S = 10.0     # was 15.0
   HEALTH_OFFLINE_THRESHOLD_S = 30.0     # was 60.0
   ```

5. **VERIFY** during this commit: grep the backend for `STALE_FRAME_THRESHOLD_S`, `HEALTH_STALLED_THRESHOLD_S`, `HEALTH_OFFLINE_THRESHOLD_S`, `_last_progress_ts`. The constants should only be read inside `camera_recorder.py`. `_last_progress_ts` should be updated only by the file-growth check (line ~659 after fix), read by the watchdog, and reset on restart. If any other updater exists, investigate before proceeding.

**`backend/recording/codec.py`** (investigate, may or may not need a change):

6. Check whether the ffmpeg invocation for the record-role sets `-timeout` (socket I/O timeout in microseconds). Frigate sets it to `10_000_000` (10s) on RTSP inputs as a belt-and-suspenders libav-layer defense. If SimpleNVR doesn't, add it. This is independent of the watchdog and complements it (catches cases where the RTSP TCP socket hangs before the watchdog's 30s threshold). Keep the value at 10s — matches Frigate, leaves room for slow cameras.

---

### Commit 2 — "recording: cross-pipeline split-brain detection"

**Scope**: the novel extension. Makes record-ffmpeg use detect-ffmpeg's frame counter as a witness to go2rtc health.

**`backend/detect_frames/ffmpeg_source.py`**:

1. Add to `__init__`:
   ```python
   self._last_frame_decoded_at: datetime | None = None
   ```
   Import `datetime, timezone` from `datetime` if not already present.

2. In `_read_frames_loop` immediately after the successful `asyncio.wait_for(stdout.readexactly(...))` at ~line 386 (inside the `try` block, after `buf` is assigned):
   ```python
   self._last_frame_decoded_at = datetime.now(timezone.utc)
   ```
   Update on EVERY successful read. Do not couple to the emittable-track path — that would miss cameras with empty scenes.

3. Expose as a property:
   ```python
   @property
   def last_frame_decoded_at(self) -> datetime | None:
       return self._last_frame_decoded_at
   ```

**`backend/motion/detector.py`**:

4. Add pass-through property on `MotionDetector`:
   ```python
   @property
   def last_frame_decoded_at(self) -> datetime | None:
       return self._source.last_frame_decoded_at if self._source else None
   ```
   Find the exact `_source` attribute name by inspection; adapt.

**`backend/motion/manager.py`**:

5. Add public method on `MotionManager`:
   ```python
   def get_last_decoded_frame_dt(self, camera_id: str) -> datetime | None:
       """Return the timestamp of the last decoded frame from detect-ffmpeg
       for this camera, or None if detection is disabled / detector absent /
       no frames decoded yet. Used by CameraRecorder's watchdog to detect
       split-brain (go2rtc source alive but record-ffmpeg silently broken)."""
       detector = self.detectors.get(camera_id)
       if detector is None:
           return None
       return detector.last_frame_decoded_at
   ```

6. **Verify `SIMPLENVR_CLASSIFIER=off` behavior.** Trace what happens when the classifier is disabled at startup: does `MotionManager` run with an empty `detectors` dict, or does the whole manager no-op? The `get_last_decoded_frame_dt` method must return `None` in either case without raising. If the classifier is toggled off at *runtime* (not just at startup), verify `self.detectors` gets cleared — stale entries would false-positive-gate split-brain.

**`backend/recording/camera_recorder.py`**:

7. Add to `__init__`:
   ```python
   self._motion_manager: "MotionManager | None" = None
   self._recorder_started_at: float = 0.0
   ```
   File already has `from __future__ import annotations` (line 27), so string forward-refs aren't required.

8. Add a post-init setter (can't use constructor injection — see startup-order gotcha):
   ```python
   def attach_motion_manager(self, motion_manager: "MotionManager") -> None:
       """Wire the cross-pipeline witness. Called by main.py after
       MotionManager is constructed. No-op if detection is disabled."""
       self._motion_manager = motion_manager
   ```

9. Set `self._recorder_started_at = time.monotonic()` inside `_spawn` (line 313) just before or after the `await spawn_proc(...)` call. This starts the grace-period clock.

10. Inside `_staleness_watchdog` at line 640, **after** computing `elapsed = time.monotonic() - self._last_progress_ts` and **before** the existing stall-termination check at line 709, add the split-brain rule:

    ```python
    STARTUP_GRACE_S = 15.0
    SPLIT_BRAIN_BYTES_THRESHOLD_S = 15.0
    SPLIT_BRAIN_DETECT_FRESH_S = 10.0

    in_startup_grace = (
        time.monotonic() - self._recorder_started_at < STARTUP_GRACE_S
    )
    if not in_startup_grace and self._motion_manager is not None:
        detect_dt = self._motion_manager.get_last_decoded_frame_dt(self.camera.id)
        if detect_dt is not None:
            detect_elapsed = (datetime.now(timezone.utc) - detect_dt).total_seconds()
            if (elapsed > SPLIT_BRAIN_BYTES_THRESHOLD_S
                    and detect_elapsed < SPLIT_BRAIN_DETECT_FRESH_S):
                logger.error(
                    "split-brain on %s: detect fresh (%.1fs) but recorder "
                    "bytes stalled (%.1fs); restarting recorder",
                    self.camera.ip, detect_elapsed, elapsed,
                )
                self._register_split_brain_restart()  # added in commit 3
                terminate_process_group(self._proc, signal.SIGTERM)
                return
    ```

    In this commit, stub `_register_split_brain_restart` as a no-op method; commit 3 fills it in.

**`backend/recording/manager.py`**:

11. Add pass-through method for the setter:
    ```python
    def attach_motion_manager(self, motion_manager: "MotionManager") -> None:
        self._motion_manager = motion_manager
        for recorder in self.recorders.values():
            recorder.attach_motion_manager(motion_manager)
    ```
    Also store `self._motion_manager` in `__init__` as `None`, and inside `_start_recorder` (wherever `CameraRecorder(...)` is constructed), call `recorder.attach_motion_manager(self._motion_manager)` if it's set. This handles recorders that start *after* the wiring.

**`backend/main.py`** (~line 175, right after `motion = MotionManager(conn, event_bus, recorder)`):

12. Add:
    ```python
    recorder.attach_motion_manager(motion)
    ```

**Verification for commit 2**:
- Unit-test the split-brain rule with a mocked motion_manager: feed (bytes_elapsed, detect_elapsed) combinations across the decision boundary.
- Integration: with a healthy test camera, confirm `get_last_decoded_frame_dt` returns a fresh datetime within 1s of `now`. Disable the classifier (`SIMPLENVR_CLASSIFIER=off`), confirm the method returns `None` and the watchdog skips the split-brain block without logging anything unusual.
- Real-world: run against the Eufy camera for 1-2h; confirm split-brain fires if the camera enters the pathology, and confirm no false-positives during normal operation.

---

### Commit 3 — "recording: circuit breaker + sub-stream fallback"

**Scope**: chronic-failure escalation. Prevents restart storms and auto-degrades a permanently-broken main-stream to sub-stream.

**`backend/recording/camera_recorder.py`**:

1. Add class constants (near existing watchdog constants):
   ```python
   CIRCUIT_BREAKER_WINDOW_S = 600.0   # 10 minutes
   CIRCUIT_BREAKER_THRESHOLD = 3       # split-brain restarts to trip
   ```

2. Add to `__init__`:
   ```python
   self._split_brain_restarts: deque[float] = deque()
   ```
   `deque` is already imported at line 38.

3. Implement `_register_split_brain_restart` (was stubbed in commit 2):
   ```python
   def _register_split_brain_restart(self) -> None:
       now = time.monotonic()
       self._split_brain_restarts.append(now)
       cutoff = now - CIRCUIT_BREAKER_WINDOW_S
       while self._split_brain_restarts and self._split_brain_restarts[0] < cutoff:
           self._split_brain_restarts.popleft()
       if len(self._split_brain_restarts) >= CIRCUIT_BREAKER_THRESHOLD:
           asyncio.create_task(self._handle_chronic_failure())
           self._split_brain_restarts.clear()  # don't re-trip until new failures
   ```

4. Implement `_handle_chronic_failure`:
   ```python
   async def _handle_chronic_failure(self) -> None:
       """Circuit breaker tripped: 3 split-brain restarts in 10 minutes.
       Emit chronic-failure event and flip to sub-stream (unless the
       user has explicitly chosen a stream)."""
       reason = f"split-brain x{CIRCUIT_BREAKER_THRESHOLD} in {int(CIRCUIT_BREAKER_WINDOW_S/60)}min"
       try:
           await self._event_bus.emit("camera_health", {
               "camera_id": self.camera.id,
               "health": "chronic_recording_failure",
               "reason": reason,
           })
       except Exception as e:
           logger.warning("chronic_recording_failure emit failed for %s: %s", self.camera.ip, e)

       # WHERE guard: only auto-switch if user hasn't explicitly picked a stream.
       # NULL or 'main' = not-explicit (defaults); 'sub' = already switched (idempotent).
       await db.set_stream_override_if_not_set(
           self._conn, camera_id=self.camera.id,
           override="sub", reason=reason,
       )
       # Next supervise-loop iteration will re-read settings and pick up the override.
       terminate_process_group(self._proc, signal.SIGTERM)
   ```

5. **Wherever `_spawn` picks main vs sub URL today** (find by searching for the existing stream-selection logic in `_spawn` or in the URL builder it calls): read `self.camera.recording_stream_override` first. If `'sub'`, use sub-stream URL. Otherwise follow existing logic. This propagates the override after a restart.

**`backend/db.py`** (or wherever migrations live — `backend/db/migrations/` if that exists):

6. Add migration:
   ```sql
   ALTER TABLE cameras ADD COLUMN recording_stream_override TEXT DEFAULT NULL;
   ALTER TABLE cameras ADD COLUMN fallback_reason TEXT DEFAULT NULL;
   ```
   Follow the project's existing migration-versioning convention. If migrations are code-based (checking schema_version), register a new version.

7. Add helper:
   ```python
   async def set_stream_override_if_not_set(
       conn, *, camera_id: str, override: str, reason: str,
   ) -> bool:
       """Sets recording_stream_override ONLY if user hasn't explicitly
       chosen a stream. Returns True if updated."""
       cursor = await conn.execute(
           "UPDATE cameras SET recording_stream_override = ?, fallback_reason = ? "
           "WHERE id = ? AND (recording_stream_override IS NULL "
           "OR recording_stream_override = 'main')",
           (override, reason, camera_id),
       )
       await conn.commit()
       return cursor.rowcount > 0
   ```

**`backend/models.py`**:

8. Add optional fields to the `Camera` dataclass:
   ```python
   recording_stream_override: str | None = None
   fallback_reason: str | None = None
   ```

**`backend/api/` (settings or cameras endpoint)**:

9. Add a PATCH endpoint that clears `recording_stream_override` and `fallback_reason` (the "retry main stream" action). Existing camera-update endpoint may work with field additions; follow existing auth/validation patterns. **No UI for this in commit 3** — consume via API only until frontend follow-up lands.

**Verification for commit 3**:
- Simulate chronic failure: artificially call `_register_split_brain_restart` 3 times quickly in a test; confirm `camera_health = "chronic_recording_failure"` fires and DB update lands.
- Manually set `recording_stream_override = 'main'` for a camera; confirm the circuit breaker's DB UPDATE is a no-op (row count 0).
- After breaker trips, confirm the next recorder respawn uses the sub-stream URL.
- Confirm the circuit breaker's deque is cleared after tripping so it doesn't re-fire immediately on the next failure.

---

## Dead code / comment removal summary

Concrete deletions (commit 1):
- `backend/recording/camera_recorder.py:600-602` — the stderr heartbeat update + its comment

Concrete rewrites (commit 1):
- `backend/recording/camera_recorder.py:54-66` — file-level docstring comment block defending OR-logic; replace with Zone-B rationale
- `backend/recording/camera_recorder.py:647-651` — inline watchdog comment calling file-growth "secondary"; replace with "sole liveness signal"

Constants changed, not deleted (commit 1):
- `STALE_FRAME_THRESHOLD_S`: 120 → 30
- `HEALTH_STALLED_THRESHOLD_S`: 15 → 10
- `HEALTH_OFFLINE_THRESHOLD_S`: 60 → 30

Post-commit-1 grep sweep to confirm cleanup:
- `_last_progress_ts` — should have exactly 4 call sites: declaration, growth-update, watchdog-read, restart-reset
- `STALE_FRAME_THRESHOLD_S` — should only be referenced inside `camera_recorder.py`
- `stderr` + `progress` / `heartbeat` — confirm no remaining code treats stderr as liveness

## Gotchas (read before changing code)

1. **Don't reuse `_last_frame_at` on MotionDetector.** It's emission-time (set only in `_observe_emittable:902`), not decode-time. Empty-scene cameras would silently suppress split-brain. Verified correctness issue. Create the new `_last_frame_decoded_at` field on `DetectFfmpegSource` as specified.
2. **Startup order is fixed**: `RecordingManager` → `MotionManager`. Use post-init setter, not constructor injection.
3. **15s startup grace matters.** Without it, simultaneous recorder+detect spawn false-positives during the 2-3s detect-ffmpeg cold start.
4. **WHERE guard on DB write** prevents clobbering a user's explicit "main" setting.
5. **`SIMPLENVR_CLASSIFIER=off` must degrade gracefully.** Split-brain check skips cleanly when `get_last_decoded_frame_dt` returns `None`. Test both startup-disabled and runtime-toggled-off paths.
6. **Windows ARM process-kill semantics.** `backend/process_cleanup.terminate_process_group` uses POSIX `os.killpg`. **Before merging commit 2**, verify there's a Windows-safe branch. A zombie ffmpeg on Windows holds the single-client RTSP slot at go2rtc and blocks restart. If no Windows branch exists, open a separate issue and keep a note in the PR.
7. **`authed_uri` propagation via event bus** is already handled by the `self._last_progress_ts == 0.0` skip at line 661. Preserve this guard during the refactor — it's the reason the watchdog gracefully handles credential-reload restarts.
8. **Segment finalize path** (`_finalize_segment`) — the deferred sanity check (step 5, not in this plan) would race with the watchdog if both issue terminates. Design already accounts for this (sanity check sets a flag, watchdog acts), but don't re-invent. If you add step 5 later, flag-only — never restart from `_finalize_segment`.

## Out of scope (explicitly — don't add)

- **Zone C (go2rtc internal frozen-frame) detection** — no open-source NVR does this. Graceful degradation: detect-ffmpeg also stalls, split-brain gate returns false, falls through to file-growth watchdog at 30s.
- **End-of-segment sanity check (`validate_segments`)** — architect recommended deferral. Low priority, can ship later as a flag-gated addition with default off.
- **Adaptive sub-stream probation ladder** — rejected. Permanent switch with UI affordance is the product decision.
- **FPS-rate thresholding** — rejected across all options.
- **Frontend UI for "Retry main stream"** — separate follow-up PR after backend lands. For now, API-only.
- **Bitrate/packet-level telemetry polling of go2rtc's `/api/streams`** — potential future addition; no v1 need.

## Test plan

**Commit 1**:
- Unit tests for existing watchdog still pass.
- Run against a healthy local camera for 30 min, confirm no false restarts.
- Simulate stall (`kill -STOP $ffmpeg_pid` targeted at the record-role ffmpeg only): watchdog should terminate & restart within 30-35s.

**Commit 2**:
- Unit tests for the split-brain rule with mocked `motion_manager`.
- With classifier enabled, confirm `get_last_decoded_frame_dt` returns fresh timestamps.
- With `SIMPLENVR_CLASSIFIER=off`, confirm the method returns `None` and split-brain path is a no-op.
- Real-world: Eufy camera 24h smoke test.

**Commit 3**:
- Force 3 `_register_split_brain_restart` calls; verify event + DB write + reset.
- Manually set `recording_stream_override='main'`; verify the WHERE-guarded UPDATE is a no-op.
- Confirm respawn uses sub-stream URL.

**Integration test across all commits**:
- Run the full app in `cargo tauri dev` (not `npm run tauri dev` — panics per memory) for 12+ hours with 3+ cameras of mixed brands. Confirm no silent recording failures, no false positives, sane log output.

## Current state and resume prompt for fresh context

To resume work in a fresh Claude session:

1. Read this file (`plans/recording-reliability-plan.md`) top to bottom.
2. Read `CLAUDE.md` and `docs/architecture.md`.
3. Check git log — commits may have already landed. The plan is commit-by-commit, so check which have been merged and pick up at the next.
4. Before writing code, re-read the **Gotchas** section. Every item in that list is a real correctness risk that was caught during architect review.

Good suggested opening prompt for the fresh session:

> Read `/Users/benjamincates/Dev/simplenvr/plans/recording-reliability-plan.md` and `/Users/benjamincates/Dev/simplenvr/CLAUDE.md`. Then check `git log` to see which of the three commits (1 stderr-fix, 2 split-brain, 3 circuit-breaker) have already landed. Start implementation at the next commit that hasn't landed. Don't re-litigate any decision from the Decisions table — if something feels wrong, re-read the "why" column first.
