"""Unit tests for the ByteTrack implementation in tracker.py.

Synthetic bbox sequences only — no real detections, no I/O. Maps to
detection pipeline plan §13.1.
"""
from __future__ import annotations

from backend.motion.tracker import (
    ByteTracker,
    Detection,
    TRACK_BUFFER,
)


def _box(
    cx: float,
    cy: float,
    w: float = 40.0,
    h: float = 80.0,
    score: float = 0.9,
    class_id: int = 0,
) -> Detection:
    return Detection(
        x1=cx - w / 2,
        y1=cy - h / 2,
        x2=cx + w / 2,
        y2=cy + h / 2,
        score=score,
        class_id=class_id,
    )


def test_id_stable_across_tiny_gap() -> None:
    """Track survives a 1-frame dropout in the middle of a short sequence.

    Feed the same bbox for 3 frames → skip frame → feed 2 more. The ID
    assigned on the first confirmed frame must persist through the gap.
    """
    tracker = ByteTracker(frame_rate=2)
    det = _box(100, 100)

    # Frame 1: tentative.
    tracks = tracker.update([det])
    assert tracks == []

    # Frame 2: confirmed.
    tracks = tracker.update([det])
    assert len(tracks) == 1
    confirmed_id = tracks[0].track_id

    # Frame 3: still confirmed, same id.
    tracks = tracker.update([det])
    assert len(tracks) == 1
    assert tracks[0].track_id == confirmed_id

    # Frame 4: DROPOUT — no detections. Track coasts, not emitted as confirmed.
    tracks = tracker.update([])
    assert all(t.track_id != confirmed_id or t.state != "removed" for t in tracks)

    # Frame 5: same bbox reappears → should rematch same track.
    tracks = tracker.update([det])
    assert len(tracks) == 1, "re-match after short gap should emit 1 confirmed track"
    assert tracks[0].track_id == confirmed_id, "track id must survive short gap"

    # Frame 6: one more for good measure.
    tracks = tracker.update([det])
    assert tracks[0].track_id == confirmed_id


def test_id_changes_after_long_gap() -> None:
    """A gap longer than TRACK_BUFFER forces a new track id on reappearance."""
    tracker = ByteTracker(frame_rate=2)
    det = _box(100, 100)

    # Two hits to get a confirmed track.
    tracker.update([det])
    tracks = tracker.update([det])
    assert len(tracks) == 1
    old_id = tracks[0].track_id

    # Disappear for TRACK_BUFFER + 2 frames — exceeds coast budget.
    for _ in range(TRACK_BUFFER + 2):
        tracker.update([])

    # Reappear — should be a brand new id (monotonically greater).
    tracker.update([det])  # tentative
    tracks = tracker.update([det])  # confirmed
    assert len(tracks) == 1
    assert tracks[0].track_id != old_id, "long gap must break identity"
    assert tracks[0].track_id > old_id, "ids are monotonically increasing"


def test_class_mismatch_penalty_prefers_same_class() -> None:
    """When two overlapping tracks of different classes both overlap a new
    detection, the class-matching track wins even with slightly worse raw IoU.
    """
    tracker = ByteTracker(frame_rate=2)

    # Seed two confirmed tracks near the same region but in distinguishable
    # positions. Class A at x=100, Class B at x=105 (overlapping).
    det_a_seed = _box(100, 100, class_id=0)  # class A
    det_b_seed = _box(105, 100, class_id=1)  # class B

    # Frame 1: tentative for both.
    tracker.update([det_a_seed, det_b_seed])
    # Frame 2: both confirmed.
    tracks = tracker.update([det_a_seed, det_b_seed])
    assert len(tracks) == 2
    id_a = next(t.track_id for t in tracks if t.class_id == 0)
    id_b = next(t.track_id for t in tracks if t.class_id == 1)

    # Now feed a single class-A detection positioned *closer to track B*.
    # Without the class penalty, Hungarian would pair the detection with
    # track B (higher raw IoU). With the +0.1 class-mismatch penalty
    # against B, the pairing should flip to A.
    # Track A predicted ~ x=100 (w=40), Track B predicted ~ x=105 (w=40).
    # Probe at x=103: offset to A is 3 (IoU ~= 37/43 = 0.860),
    #                offset to B is 2 (IoU ~= 38/42 = 0.905).
    # cost_A = 1 - 0.860 = 0.140; cost_B = 1 - 0.905 + 0.1 = 0.195 → A wins.
    probe = _box(103, 100, class_id=0)
    tracks = tracker.update([probe])

    # The class-A probe should match track A; track B becomes lost and
    # is not emitted.
    emitted_ids = {t.track_id for t in tracks}
    assert id_a in emitted_ids, "class-A probe should keep track A alive"
    # Track B may or may not still be emitted as confirmed depending on
    # whether class penalty fully flipped the assignment. Key assertion:
    # track A survived.
    track_a_now = next(t for t in tracks if t.track_id == id_a)
    assert track_a_now.class_id == 0


def test_confirmed_only_emitted() -> None:
    """update() emits nothing on frame 1 (tentative); 1 track on frame 2."""
    tracker = ByteTracker(frame_rate=2)
    det = _box(200, 150)

    tracks_f1 = tracker.update([det])
    assert tracks_f1 == [], "first frame is tentative, nothing emitted"

    tracks_f2 = tracker.update([det])
    assert len(tracks_f2) == 1
    assert tracks_f2[0].confirmed is True
    assert tracks_f2[0].state == "confirmed"
    assert tracks_f2[0].time_since_update == 0
