"""Decision-boundary tests for the record_failing label rewrite.

`should_label_record_failing` is the pure predicate extracted from
`CameraRecorder._staleness_watchdog`. It decides whether an "offline"
label the file-growth ladder assigned should be rewritten to
"record_failing" because detect-ffmpeg has decoded a frame recently
enough to prove the camera is reachable.

See `health-labeling-plan.md` §2 for the "why" — OFFLINE on a tile
that's actively rendering live video is misleading; we distinguish
the two using detect-ffmpeg as a witness. The freshness threshold is
intentionally shared with the split-brain kill (`SPLIT_BRAIN_DETECT_
FRESH_S`) — one signal, one source of truth.
"""
from __future__ import annotations

import pytest

from backend.recording.camera_recorder import (
    SPLIT_BRAIN_DETECT_FRESH_S,
    should_label_record_failing,
)


# Threshold-relative helpers. If the freshness gate moves, the named
# cases stay semantically correct while the numeric boundary follows.
DETECT_FRESH = SPLIT_BRAIN_DETECT_FRESH_S - 8.0      # 2s — well fresh
DETECT_BORDERLINE = SPLIT_BRAIN_DETECT_FRESH_S       # 10s — exactly at
DETECT_STALE = SPLIT_BRAIN_DETECT_FRESH_S + 2.0      # 12s — over gate


# ---------------------------------------------------------------------------
# The whole point of the rule: OFFLINE on a live-video tile is wrong.
# ---------------------------------------------------------------------------

def test_rewrites_offline_when_detect_fresh():
    assert should_label_record_failing(
        new_health="offline",
        detect_elapsed_s=DETECT_FRESH,
    ) is True


# ---------------------------------------------------------------------------
# No witness = no rewrite. Both pipelines starved = genuine "Offline".
# ---------------------------------------------------------------------------

def test_keeps_offline_when_detect_also_stale():
    assert should_label_record_failing(
        new_health="offline",
        detect_elapsed_s=DETECT_STALE,
    ) is False


# ---------------------------------------------------------------------------
# Graceful degradation when detect is unavailable. Three runtime scenarios
# all collapse to `detect_elapsed_s=None` from the caller's point of view:
#
#   a) `SIMPLENVR_CLASSIFIER=off` at startup → MotionManager's detectors
#      dict is empty, `get_last_decoded_frame_dt` returns None
#   b) Classifier enabled but detector not yet attached for this camera
#      (race between recorder spawn and attach call)
#   c) Detector attached but detect-ffmpeg hasn't produced its first
#      frame yet (cold start, typically <3s)
#
# In all three we have no proof the camera is reachable, so we keep the
# conservative "offline" label rather than claim "record_failing".
# ---------------------------------------------------------------------------

def test_keeps_offline_when_detect_unavailable():
    assert should_label_record_failing(
        new_health="offline",
        detect_elapsed_s=None,
    ) is False


# ---------------------------------------------------------------------------
# Only "offline" is subject to rewrite. "ok" and "stalled" already describe
# states where the recorder is making progress, so no witness is needed.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("new_health", ["ok", "stalled"])
def test_does_not_rewrite_non_offline_labels(new_health):
    assert should_label_record_failing(
        new_health=new_health,
        detect_elapsed_s=DETECT_FRESH,
    ) is False


# ---------------------------------------------------------------------------
# Strict `<` on detect_elapsed_s, matching `should_trip_split_brain`'s
# convention. Exact equality at the boundary = "do not rewrite".
# ---------------------------------------------------------------------------

def test_strict_inequality_at_detect_fresh_boundary():
    assert should_label_record_failing(
        new_health="offline",
        detect_elapsed_s=DETECT_BORDERLINE,
    ) is False
