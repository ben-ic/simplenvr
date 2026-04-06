"""
Compute current storage status for the API and dashboard.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .. import db
from ..models import StorageStatus

if TYPE_CHECKING:
    import aiosqlite

    from ..models import Settings
    from .manager import RecordingManager


async def compute_storage_status(
    conn: "aiosqlite.Connection",
    manager: "RecordingManager",
    settings: "Settings",
) -> StorageStatus:
    # Completed segments from DB
    used = await db.get_total_used_bytes(conn)

    # Add in-progress segment files (so usage updates in real time, not
    # only when a segment finishes)
    inprogress_bytes = sum(
        r.in_progress_file_bytes for r in manager.recorders.values()
    )
    used += inprogress_bytes

    limit = int(settings.max_storage_gb * 1024 * 1024 * 1024)
    free = max(limit - used, 0)

    # Use live bitrate (falls back to rolling average if available)
    total_bitrate = sum(
        r.live_bitrate_bps for r in manager.recorders.values() if r.is_running
    )
    cameras_recording = sum(1 for r in manager.recorders.values() if r.is_running)

    if total_bitrate > 0:
        seconds_remaining = int(free * 8 / total_bitrate)
    else:
        seconds_remaining = -1  # Estimating...

    # Per-camera bytes (completed)
    cursor = await conn.execute(
        "SELECT camera_id, COALESCE(SUM(file_bytes), 0) AS total "
        "FROM recordings GROUP BY camera_id"
    )
    rows = await cursor.fetchall()
    per_camera: dict[str, int] = {r["camera_id"]: int(r["total"]) for r in rows}

    # Add in-progress per camera
    for cam_id, recorder in manager.recorders.items():
        per_camera[cam_id] = (
            per_camera.get(cam_id, 0) + recorder.in_progress_file_bytes
        )

    return StorageStatus(
        used_bytes=used,
        limit_bytes=limit,
        free_bytes=free,
        total_bitrate_bps=total_bitrate,
        seconds_remaining=seconds_remaining,
        cameras_recording=cameras_recording,
        per_camera_bytes=per_camera,
    )
