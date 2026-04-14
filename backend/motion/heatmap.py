"""Per-camera false-alarm heatmap (FP Layer 8 of Detection Pipeline v2).

Learns which spatial cells in a camera's frame generate noise (waving leaves,
shadow flicker, etc.) and produces a down-weighting multiplier for tracks
originating there. Owns its own SQLite schema; does not touch backend.db.

Grid: 16 x 12 per camera. Each cell is a Beta(alpha, beta). Init (1.0, 1.0).
Learning: confirmed FP -> beta += 1 on dominant cell; confirmed TP -> alpha += 1.
Scoring: alpha / (alpha + beta) once alpha + beta > MIN_SAMPLES; else 1.0.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import datetime, timezone
from typing import ClassVar, Sequence


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class HeatmapLayer:
    GRID_W: ClassVar[int] = 16
    GRID_H: ClassVar[int] = 12
    MIN_SAMPLES: ClassVar[int] = 20

    def __init__(self, conn: sqlite3.Connection, camera_id: str) -> None:
        self._conn = conn
        self._camera_id = camera_id
        self._ensure_schema()
        self.grid: dict[tuple[int, int], tuple[float, float]] = {}
        self._load()

    # ---------- schema / persistence ----------

    def _ensure_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS detection_heatmap (
                camera_id  TEXT NOT NULL,
                cell_x     INTEGER NOT NULL,
                cell_y     INTEGER NOT NULL,
                alpha      REAL NOT NULL DEFAULT 1.0,
                beta       REAL NOT NULL DEFAULT 1.0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (camera_id, cell_x, cell_y)
            )
            """
        )
        self._conn.commit()

    def _load(self) -> None:
        cur = self._conn.execute(
            "SELECT cell_x, cell_y, alpha, beta FROM detection_heatmap WHERE camera_id = ?",
            (self._camera_id,),
        )
        for cell_x, cell_y, alpha, beta in cur.fetchall():
            self.grid[(int(cell_x), int(cell_y))] = (float(alpha), float(beta))

    def _write_cell(self, cell_x: int, cell_y: int, alpha: float, beta: float) -> None:
        self._conn.execute(
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
        self._conn.commit()

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

    def _bump(self, cell: tuple[int, int], *, alpha_delta: float, beta_delta: float) -> None:
        alpha, beta = self.grid.get(cell, (1.0, 1.0))
        alpha += alpha_delta
        beta += beta_delta
        self.grid[cell] = (alpha, beta)
        self._write_cell(cell[0], cell[1], alpha, beta)

    # ---------- learning ----------

    def record_false_positive(
        self,
        observations: Sequence[tuple[float, float]],
        frame_width: int,
        frame_height: int,
    ) -> None:
        cell = self._dominant_cell(observations, frame_width, frame_height)
        if cell is None:
            return
        self._bump(cell, alpha_delta=0.0, beta_delta=1.0)

    def record_true_positive(
        self,
        observations: Sequence[tuple[float, float]],
        frame_width: int,
        frame_height: int,
    ) -> None:
        cell = self._dominant_cell(observations, frame_width, frame_height)
        if cell is None:
            return
        self._bump(cell, alpha_delta=1.0, beta_delta=0.0)

    # ---------- inference ----------

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
