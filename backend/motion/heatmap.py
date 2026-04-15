"""Per-camera false-alarm heatmap (FP Layer 8 of Detection Pipeline v2).

Learns which spatial cells in a camera's frame generate noise (waving leaves,
shadow flicker, etc.) and produces a down-weighting multiplier for tracks
originating there. Schema lives in backend/db.py's SCHEMA constant so every
connection that opens the DB sees the table; this class shares the main
aiosqlite connection (single-writer discipline).

Grid: 16 x 12 per camera. Each cell is a Beta(alpha, beta). Init (1.0, 1.0).
Learning: confirmed FP -> beta += 1 on dominant cell; confirmed TP -> alpha += 1.
Scoring: alpha / (alpha + beta) once alpha + beta > MIN_SAMPLES; else 1.0.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import TYPE_CHECKING, ClassVar, Sequence

if TYPE_CHECKING:
    import aiosqlite


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class HeatmapLayer:
    GRID_W: ClassVar[int] = 16
    GRID_H: ClassVar[int] = 12
    MIN_SAMPLES: ClassVar[int] = 20

    def __init__(self, conn: "aiosqlite.Connection", camera_id: str) -> None:
        # No I/O in __init__ — async code must go through create(). The
        # bare constructor is still useful for callers that never touch
        # the DB (e.g. scripts/detect_replay.py, where the grid stays
        # empty and score_multiplier returns 1.0 throughout).
        self._conn = conn
        self._camera_id = camera_id
        self.grid: dict[tuple[int, int], tuple[float, float]] = {}

    @classmethod
    async def create(
        cls, conn: "aiosqlite.Connection", camera_id: str
    ) -> "HeatmapLayer":
        """Build a HeatmapLayer and load its persisted cells from `conn`.

        Schema is assumed to already exist (created by db.init_db() via
        the main SCHEMA script). Loads once at construction; subsequent
        reads hit the in-memory `grid` dict.
        """
        layer = cls(conn, camera_id)
        await layer._load()
        return layer

    # ---------- persistence ----------

    async def _load(self) -> None:
        async with self._conn.execute(
            "SELECT cell_x, cell_y, alpha, beta FROM detection_heatmap WHERE camera_id = ?",
            (self._camera_id,),
        ) as cur:
            rows = await cur.fetchall()
        for cell_x, cell_y, alpha, beta in rows:
            self.grid[(int(cell_x), int(cell_y))] = (float(alpha), float(beta))

    async def _write_cell(
        self, cell_x: int, cell_y: int, alpha: float, beta: float
    ) -> None:
        await self._conn.execute(
            """
            INSERT INTO detection_heatmap (camera_id, cell_x, cell_y, alpha, beta, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(camera_id, cell_x, cell_y) DO UPDATE SET
                alpha = excluded.alpha,
                beta = excluded.beta,
                updated_at = excluded.updated_at
            """,
            (self._camera_id, cell_x, cell_y, alpha, beta, _now_iso()),
        )
        await self._conn.commit()

    # ---------- cell math ----------

    @classmethod
    def _point_to_cell(
        cls, cx: float, cy: float, frame_width: int, frame_height: int
    ) -> tuple[int, int]:
        if frame_width <= 0 or frame_height <= 0:
            return (0, 0)
        x = int(cx / frame_width * cls.GRID_W)
        y = int(cy / frame_height * cls.GRID_H)
        if x < 0:
            x = 0
        elif x >= cls.GRID_W:
            x = cls.GRID_W - 1
        if y < 0:
            y = 0
        elif y >= cls.GRID_H:
            y = cls.GRID_H - 1
        return (x, y)

    def _dominant_cell(
        self,
        observations: Sequence[tuple[float, float]],
        frame_width: int,
        frame_height: int,
    ) -> tuple[int, int] | None:
        if not observations:
            return None
        counter: Counter[tuple[int, int]] = Counter()
        for cx, cy in observations:
            counter[self._point_to_cell(cx, cy, frame_width, frame_height)] += 1
        # Counter.most_common is stable on ties (insertion order of the first seen).
        return counter.most_common(1)[0][0]

    async def _bump(
        self, cell: tuple[int, int], *, alpha_delta: float, beta_delta: float
    ) -> None:
        alpha, beta = self.grid.get(cell, (1.0, 1.0))
        alpha += alpha_delta
        beta += beta_delta
        self.grid[cell] = (alpha, beta)
        await self._write_cell(cell[0], cell[1], alpha, beta)

    # ---------- learning (cold path — once per closed track) ----------

    async def record_false_positive(
        self,
        observations: Sequence[tuple[float, float]],
        frame_width: int,
        frame_height: int,
    ) -> None:
        cell = self._dominant_cell(observations, frame_width, frame_height)
        if cell is None:
            return
        await self._bump(cell, alpha_delta=0.0, beta_delta=1.0)

    async def record_true_positive(
        self,
        observations: Sequence[tuple[float, float]],
        frame_width: int,
        frame_height: int,
    ) -> None:
        cell = self._dominant_cell(observations, frame_width, frame_height)
        if cell is None:
            return
        await self._bump(cell, alpha_delta=1.0, beta_delta=0.0)

    # ---------- inference (hot path — per detection, in-memory only) ----------

    def score_multiplier(
        self,
        cx: float,
        cy: float,
        frame_width: int,
        frame_height: int,
    ) -> float:
        cell = self._point_to_cell(cx, cy, frame_width, frame_height)
        alpha, beta = self.grid.get(cell, (1.0, 1.0))
        if (alpha + beta) > self.MIN_SAMPLES:
            return alpha / (alpha + beta)
        return 1.0

    def close(self) -> None:
        """No-op. Writes are immediate; kept for interface symmetry."""
        return None
