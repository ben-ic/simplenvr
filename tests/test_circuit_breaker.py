"""Sliding-window tests for the chronic-failure circuit breaker.

`prune_and_check_breaker_trip` is the pure helper extracted from
`CameraRecorder._register_recording_failure_restart`. Each test drives the
deque directly with a fake monotonic clock — no asyncio, no DB, no
CameraRecorder instance. This pins the deque semantics (window prune,
trip threshold) independently of the side effects (asyncio.create_task,
terminate_process_group) that the caller owns.

See `plans/recording-reliability-plan.md` §Decisions for the 3-in-10-minutes
rationale.
"""
from __future__ import annotations

from collections import deque

import pytest

from backend.recording.camera_recorder import (
    CIRCUIT_BREAKER_THRESHOLD,
    CIRCUIT_BREAKER_WINDOW_S,
    prune_and_check_breaker_trip,
)


def _trip_at(restarts: deque[float], now: float) -> bool:
    """Thin wrapper that threads the locked-in production thresholds.
    Lets individual tests focus on the timeline without repeating the
    threshold/window kwargs."""
    return prune_and_check_breaker_trip(
        restarts,
        now=now,
        window_s=CIRCUIT_BREAKER_WINDOW_S,
        threshold=CIRCUIT_BREAKER_THRESHOLD,
    )


# ---------------------------------------------------------------------------
# Under the threshold — two restarts is not a chronic failure.
# ---------------------------------------------------------------------------

def test_two_restarts_in_window_do_not_trip():
    d: deque[float] = deque()
    assert _trip_at(d, now=0.0) is False
    assert _trip_at(d, now=60.0) is False
    assert len(d) == 2


# ---------------------------------------------------------------------------
# Hitting the threshold — three restarts well inside the window trips on
# the third call.
# ---------------------------------------------------------------------------

def test_third_restart_in_window_trips():
    d: deque[float] = deque()
    assert _trip_at(d, now=0.0) is False
    assert _trip_at(d, now=60.0) is False
    assert _trip_at(d, now=120.0) is True


# ---------------------------------------------------------------------------
# Window prune — a first restart older than the window is evicted, so
# three recent restarts trip even with a fourth ancient entry.
# ---------------------------------------------------------------------------

def test_old_entries_are_pruned_by_sliding_window():
    d: deque[float] = deque()
    # t=0: ancient restart that will fall out of the window
    assert _trip_at(d, now=0.0) is False
    # Advance past the window and log three fresh restarts. The ancient
    # entry gets pruned by each call, so we never exceed 3 in the deque
    # until the third fresh restart lands.
    t_base = CIRCUIT_BREAKER_WINDOW_S + 100.0
    assert _trip_at(d, now=t_base) is False
    assert _trip_at(d, now=t_base + 60.0) is False
    assert _trip_at(d, now=t_base + 120.0) is True
    # Deque held just the three fresh entries at the moment of trip.
    assert len(d) == CIRCUIT_BREAKER_THRESHOLD


# ---------------------------------------------------------------------------
# Window boundary — an entry exactly at `now - window_s` is strictly
# less-than-cutoff and gets pruned; one nanosecond inside is kept.
# ---------------------------------------------------------------------------

def test_window_boundary_prune_is_strict_less_than():
    d: deque[float] = deque()
    # Entry placed *at* the cutoff boundary gets pruned.
    # With window=600 and now=600.0, cutoff = 0.0, and the while-loop
    # condition `restarts[0] < cutoff` is False for a t=0.0 entry, so
    # it stays. Pin that semantics here so a future refactor to `<=`
    # doesn't silently flip breaker behavior at the exact boundary.
    d.append(0.0)
    d.append(60.0)
    result = _trip_at(d, now=CIRCUIT_BREAKER_WINDOW_S)
    # Three entries in the window (0, 60, now=600) → trips.
    assert result is True
    assert len(d) == 3


# ---------------------------------------------------------------------------
# Case 4 — post-trip re-arming.
#
# YOUR CONTRIBUTION. When the breaker trips, the current production
# caller clears the deque so it doesn't re-fire immediately on the next
# failure. That means after the 3rd fire, the 4th call starts fresh
# from an empty deque — the next trip needs three more failures.
#
# But is that the behavior you want? Alternatives to consider:
#
#   (a) Clear on trip (current). Chronic-failure event fires at
#       restart #3, then #6, #9... User sees repeated events as long
#       as failures persist, and the sub-stream also flapping 3× will
#       fire a second event.
#
#   (b) Don't clear on trip. The deque stays at threshold; the next
#       failure re-fires immediately, turning the breaker into a
#       every-failure-is-chronic alarm.
#
#   (c) Clear on trip AND gate via a "breaker tripped" latch until
#       user acknowledges via the retry-main-stream endpoint. Prevents
#       chronic_recording_failure event spam, but the backend has to
#       track a latch and reset it on the retry action.
#
# The production code currently implements (a). Your test encodes which
# of these is the intended product behavior. If you want (a), assert
# that after a trip + clear + 2 more restarts, the deque holds only 2
# entries. If you want (b), don't clear in the test body and assert
# the next call trips again. If you want (c), the test may need to
# drive a latched higher-level helper we haven't written yet — flag
# that and we'll add the helper together.
#
# Keep it focused (~5-10 lines). Mention which option you picked in a
# short comment on the test body so future-us knows the decision was
# deliberate.
# ---------------------------------------------------------------------------

def test_post_trip_re_arming_behavior():
    # Option (a) from the docstring: clear-on-trip. Matches production
    # behavior in `_register_recording_failure_restart`. After the breaker
    # fires, the next chronic_recording_failure event requires a fresh
    # run of CIRCUIT_BREAKER_THRESHOLD restarts — no immediate re-fire.
    d: deque[float] = deque()
    assert _trip_at(d, now=0.0) is False
    assert _trip_at(d, now=60.0) is False
    assert _trip_at(d, now=120.0) is True   # 3rd restart → trips
    d.clear()                                # what the caller does on trip
    assert _trip_at(d, now=180.0) is False  # 4th and 5th alone don't re-fire
    assert _trip_at(d, now=240.0) is False
    assert _trip_at(d, now=300.0) is True   # 6th restart → trips again


# ---------------------------------------------------------------------------
# Fast-fail exits feed the same deque as split-brain and plain-stall kills.
#
# The pure helper is source-agnostic — it just takes timestamps — so this
# test is deliberately redundant with `test_third_restart_in_window_trips`
# at the numeric level. Its value is documentary: the breaker's contract
# is that ALL watchdog-initiated restarts count, regardless of which
# specific watchdog branch decided to fire. Three fast-fail exits (e.g.
# an RTSP auth loop where ffmpeg dies rc=255 within a couple seconds each
# time) trip the breaker just like three split-brain kills or three
# plain-stall kills. See `plans/health-labeling-plan.md` §4.3 for why fast-fail
# was previously missed by the Path #3 coverage hole.
# ---------------------------------------------------------------------------

def test_fast_fail_exits_count_toward_breaker_trip():
    d: deque[float] = deque()
    # Typical RTSP auth loop: ffmpeg dies rc!=0 within a few seconds,
    # backoff sleeps briefly, respawns, dies again. No bytes ever land
    # so the staleness watchdog doesn't fire — only _process_monitor
    # sees the exit and registers the restart.
    assert _trip_at(d, now=0.0) is False
    assert _trip_at(d, now=1.5) is False
    assert _trip_at(d, now=3.0) is True
