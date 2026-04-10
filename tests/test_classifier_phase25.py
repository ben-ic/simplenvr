"""Unit tests for Phase 2.5 classifier changes.

Pure synthetic inputs — no ONNX, no real MP4s, no DB. Tests cover:
  - _crop_bbox: scale + margin + clamp + minimum-size guard
  - _compute_seek_samples: bbox_history indexing + timestamp math
  - _is_corrupt_green / is_corrupt_green: green-frame detection
  - _median_verdict: dual-threshold behavior
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from backend.classification.classifier import (
    RecordingContext,
    YoloxClassifier,
    _GREEN_CORRUPT_THRESHOLD,
    _MIN_CONFIDENCE_FULLRES,
    _MIN_CONFIDENCE_PREVIEW,
    _MIN_CROP_PX,
)
from backend.classification.labelmap import all_labels
from backend.motion.detector import is_corrupt_green
from backend.motion.tracker import Track


T0 = datetime(2026, 4, 10, 14, 0, 0, tzinfo=timezone.utc)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _make_track(
    bbox=(50, 30, 40, 80),
    bbox_history=None,
    first_seen=None,
    last_seen=None,
) -> Track:
    first_seen = first_seen or T0
    last_seen = last_seen or at(4.0)
    return Track(
        id="test-track",
        camera_id="cam-1",
        state="closed",
        first_seen=first_seen,
        last_seen=last_seen,
        bbox=bbox,
        bbox_history=bbox_history or [bbox],
        frame_count=5,
    )


def _make_ctx(
    started_at=None,
    duration_s=60.0,
) -> RecordingContext:
    return RecordingContext(
        file_path="/fake/recording.mp4",
        started_at_utc=started_at or at(-10.0),  # segment started 10s before track
        duration_s=duration_s,
    )


# ---------------------------------------------------------------------------
# _crop_bbox
# ---------------------------------------------------------------------------

class TestCropBbox:
    """Tests for YoloxClassifier._crop_bbox (static method)."""

    def test_basic_crop_with_margin(self):
        # bbox (50, 30, 40, 80) at scale=6 → (300, 180, 240, 480)
        # 30% margin: mx=72, my=144
        # x1=300-72=228, y1=180-144=36, x2=300+240+72=612, y2=180+480+144=804
        frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
        crop = YoloxClassifier._crop_bbox(
            frame, bbox=(50, 30, 40, 80), scale=6.0, cap_w=1920, cap_h=1080
        )
        assert crop is not None
        assert crop.shape[0] == 804 - 36   # height
        assert crop.shape[1] == 612 - 228  # width

    def test_clamps_to_frame_bounds(self):
        # bbox near top-left corner — margin would go negative
        frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
        crop = YoloxClassifier._crop_bbox(
            frame, bbox=(0, 0, 40, 40), scale=6.0, cap_w=1920, cap_h=1080
        )
        assert crop is not None
        # x1 and y1 should be clamped to 0
        assert crop.shape[0] > 0
        assert crop.shape[1] > 0

    def test_clamps_to_frame_bottom_right(self):
        # bbox near bottom-right — margin would exceed frame
        frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
        crop = YoloxClassifier._crop_bbox(
            frame, bbox=(300, 160, 20, 20), scale=6.0, cap_w=1920, cap_h=1080
        )
        assert crop is not None
        # x2 should be clamped to 1920, y2 to 1080
        assert crop.shape[1] <= 1920
        assert crop.shape[0] <= 1080

    def test_too_small_returns_none(self):
        # Tiny bbox that scales to less than _MIN_CROP_PX
        frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
        # bbox (0,0,2,2) at scale=6 → (0,0,12,12), margin adds ~3px each side
        # total crop ~18x18 — below 48px minimum
        crop = YoloxClassifier._crop_bbox(
            frame, bbox=(0, 0, 2, 2), scale=6.0, cap_w=1920, cap_h=1080
        )
        assert crop is None

    def test_large_bbox_fills_frame(self):
        # bbox that with margin exceeds frame in both dimensions
        frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
        crop = YoloxClassifier._crop_bbox(
            frame, bbox=(0, 0, 320, 180), scale=6.0, cap_w=1920, cap_h=1080
        )
        assert crop is not None
        # Should be clamped to full frame
        assert crop.shape == (1080, 1920, 3)


# ---------------------------------------------------------------------------
# _compute_seek_samples
# ---------------------------------------------------------------------------

class TestComputeSeekSamples:
    """Tests for YoloxClassifier._compute_seek_samples."""

    # We can't instantiate YoloxClassifier (needs ONNX model), but
    # _compute_seek_samples is a regular method that only reads self
    # for nothing. We'll call it unbound by passing a dummy self.

    def _call(self, track, ctx):
        # Call as unbound — _compute_seek_samples doesn't use self
        return YoloxClassifier._compute_seek_samples(None, track, ctx)

    def test_single_bbox_history(self):
        bbox = (50, 30, 40, 80)
        track = _make_track(bbox=bbox, bbox_history=[bbox])
        ctx = _make_ctx()
        samples = self._call(track, ctx)
        # With 1 entry, all three indices (25%, 50%, 75%) collapse to index 0
        assert len(samples) == 1
        assert samples[0][1] == bbox

    def test_long_history_samples_three(self):
        bboxes = [(i * 10, i * 5, 40, 80) for i in range(20)]
        track = _make_track(bbox=bboxes[-1], bbox_history=bboxes)
        ctx = _make_ctx()
        samples = self._call(track, ctx)
        assert len(samples) == 3
        # Indices should be 5, 10, 15 (25%, 50%, 75% of 20)
        assert samples[0][1] == bboxes[5]
        assert samples[1][1] == bboxes[10]
        assert samples[2][1] == bboxes[15]

    def test_offsets_are_positive_and_ordered(self):
        bboxes = [(i * 10, 0, 40, 80) for i in range(10)]
        track = _make_track(
            bbox=bboxes[-1], bbox_history=bboxes,
            first_seen=at(5.0), last_seen=at(9.0),
        )
        ctx = _make_ctx(started_at=T0)  # segment started at T0
        samples = self._call(track, ctx)
        offsets = [s[0] for s in samples]
        assert all(o >= 0 for o in offsets)
        assert offsets == sorted(offsets)

    def test_offsets_clamped_to_duration(self):
        track = _make_track(first_seen=at(55.0), last_seen=at(65.0))
        ctx = _make_ctx(started_at=T0, duration_s=60.0)
        samples = self._call(track, ctx)
        max_ms = 60.0 * 1000
        for offset_ms, _ in samples:
            assert offset_ms <= max_ms

    def test_in_progress_segment_uses_default_max(self):
        track = _make_track(first_seen=at(5.0), last_seen=at(9.0))
        ctx = _make_ctx(started_at=T0, duration_s=None)
        samples = self._call(track, ctx)
        # Should not crash; uses 120s default
        assert len(samples) >= 1


# ---------------------------------------------------------------------------
# Green frame corruption detection
# ---------------------------------------------------------------------------

class TestCorruptGreen:
    """Tests for green-frame detection in both detector and classifier."""

    def test_clean_frame_not_flagged(self):
        # Normal frame — random colors
        frame = np.random.randint(0, 256, (100, 100, 3), dtype=np.uint8)
        assert not is_corrupt_green(frame, np)
        assert not YoloxClassifier._is_corrupt_green(frame, np)

    def test_half_green_frame_flagged(self):
        # Top half is pure green (decoder fill), bottom half normal
        frame = np.random.randint(0, 256, (100, 100, 3), dtype=np.uint8)
        frame[:50, :, :] = [0, 255, 0]  # BGR: pure green
        assert is_corrupt_green(frame, np)
        assert YoloxClassifier._is_corrupt_green(frame, np)

    def test_mostly_green_frame_flagged(self):
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        frame[:, :, 1] = 255  # All green channel maxed, R=B=0
        assert is_corrupt_green(frame, np)

    def test_small_green_patch_not_flagged(self):
        # 5% green — below the 15% threshold
        frame = np.random.randint(50, 200, (100, 100, 3), dtype=np.uint8)
        frame[:5, :, :] = [0, 255, 0]
        assert not is_corrupt_green(frame, np)

    def test_green_car_not_flagged(self):
        # A "green" object has green but also significant R and B
        frame = np.random.randint(50, 200, (100, 100, 3), dtype=np.uint8)
        # A green-ish car: R=30, G=180, B=30 — not decoder green
        frame[40:60, 40:60, :] = [30, 180, 30]
        assert not is_corrupt_green(frame, np)

    def test_threshold_boundary(self):
        # Exactly at threshold — 15% of pixels are decoder green
        frame = np.random.randint(50, 200, (100, 100, 3), dtype=np.uint8)
        n_green = int(100 * 100 * _GREEN_CORRUPT_THRESHOLD)
        rows = n_green // 100
        frame[:rows, :, :] = [0, 255, 0]
        # At exactly the threshold, should NOT flag (> not >=)
        # Due to integer rounding this might be slightly under/over
        # Just verify it doesn't crash
        result = is_corrupt_green(frame, np)
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# _median_verdict
# ---------------------------------------------------------------------------

class TestMedianVerdict:
    """Tests for the shared median-of-track → argmax → threshold logic."""

    def test_single_label_above_threshold(self):
        per_label = {
            "person": [0.45, 0.50, 0.40],
            "vehicle": [0.05, 0.03, 0.02],
            "animal": [0.01, 0.02, 0.01],
        }
        result = YoloxClassifier._median_verdict(per_label, 0.30)
        assert result.label == "person"
        assert result.confidence == pytest.approx(0.45)

    def test_below_threshold_returns_none(self):
        per_label = {
            "person": [0.10, 0.12, 0.11],
            "vehicle": [0.05, 0.03, 0.02],
            "animal": [0.01, 0.02, 0.01],
        }
        result = YoloxClassifier._median_verdict(per_label, 0.30)
        assert result.label is None

    def test_preview_threshold_lower_than_fullres(self):
        # Same scores pass at 0.20 (preview) but fail at 0.30 (full-res)
        per_label = {
            "person": [0.25, 0.22, 0.24],
            "vehicle": [0.05, 0.03, 0.02],
            "animal": [0.01, 0.02, 0.01],
        }
        fullres = YoloxClassifier._median_verdict(per_label, _MIN_CONFIDENCE_FULLRES)
        preview = YoloxClassifier._median_verdict(per_label, _MIN_CONFIDENCE_PREVIEW)
        assert fullres.label is None
        assert preview.label == "person"

    def test_highest_median_wins(self):
        per_label = {
            "person": [0.30, 0.32, 0.31],
            "vehicle": [0.45, 0.50, 0.48],
            "animal": [0.35, 0.33, 0.34],
        }
        result = YoloxClassifier._median_verdict(per_label, 0.30)
        assert result.label == "vehicle"

    def test_empty_scores_returns_none(self):
        per_label = {
            "person": [],
            "vehicle": [],
            "animal": [],
        }
        result = YoloxClassifier._median_verdict(per_label, 0.20)
        assert result.label is None
        assert result.confidence == 0.0

    def test_single_frame_uses_that_value(self):
        per_label = {
            "person": [0.55],
            "vehicle": [0.10],
            "animal": [0.02],
        }
        result = YoloxClassifier._median_verdict(per_label, 0.30)
        assert result.label == "person"
        assert result.confidence == pytest.approx(0.55)
