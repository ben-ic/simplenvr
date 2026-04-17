# Auto-retry main stream after chronic-failure breaker trip

## Goal
After the chronic-failure breaker downgrades a camera to its sub-stream
(`recording_stream_override='sub'`), automatically restore the main
stream once it becomes healthy again — without thrashing on a flaky
main, and without losing recordings during the recovery attempt.

## Behavior

**Cadence:** every 5 minutes while a camera is in chronic-fallback
state (`recording_stream_override='sub'` AND no active lockout).

**Probe mechanism (probe-then-swap):**
1. Spawn a parallel detect-only ffmpeg on the main stream via the
   go2rtc loopback (`rtsp://127.0.0.1:58554/<uuid>`). Reuses go2rtc's
   single upstream RTSP connection to the camera — no dual-claim, safe
   on Tapo/Eufy single-client cameras. The sub recorder keeps writing
   the whole time; zero recording gap during the probe.
2. Probe is healthy if it delivers a first frame within 30 s AND stays
   clean for 60 s (no premature exit, no scene-error log spam).
3. On success: terminate the probe ffmpeg, clear
   `recording_stream_override`, refresh `self.camera`, SIGTERM the
   sub recorder. The normal `_process_monitor` respawn loop brings
   main back up. Emit `camera_health = "ok"` and a one-shot
   `recording_quality_restored` event for the UI.
4. On failure: terminate the probe ffmpeg, leave override at `'sub'`,
   try again at the next 5-minute tick.

## Lockout — preventing thrash on marginally-flaky main

A probe is a 90-second snapshot; a real recording session is the
truth. Main can be healthy enough to pass the probe but flaky enough
to fail an hour-long session (network congestion in waves, firmware
bug under sustained bitrate, dying cable). Without a lockout we'd
oscillate quality every ~6 minutes forever.

**Rule:** if a probe-restored main re-trips the breaker within 1 hour
of the restoration timestamp, set `recording_main_lockout_until = now
+ 24 h` on the camera row. The probe loop reads this column; while
it's in the future, no probes spawn.

**Why 1 hour:** decouples the lockout-trigger window from the probe
cadence (5 min). A trigger window close to cadence would lock us
out on a single bad cycle. 1 hour is a reasonable "did the recording
session actually hold" threshold.

**Why 24 hours:** spans typical self-healing windows — overnight ISP
recovery, scheduled wifi reset, firmware reboot cycle. Long enough
to not be noisy; short enough that a one-time event doesn't ground
the camera permanently.

**User Retry always wins.** `POST /cameras/{id}/retry-main-stream`
clears `recording_main_lockout_until` alongside
`recording_stream_override` and `fallback_reason`. Same principle as
the existing user-pinned `override='main'` guard: explicit user
intent overrides the breaker's automated state.

## Schema

One new nullable column on `cameras`:

```sql
ALTER TABLE cameras ADD COLUMN recording_main_lockout_until TEXT;
```

ISO-8601 UTC timestamp string, NULL meaning "no lockout active."
Stored, not in-memory, so an app restart doesn't reset the probation.

## Pure predicate (testable in isolation)

```python
def should_attempt_main_probe(
    *,
    in_fallback: bool,            # recording_stream_override == 'sub'
    lockout_until: float | None,  # epoch seconds, or None
    last_probe_at: float | None,  # monotonic seconds, or None
    now: float,                   # monotonic seconds
    cadence_s: float = 300.0,
) -> bool:
    """Return True iff the probe loop should spawn a probe right now."""
```

Tested against boundary conditions in
`tests/test_main_probe_rule.py` — same shape as the existing pure
predicates `should_trip_split_brain`, `should_label_record_failing`,
`should_trip_first_segment_grace`.

## Lifecycle + race conditions

- Probe ffmpeg lifecycle is owned by `CameraRecorder`. A new
  `_main_probe_task: asyncio.Task | None` field. Cancelled and awaited
  in `stop()` alongside the other per-recorder tasks.
- If the camera transitions to `health="offline"` during a probe, the
  probe is cancelled (no point probing main when the camera is dark).
- If the user clicks Retry mid-probe, the probe is cancelled and the
  Retry handler proceeds (clearing override + bouncing recorder).
- The restoration timestamp is captured in
  `_main_restored_at: float | None`, used by the breaker's
  `_register_recording_failure_restart` to decide whether the
  re-trip falls inside the 1-hour lockout-trigger window.

## Out of scope (deliberate)

- Configurable cadence / lockout durations — zero-config principle
  from CLAUDE.md feedback memory.
- Exponential cadence — fixed 5 min is simpler and predictable.
- In-place swap probe (kill sub, spawn main, fall back if it fails)
  — cheaper to implement but creates 30-60 s recording gaps every 5
  min when main is permanently broken. Probe-then-swap is the
  premium-feeling default.
- Notifying the user via toast when main is auto-restored — the UI
  state change (badge disappears) is sufficient signal. Revisit if
  user testing shows the recovery feels invisible.

## Files touched (when implemented)

- `backend/recording/camera_recorder.py` — probe loop, probe ffmpeg
  lifecycle, lockout state, predicate
- `backend/db.py` — schema migration for the new column,
  `set_main_lockout_until`, update `clear_stream_override` to also
  clear the lockout
- `backend/api/cameras.py` — Retry endpoint also clears lockout
- `tests/test_main_probe_rule.py` — boundary tests for the predicate
- `docs/architecture.md` — promote the spec from this plan doc into
  the "Recording reliability" section, mark this plan doc done in
  CLAUDE.md
