"""Tests for the per-camera false-alarm heatmap (FP Layer 8)."""

from __future__ import annotations

import sqlite3

import pytest

from backend.motion.heatmap import HeatmapLayer


FRAME_W = 640
FRAME_H = 480


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    try:
        yield c
    finally:
        c.close()


def _obs_at_cell(cell_x: int, cell_y: int) -> list[tuple[float, float]]:
    """Build a dominant-cell observation list pointed at (cell_x, cell_y)."""
    cx = (cell_x + 0.5) / HeatmapLayer.GRID_W * FRAME_W
    cy = (cell_y + 0.5) / HeatmapLayer.GRID_H * FRAME_H
    return [(cx, cy)] * 5


def test_empty_grid_returns_1_0(conn: sqlite3.Connection) -> None:
    layer = HeatmapLayer(conn, "cam-a")
    for gx in range(HeatmapLayer.GRID_W):
        for gy in range(HeatmapLayer.GRID_H):
            cx = (gx + 0.5) / HeatmapLayer.GRID_W * FRAME_W
            cy = (gy + 0.5) / HeatmapLayer.GRID_H * FRAME_H
            assert layer.score_multiplier(cx, cy, FRAME_W, FRAME_H) == 1.0


def test_fp_training_drives_multiplier_down(conn: sqlite3.Connection) -> None:
    layer = HeatmapLayer(conn, "cam-a")
    noisy = (3, 4)
    quiet = (10, 2)

    for _ in range(25):
        layer.record_false_positive(_obs_at_cell(*noisy), FRAME_W, FRAME_H)

    noisy_cx = (noisy[0] + 0.5) / HeatmapLayer.GRID_W * FRAME_W
    noisy_cy = (noisy[1] + 0.5) / HeatmapLayer.GRID_H * FRAME_H
    quiet_cx = (quiet[0] + 0.5) / HeatmapLayer.GRID_W * FRAME_W
    quiet_cy = (quiet[1] + 0.5) / HeatmapLayer.GRID_H * FRAME_H

    noisy_mult = layer.score_multiplier(noisy_cx, noisy_cy, FRAME_W, FRAME_H)
    quiet_mult = layer.score_multiplier(quiet_cx, quiet_cy, FRAME_W, FRAME_H)

    # alpha=1, beta=26 -> 1/27 ~= 0.037
    assert noisy_mult < 0.1
    assert quiet_mult == 1.0


def test_tp_training_drives_multiplier_up(conn: sqlite3.Connection) -> None:
    layer = HeatmapLayer(conn, "cam-a")
    hotspot = (7, 6)

    for _ in range(25):
        layer.record_true_positive(_obs_at_cell(*hotspot), FRAME_W, FRAME_H)

    cx = (hotspot[0] + 0.5) / HeatmapLayer.GRID_W * FRAME_W
    cy = (hotspot[1] + 0.5) / HeatmapLayer.GRID_H * FRAME_H

    mult = layer.score_multiplier(cx, cy, FRAME_W, FRAME_H)
    # alpha=26, beta=1 -> 26/27 ~= 0.963
    assert mult > 0.9
    assert mult <= 1.0


def test_persistence_across_instances(conn: sqlite3.Connection) -> None:
    cam = "cam-roundtrip"
    fp_cell = (2, 2)
    tp_cell = (9, 5)

    first = HeatmapLayer(conn, cam)
    for _ in range(25):
        first.record_false_positive(_obs_at_cell(*fp_cell), FRAME_W, FRAME_H)
    for _ in range(25):
        first.record_true_positive(_obs_at_cell(*tp_cell), FRAME_W, FRAME_H)
    fp_mult_before = first.score_multiplier(
        (fp_cell[0] + 0.5) / HeatmapLayer.GRID_W * FRAME_W,
        (fp_cell[1] + 0.5) / HeatmapLayer.GRID_H * FRAME_H,
        FRAME_W,
        FRAME_H,
    )
    tp_mult_before = first.score_multiplier(
        (tp_cell[0] + 0.5) / HeatmapLayer.GRID_W * FRAME_W,
        (tp_cell[1] + 0.5) / HeatmapLayer.GRID_H * FRAME_H,
        FRAME_W,
        FRAME_H,
    )
    first.close()

    second = HeatmapLayer(conn, cam)
    assert second.grid[fp_cell] == first.grid[fp_cell]
    assert second.grid[tp_cell] == first.grid[tp_cell]

    fp_mult_after = second.score_multiplier(
        (fp_cell[0] + 0.5) / HeatmapLayer.GRID_W * FRAME_W,
        (fp_cell[1] + 0.5) / HeatmapLayer.GRID_H * FRAME_H,
        FRAME_W,
        FRAME_H,
    )
    tp_mult_after = second.score_multiplier(
        (tp_cell[0] + 0.5) / HeatmapLayer.GRID_W * FRAME_W,
        (tp_cell[1] + 0.5) / HeatmapLayer.GRID_H * FRAME_H,
        FRAME_W,
        FRAME_H,
    )
    assert fp_mult_after == fp_mult_before
    assert tp_mult_after == tp_mult_before


def test_per_camera_isolation(conn: sqlite3.Connection) -> None:
    a = HeatmapLayer(conn, "cam-a")
    b = HeatmapLayer(conn, "cam-b")
    cell = (4, 4)
    for _ in range(25):
        a.record_false_positive(_obs_at_cell(*cell), FRAME_W, FRAME_H)

    cx = (cell[0] + 0.5) / HeatmapLayer.GRID_W * FRAME_W
    cy = (cell[1] + 0.5) / HeatmapLayer.GRID_H * FRAME_H
    assert a.score_multiplier(cx, cy, FRAME_W, FRAME_H) < 0.1
    assert b.score_multiplier(cx, cy, FRAME_W, FRAME_H) == 1.0
