"""Decision-boundary tests for the first-segment grace deadline.

`should_trip_first_segment_grace` is the pure predicate behind the
"ffmpeg spawned but never wrote its first segment byte" kill path in
`CameraRecorder._staleness_watchdog`. The file-growth watchdog bails
early while `_last_progress_ts == 0.0`, and ffmpeg's own `-timeout`
only fires on socket I/O failures — so a camera whose RTSP socket
stays alive but never produces decodable media slips past every other
gate. This rule is the last backstop.
"""
from __future__ import annotations

import pytest

from backend.recording.camera_recorder import (
    FIRST_SEGMENT_DEADLINE_S,
    should_trip_first_segment_grace,
)


UPTIME_UNDER = FIRST_SEGMENT_DEADLINE_S - 5.0   # 40s — still inside grace
UPTIME_AT = FIRST_SEGMENT_DEADLINE_S            # 45s — exact boundary
UPTIME_OVER = FIRST_SEGMENT_DEADLINE_S + 5.0    # 50s — past deadline


def test_trips_when_past_deadline_with_no_progress():
    assert should_trip_first_segment_grace(
        recorder_uptime_s=UPTIME_OVER,
        last_progress_ts=0.0,
    ) is True


def test_never_trips_when_progress_has_been_observed():
    # Any non-zero last_progress_ts means at least one segment byte
    # landed — the main staleness ladder owns this case, not us.
    assert should_trip_first_segment_grace(
        recorder_uptime_s=UPTIME_OVER,
        last_progress_ts=1.0,
    ) is False


def test_does_not_trip_inside_grace_window():
    # Typical Tapo/Reolink handshake is 5-15s; we must not false-fire on
    # a slow-but-healthy handshake.
    assert should_trip_first_segment_grace(
        recorder_uptime_s=UPTIME_UNDER,
        last_progress_ts=0.0,
    ) is False


def test_strict_inequality_at_exact_boundary():
    # Matches the `>` (not `>=`) sense in the predicate body: uptime
    # equal to the deadline is "still in grace". A future refactor to
    # `>=` would trip on the edge tick, shortening the effective window.
    assert should_trip_first_segment_grace(
        recorder_uptime_s=UPTIME_AT,
        last_progress_ts=0.0,
    ) is False


@pytest.mark.parametrize(
    "uptime, last_progress, expected",
    [
        (UPTIME_OVER, 0.0, True),
        (UPTIME_OVER, 100.0, False),
        (UPTIME_UNDER, 0.0, False),
        (UPTIME_UNDER, 100.0, False),
    ],
)
def test_matrix(uptime, last_progress, expected):
    assert should_trip_first_segment_grace(
        recorder_uptime_s=uptime,
        last_progress_ts=last_progress,
    ) is expected
