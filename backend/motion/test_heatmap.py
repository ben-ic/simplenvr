"""Tests for the per-camera false-alarm heatmap (FP Layer 8).

Uses aiosqlite against an on-disk temp DB so the tests exercise the
exact same code path the sidecar does. Connections are single-threaded
per test (single writer — the whole point of the refactor), so
`:memory:` via aiosqlite would also work; temp file is chosen because
it's the closer analogue to production and catches any accidental
reliance on memory-DB quirks.

Tests are sync-on-the-outside, async-on-the-inside: `asyncio.run(inner())`
drives each test, so this file needs no pytest-asyncio markers or
conftest.py. Matches the project's current test infrastructure (no
async tests elsewhere in backend/).
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import aiosqlite
import pytest

from backend.db import SCHEMA
from backend.motion.heatmap import HeatmapLayer


FRAME_W = 640
FRAME_H = 480


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _open_conn(db_path: Path) -> aiosqlite.Connection:
    """Open a fresh aiosqlite connection with the project's real SCHEMA.

    Using `db.SCHEMA` (instead of a local DDL copy) guarantees the tests
    break loudly if `detection_heatmap` ever drifts out of the main
    schema — which is exactly what we want, since the whole refactor
    rests on the table being created by init_db().
    """
    conn = await aiosqlite.connect(str(db_path))
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA busy_timeout=5000")
    await conn.executescript(SCHEMA)
    return conn


def _obs_at_cell(cell_x: int, cell_y: int) -> list[tuple[float, float]]:
    """Build a dominant-cell observation list pointed at (cell_x, cell_y)."""
    cx = (cell_x + 0.5) / HeatmapLayer.GRID_W * FRAME_W
    cy = (cell_y + 0.5) / HeatmapLayer.GRID_H * FRAME_H
    return [(cx, cy)] * 5


def _cell_center(cell_x: int, cell_y: int) -> tuple[float, float]:
    cx = (cell_x + 0.5) / HeatmapLayer.GRID_W * FRAME_W
    cy = (cell_y + 0.5) / HeatmapLayer.GRID_H * FRAME_H
    return cx, cy


def _run(coro: asyncio.coroutines) -> None:
    asyncio.run(coro)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "heatmap_test.db"


# ---------------------------------------------------------------------------
# Existing invariants (ported from the sync version)
# ---------------------------------------------------------------------------


def test_empty_grid_returns_1_0(db_path: Path) -> None:
    async def inner() -> None:
        conn = await _open_conn(db_path)
        try:
            layer = await HeatmapLayer.create(conn, "cam-a")
            for gx in range(HeatmapLayer.GRID_W):
                for gy in range(HeatmapLayer.GRID_H):
                    cx, cy = _cell_center(gx, gy)
                    assert layer.score_multiplier(cx, cy, FRAME_W, FRAME_H) == 1.0
        finally:
            await conn.close()

    _run(inner())


def test_fp_training_drives_multiplier_down(db_path: Path) -> None:
    async def inner() -> None:
        conn = await _open_conn(db_path)
        try:
            layer = await HeatmapLayer.create(conn, "cam-a")
            noisy = (3, 4)
            quiet = (10, 2)
            for _ in range(25):
                await layer.record_false_positive(
                    _obs_at_cell(*noisy), FRAME_W, FRAME_H
                )
            noisy_mult = layer.score_multiplier(
                *_cell_center(*noisy), FRAME_W, FRAME_H
            )
            quiet_mult = layer.score_multiplier(
                *_cell_center(*quiet), FRAME_W, FRAME_H
            )
            # alpha=1, beta=26 -> 1/27 ~= 0.037
            assert noisy_mult < 0.1
            assert quiet_mult == 1.0
        finally:
            await conn.close()

    _run(inner())


def test_tp_training_drives_multiplier_up(db_path: Path) -> None:
    async def inner() -> None:
        conn = await _open_conn(db_path)
        try:
            layer = await HeatmapLayer.create(conn, "cam-a")
            hotspot = (7, 6)
            for _ in range(25):
                await layer.record_true_positive(
                    _obs_at_cell(*hotspot), FRAME_W, FRAME_H
                )
            mult = layer.score_multiplier(
                *_cell_center(*hotspot), FRAME_W, FRAME_H
            )
            # alpha=26, beta=1 -> 26/27 ~= 0.963
            assert mult > 0.9
            assert mult <= 1.0
        finally:
            await conn.close()

    _run(inner())


def test_persistence_across_instances(db_path: Path) -> None:
    """FP/TP learning must survive a process-style restart: write with
    instance A, close, open a fresh connection + instance B, read the
    same grid back. This is the failure mode the old sync sqlite3
    connection was masking — it wrote immediately but shared no state
    with the main aiosqlite connection, so the main connection's view
    could drift."""
    async def inner() -> None:
        cam = "cam-roundtrip"
        fp_cell = (2, 2)
        tp_cell = (9, 5)

        conn_a = await _open_conn(db_path)
        try:
            first = await HeatmapLayer.create(conn_a, cam)
            for _ in range(25):
                await first.record_false_positive(
                    _obs_at_cell(*fp_cell), FRAME_W, FRAME_H
                )
            for _ in range(25):
                await first.record_true_positive(
                    _obs_at_cell(*tp_cell), FRAME_W, FRAME_H
                )
            fp_mult_before = first.score_multiplier(
                *_cell_center(*fp_cell), FRAME_W, FRAME_H
            )
            tp_mult_before = first.score_multiplier(
                *_cell_center(*tp_cell), FRAME_W, FRAME_H
            )
        finally:
            await conn_a.close()

        conn_b = await _open_conn(db_path)
        try:
            second = await HeatmapLayer.create(conn_b, cam)
            assert second.grid[fp_cell] == first.grid[fp_cell]
            assert second.grid[tp_cell] == first.grid[tp_cell]
            fp_mult_after = second.score_multiplier(
                *_cell_center(*fp_cell), FRAME_W, FRAME_H
            )
            tp_mult_after = second.score_multiplier(
                *_cell_center(*tp_cell), FRAME_W, FRAME_H
            )
            assert fp_mult_after == fp_mult_before
            assert tp_mult_after == tp_mult_before
        finally:
            await conn_b.close()

    _run(inner())


def test_per_camera_isolation(db_path: Path) -> None:
    async def inner() -> None:
        conn = await _open_conn(db_path)
        try:
            a = await HeatmapLayer.create(conn, "cam-a")
            b = await HeatmapLayer.create(conn, "cam-b")
            cell = (4, 4)
            for _ in range(25):
                await a.record_false_positive(
                    _obs_at_cell(*cell), FRAME_W, FRAME_H
                )
            cx, cy = _cell_center(*cell)
            assert a.score_multiplier(cx, cy, FRAME_W, FRAME_H) < 0.1
            assert b.score_multiplier(cx, cy, FRAME_W, FRAME_H) == 1.0
        finally:
            await conn.close()

    _run(inner())


# ---------------------------------------------------------------------------
# Single-writer discipline: the whole point of the refactor
# ---------------------------------------------------------------------------


def test_schema_comes_from_main_db_schema(db_path: Path) -> None:
    """HeatmapLayer must not create its own table. If db.SCHEMA forgets
    the detection_heatmap DDL, this test catches it: create() against a
    fresh connection that only ran db.SCHEMA should just work."""
    async def inner() -> None:
        conn = await aiosqlite.connect(str(db_path))
        try:
            await conn.executescript(SCHEMA)  # no HeatmapLayer side effect
            layer = await HeatmapLayer.create(conn, "cam-schema")
            await layer.record_false_positive(_obs_at_cell(0, 0), FRAME_W, FRAME_H)
            # One row persisted, no exception.
            async with conn.execute(
                "SELECT COUNT(*) FROM detection_heatmap WHERE camera_id=?",
                ("cam-schema",),
            ) as cur:
                (n,) = await cur.fetchone()
            assert n == 1
        finally:
            await conn.close()

    _run(inner())


def test_writes_go_through_provided_conn(db_path: Path) -> None:
    """HeatmapLayer writes must land on the aiosqlite.Connection it was
    handed — i.e. not via a second sqlite3.connect() somewhere. We
    verify by reading the row back on the same connection; if a stray
    second connection was doing the write, WAL isolation would hide the
    row until commit on the other connection (and we never commit any
    other connection here)."""
    async def inner() -> None:
        conn = await _open_conn(db_path)
        try:
            layer = await HeatmapLayer.create(conn, "cam-single")
            await layer.record_true_positive(
                _obs_at_cell(5, 5), FRAME_W, FRAME_H
            )
            async with conn.execute(
                "SELECT alpha, beta FROM detection_heatmap "
                "WHERE camera_id=? AND cell_x=? AND cell_y=?",
                ("cam-single", 5, 5),
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
            alpha, beta = row
            assert alpha == 2.0  # 1.0 init + 1.0 TP
            assert beta == 1.0
        finally:
            await conn.close()

    _run(inner())


def test_concurrent_record_calls_serialize(db_path: Path) -> None:
    """Two in-flight record_* calls on the same connection must not
    raise "database is locked". Under the old two-connection design,
    racing writes hit the SQLite writer lock and threw; under the
    single-connection design aiosqlite's internal worker queue
    serializes them. This test asserts the new behavior."""
    async def inner() -> None:
        conn = await _open_conn(db_path)
        try:
            layer = await HeatmapLayer.create(conn, "cam-race")
            # Fire off 20 concurrent writes — plenty to surface lock
            # contention if the connection wasn't single-writer.
            await asyncio.gather(
                *[
                    layer.record_false_positive(
                        _obs_at_cell(i % HeatmapLayer.GRID_W,
                                     i % HeatmapLayer.GRID_H),
                        FRAME_W, FRAME_H,
                    )
                    for i in range(20)
                ]
            )
            async with conn.execute(
                "SELECT COUNT(*) FROM detection_heatmap WHERE camera_id=?",
                ("cam-race",),
            ) as cur:
                (n,) = await cur.fetchone()
            # 20 writes across potentially-overlapping cells -> at least 1 row.
            assert n >= 1
        finally:
            await conn.close()

    _run(inner())


def test_hot_path_score_multiplier_stays_sync(db_path: Path) -> None:
    """score_multiplier is called per detection per frame. It must not
    touch the DB — only the in-memory `grid` cache. If a future refactor
    makes it async or adds a DB read, this test fails: we close the
    connection before calling it, and a DB-touching implementation will
    raise."""
    async def inner() -> None:
        conn = await _open_conn(db_path)
        layer = await HeatmapLayer.create(conn, "cam-hotpath")
        # Load some state so the grid isn't trivially empty.
        await layer.record_false_positive(_obs_at_cell(1, 1), FRAME_W, FRAME_H)
        await conn.close()

        # Connection is gone. score_multiplier must still return cleanly
        # because it only reads self.grid.
        cx, cy = _cell_center(1, 1)
        result = layer.score_multiplier(cx, cy, FRAME_W, FRAME_H)
        assert isinstance(result, float)
        assert 0.0 <= result <= 1.0

    _run(inner())
