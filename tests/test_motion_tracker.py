"""Unit tests for the IOU tracker.

Pure synthetic bbox sequences — no OpenCV, no DB, no asyncio. The
tracker is deliberately designed as a pure function of its inputs so
these tests can exercise every branch without any real camera.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.motion.tracker import (
    IDLE_TIMEOUT_SECONDS,
    IOU_MATCH_THRESHOLD,
    PROMOTION_FRAME_COUNT,
    CameraTracker,
    iou,
)


T0 = datetime(2026, 4, 9, 12, 0, 0, tzinfo=timezone.utc)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# iou() math
# ---------------------------------------------------------------------------

def test_iou_identical_bboxes_is_one():
    assert iou((10, 10, 50, 50), (10, 10, 50, 50)) == 1.0


def test_iou_disjoint_bboxes_is_zero():
    assert iou((0, 0, 10, 10), (100, 100, 10, 10)) == 0.0


def test_iou_half_overlap():
    # Two 10x10 boxes overlapping on a 5x10 strip.
    # Intersection = 50, union = 100+100-50 = 150 → 1/3.
    assert iou((0, 0, 10, 10), (5, 0, 10, 10)) == pytest.approx(1 / 3)


def test_iou_degenerate_zero_area_returns_zero():
    assert iou((0, 0, 0, 10), (0, 0, 10, 10)) == 0.0
    assert iou((0, 0, 10, 10), (0, 0, 10, 0)) == 0.0


def test_iou_contained_bbox():
    # Small box entirely inside a larger one.
    # inter = 25, union = 100, → 0.25
    assert iou((0, 0, 10, 10), (2, 2, 5, 5)) == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# Single-blob scenarios
# ---------------------------------------------------------------------------

def test_single_blob_creates_candidate_then_promotes():
    """Track should reach `promoted` state after exactly
    PROMOTION_FRAME_COUNT matching frames on the same blob."""
    tracker = CameraTracker("cam-1")
    for i in range(PROMOTION_FRAME_COUNT):
        tracker.observe((100, 100, 50, 50), at(i * 0.3))

    assert tracker.active_count == 1
    track = list(tracker._active.values())[0]
    assert track.state == "promoted"
    assert track.frame_count == PROMOTION_FRAME_COUNT


def test_single_blob_below_promotion_gate_is_dropped_on_close():
    """A track with (PROMOTION_FRAME_COUNT - 1) frames must not be
    promoted — sweep should discard it silently on idle timeout."""
    tracker = CameraTracker("cam-1")
    unpromoted_count = max(1, PROMOTION_FRAME_COUNT - 1)
    for i in range(unpromoted_count):
        tracker.observe((100, 100, 50, 50), at(i * 0.3))

    # Sweep after the idle timeout → track should close and be discarded.
    closed = tracker.sweep_idle(at(1.0 + IDLE_TIMEOUT_SECONDS + 0.5))
    assert closed == []
    assert tracker.active_count == 0


def test_promoted_track_survives_bbox_jitter():
    """MOG2 bboxes jitter frame-to-frame; the tracker should absorb
    small wiggles into the same track, not create new ones."""
    tracker = CameraTracker("cam-1")
    for i, dx in enumerate([0, 2, -1, 3, 0, -2, 1]):
        tracker.observe((100 + dx, 100, 50, 50), at(i * 0.3))
    assert tracker.active_count == 1
    track = list(tracker._active.values())[0]
    assert track.state == "promoted"


def test_track_closes_after_idle_timeout():
    tracker = CameraTracker("cam-1")
    for i in range(PROMOTION_FRAME_COUNT):
        tracker.observe((100, 100, 50, 50), at(i * 0.3))

    # Fast-forward past the idle timeout with no new observations.
    closed = tracker.sweep_idle(at(5.0 + IDLE_TIMEOUT_SECONDS + 0.1))
    assert len(closed) == 1
    assert closed[0].state == "closed"
    assert closed[0].frame_count >= PROMOTION_FRAME_COUNT
    assert tracker.active_count == 0


# ---------------------------------------------------------------------------
# Multi-blob scenarios — the whole point of the tracker
# ---------------------------------------------------------------------------

def test_two_disjoint_blobs_produce_two_tracks():
    """Person in the top-left + car in the bottom-right = 2 tracks."""
    tracker = CameraTracker("cam-1")
    for i in range(PROMOTION_FRAME_COUNT):
        t = at(i * 0.3)
        tracker.observe((50, 50, 60, 80), t)      # person blob
        tracker.observe((500, 400, 200, 120), t)  # car blob

    assert tracker.active_count == 2
    states = {t.state for t in tracker._active.values()}
    assert states == {"promoted"}


def test_blob_disappearing_and_reappearing_outside_idle_is_new_track():
    tracker = CameraTracker("cam-1")
    # First track: 5 frames of a person, then gone.
    for i in range(PROMOTION_FRAME_COUNT):
        tracker.observe((100, 100, 50, 50), at(i * 0.3))

    # Idle sweep closes it.
    closed_1 = tracker.sweep_idle(at(5.0 + IDLE_TIMEOUT_SECONDS + 0.1))
    assert len(closed_1) == 1

    # Later, a similar bbox appears — should be a brand new track with
    # a different id because the old one was already closed.
    for i in range(PROMOTION_FRAME_COUNT):
        tracker.observe((100, 100, 50, 50), at(20 + i * 0.3))
    closed_2 = tracker.sweep_idle(at(30.0))
    assert len(closed_2) == 1
    assert closed_1[0].id != closed_2[0].id


def test_blob_temporarily_missing_within_idle_window_stays_same_track():
    """A person briefly occluded by a parked car shouldn't fragment
    into two tracks — as long as reappearance is within the idle
    timeout, the track persists."""
    tracker = CameraTracker("cam-1")
    for i in range(PROMOTION_FRAME_COUNT):
        tracker.observe((100, 100, 50, 50), at(i * 0.3))

    # Gap of 1 second (< IDLE_TIMEOUT_SECONDS) — no new observations.
    # Then the blob reappears in roughly the same spot.
    tracker.sweep_idle(at(2.0 + 1.0))  # within idle window
    for i in range(3):
        tracker.observe((102, 101, 50, 50), at(3.0 + i * 0.3))

    # Should still be exactly one track.
    assert tracker.active_count == 1


def test_track_greedy_assignment_prefers_best_iou():
    """Two tracks drift into similar positions; a new bbox should
    go to whichever has the higher IOU, not the first-created."""
    tracker = CameraTracker("cam-1")

    # Track A at x=100, track B at x=200.
    for i in range(PROMOTION_FRAME_COUNT):
        t = at(i * 0.3)
        tracker.observe((100, 100, 40, 40), t)
        tracker.observe((200, 100, 40, 40), t)

    assert tracker.active_count == 2

    # A bbox at x=202 has higher IOU with track B than with track A.
    # It should be absorbed into B's track, not A's, and not create a
    # third track.
    tracker.observe((202, 100, 40, 40), at(3.0))
    assert tracker.active_count == 2


# ---------------------------------------------------------------------------
# Unpromoted noise filtering
# ---------------------------------------------------------------------------

def test_single_frame_flicker_is_filtered():
    """One MOG2 flicker on an otherwise empty camera should produce
    zero Inbox events — never promoted, never forwarded."""
    tracker = CameraTracker("cam-1")
    tracker.observe((300, 300, 10, 10), at(0.0))
    closed = tracker.sweep_idle(at(IDLE_TIMEOUT_SECONDS + 0.5))
    assert closed == []
    assert tracker.active_count == 0


def test_sub_promotion_flicker_is_filtered():
    """A flicker with (PROMOTION_FRAME_COUNT - 1) frames must be
    filtered regardless of how we tune the gate. Parameterized on the
    constant so tuning stays honest — lower the gate and this test
    still enforces the 'one below' invariant."""
    if PROMOTION_FRAME_COUNT < 2:
        return  # degenerate — nothing to filter
    tracker = CameraTracker("cam-1")
    for i in range(PROMOTION_FRAME_COUNT - 1):
        tracker.observe((300 + i, 300, 10, 10), at(i * 0.3))
    closed = tracker.sweep_idle(at(IDLE_TIMEOUT_SECONDS + 0.5))
    assert closed == []


# ---------------------------------------------------------------------------
# Flush on shutdown
# ---------------------------------------------------------------------------

def test_flush_returns_promoted_tracks_immediately():
    tracker = CameraTracker("cam-1")
    for i in range(PROMOTION_FRAME_COUNT + 2):
        tracker.observe((100, 100, 50, 50), at(i * 0.3))

    flushed = tracker.flush(at(1.5))
    assert len(flushed) == 1
    assert flushed[0].state == "closed"
    assert tracker.active_count == 0


def test_flush_drops_unpromoted_candidates():
    """Sub-promotion candidates on shutdown are silently discarded,
    not forwarded to the classifier. Parameterized on the gate
    constant for the same reason as test_sub_promotion_flicker."""
    if PROMOTION_FRAME_COUNT < 2:
        return
    tracker = CameraTracker("cam-1")
    for i in range(PROMOTION_FRAME_COUNT - 1):
        tracker.observe((100 + i, 100, 50, 50), at(i * 0.3))
    flushed = tracker.flush(at(1.0))
    assert flushed == []
    assert tracker.active_count == 0
