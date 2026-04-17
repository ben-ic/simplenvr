"""Decision-boundary tests for the cross-pipeline split-brain predicate.

`should_trip_split_brain` is the pure predicate extracted from
`CameraRecorder._staleness_watchdog`. It answers "given these three
elapsed measurements, should we restart the record-ffmpeg right now?"

These tests pin the decision surface so a future refactor can't silently
invert a threshold or flip a sense. They do NOT reproduce the underlying
Eufy fragmented-MP4 pathology — only live verification does that. See
`plans/recording-reliability-plan.md` §Decisions for threshold rationale.
"""
from __future__ import annotations

import pytest

from backend.recording.camera_recorder import (
    SPLIT_BRAIN_BYTES_THRESHOLD_S,
    SPLIT_BRAIN_DETECT_FRESH_S,
    STARTUP_GRACE_S,
    should_trip_split_brain,
)


# Threshold-relative helpers. Tests read nicer when each case names its
# intent ("bytes comfortably stalled", "detect barely fresh") rather
# than inlining literal seconds; if the constants ever move, the test
# names stay correct while the numeric boundary shifts with them.
BYTES_STALE = SPLIT_BRAIN_BYTES_THRESHOLD_S + 1.0   # 16s — over the gate
BYTES_BORDERLINE = SPLIT_BRAIN_BYTES_THRESHOLD_S      # 15s — exactly at
BYTES_OK = SPLIT_BRAIN_BYTES_THRESHOLD_S - 1.0        # 14s — under

DETECT_FRESH = SPLIT_BRAIN_DETECT_FRESH_S - 8.0       # 2s — well fresh
DETECT_BORDERLINE = SPLIT_BRAIN_DETECT_FRESH_S        # 10s — exactly at
DETECT_STALE = SPLIT_BRAIN_DETECT_FRESH_S + 2.0       # 12s — over gate

UPTIME_IN_GRACE = STARTUP_GRACE_S - 5.0               # 10s uptime
UPTIME_POST_GRACE = STARTUP_GRACE_S + 5.0             # 20s uptime


# ---------------------------------------------------------------------------
# The happy-path Zone-B pathology: Eufy muxer stuck, go2rtc healthy.
# ---------------------------------------------------------------------------

def test_trips_when_bytes_stalled_and_detect_fresh_past_grace():
    assert should_trip_split_brain(
        bytes_elapsed_s=BYTES_STALE,
        recorder_uptime_s=UPTIME_POST_GRACE,
        detect_elapsed_s=DETECT_FRESH,
    ) is True


# ---------------------------------------------------------------------------
# Startup grace suppresses the rule even when both conditions are met.
# Without this, concurrent recorder+detect spawn false-positives every time.
# ---------------------------------------------------------------------------

def test_never_trips_inside_startup_grace_even_with_full_pathology():
    assert should_trip_split_brain(
        bytes_elapsed_s=BYTES_STALE,
        recorder_uptime_s=UPTIME_IN_GRACE,
        detect_elapsed_s=DETECT_FRESH,
    ) is False


# ---------------------------------------------------------------------------
# Zone C (both ffmpegs dead) — detect is also stale, so split-brain is
# NOT the right diagnosis. Falls through to the 30s file-growth watchdog,
# which will issue the kill.
# ---------------------------------------------------------------------------

def test_does_not_trip_when_both_pipelines_stalled_zone_c():
    assert should_trip_split_brain(
        bytes_elapsed_s=BYTES_STALE,
        recorder_uptime_s=UPTIME_POST_GRACE,
        detect_elapsed_s=DETECT_STALE,
    ) is False


# ---------------------------------------------------------------------------
# Under the bytes threshold — record-ffmpeg is still producing within
# the jitter window. Don't restart on detect's word alone.
# ---------------------------------------------------------------------------

def test_does_not_trip_when_bytes_still_within_jitter_window():
    assert should_trip_split_brain(
        bytes_elapsed_s=BYTES_OK,
        recorder_uptime_s=UPTIME_POST_GRACE,
        detect_elapsed_s=DETECT_FRESH,
    ) is False


# ---------------------------------------------------------------------------
# Exact-boundary semantics — document which side of each threshold wins.
# The rule is strict `>` on bytes and strict `<` on detect (see predicate
# body), so exact equality on either one means "do not trip".
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bytes_s, detect_s, expected",
    [
        (BYTES_BORDERLINE, DETECT_FRESH, False),   # bytes == gate → no
        (BYTES_STALE, DETECT_BORDERLINE, False),   # detect == gate → no
        (BYTES_BORDERLINE, DETECT_BORDERLINE, False),
    ],
)
def test_strict_inequality_at_exact_boundaries(bytes_s, detect_s, expected):
    assert should_trip_split_brain(
        bytes_elapsed_s=bytes_s,
        recorder_uptime_s=UPTIME_POST_GRACE,
        detect_elapsed_s=detect_s,
    ) is expected


# ---------------------------------------------------------------------------
# Case 5 — degradation when the motion manager has no answer.
#
# YOUR CONTRIBUTION. Three distinct runtime scenarios all surface as
# `detect_elapsed_s=None` from the caller's point of view:
#
#   a) `SIMPLENVR_CLASSIFIER=off` at startup → MotionManager's detectors
#      dict is empty, `get_last_decoded_frame_dt` returns None
#   b) Classifier enabled but detector not yet attached for this camera
#      (race between recorder spawn and `attach` call)
#   c) Detector attached but detect-ffmpeg hasn't produced its first
#      frame yet (cold start, typically <3s)
#
# All three must NOT trip the split-brain rule. The predicate's contract
# says `None` degrades gracefully — the watchdog falls through to the
# file-growth kill threshold.
#
# Write a single test asserting this. The assertion is one line; the
# meaningful input is the docstring/comment where you name which of the
# three runtime scenarios you consider in-scope. If you want more than
# one assertion (e.g. parametrized across all three naming strings),
# that also works — the point is to pin the graceful-degradation contract.
# ---------------------------------------------------------------------------

def test_no_trip_when_detect_unavailable():
    # Covers all three `detect_elapsed_s=None` surfaces documented above:
    # classifier off, detector not yet attached, and detect-ffmpeg
    # pre-first-frame. The watchdog degrades to the 30s file-growth kill
    # threshold in every case.
    assert should_trip_split_brain(
        bytes_elapsed_s=BYTES_STALE,
        recorder_uptime_s=UPTIME_POST_GRACE,
        detect_elapsed_s=None,
    ) is False
