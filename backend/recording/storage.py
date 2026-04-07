"""
Compute current storage status for the API and dashboard.
"""

from __future__ import annotations

import shutil
import time
from typing import TYPE_CHECKING

from .. import db
from ..models import StorageStats, StorageStatus

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


# Cache for compute_storage_stats — keyed implicitly by 60s wall time.
# Storage stats are low-priority and the underlying numbers (DB usage,
# disk free, last-N segment bitrate) only meaningfully change minute to
# minute, so caching avoids hitting the DB on every poll.
_STATS_CACHE_TTL = 60.0
_stats_cache: tuple[float, "StorageStats"] | None = None


def _invalidate_storage_stats_cache() -> None:
    global _stats_cache
    _stats_cache = None


async def compute_storage_stats(
    conn: "aiosqlite.Connection",
    manager: "RecordingManager",
    settings: "Settings",
) -> "StorageStats":
    """Retention-aware storage stats.

    Computes "how many days of recording the user can scrub back" by dividing
    the user's configured storage budget by the empirically measured aggregate
    bitrate from the last N completed segments. This reflects the circular-
    buffer model: oldest segments are overwritten when the budget fills, so
    free disk space is irrelevant to the retention window.
    """
    global _stats_cache
    now_mono = time.monotonic()
    if _stats_cache is not None:
        ts, cached = _stats_cache
        # Invalidate if budget changed since the last cache fill
        if (
            now_mono - ts < _STATS_CACHE_TTL
            and cached.storage_budget_gb == float(settings.max_storage_gb)
        ):
            return cached

    budget_gb = float(settings.max_storage_gb)

    # Current usage = completed segments + bytes already written to in-progress
    used_bytes = await db.get_total_used_bytes(conn)
    used_bytes += sum(
        r.in_progress_file_bytes for r in manager.recorders.values()
    )
    current_usage_gb = used_bytes / 1e9

    # Free disk on the volume that holds recordings (for context, not math).
    # Uses the manager's currently effective recordings dir so a user-
    # configured override (external drive) reports the right volume.
    try:
        usage = shutil.disk_usage(str(manager.recordings_dir))
        free_disk_gb = usage.free / 1e9
    except OSError:
        free_disk_gb = 0.0

    # Empirical bitrate from the last N completed segments
    rows = await db.get_recent_completed_recordings(conn, 20)
    bitrate_gb_per_day: float | None = None
    retention_days: float | None = None
    ready = False

    if rows:
        total_bytes = sum(int(r["file_bytes"] or 0) for r in rows)
        total_secs = sum(float(r["duration_s"] or 0) for r in rows)
        # Need at least a minute of footage to get a stable rate
        if total_secs >= 60 and total_bytes > 0:
            bytes_per_sec = total_bytes / total_secs
            bitrate_gb_per_day = bytes_per_sec * 86400 / 1e9
            if bitrate_gb_per_day > 0 and budget_gb > 0:
                retention_days = budget_gb / bitrate_gb_per_day
                ready = True

    stats = StorageStats(
        storage_budget_gb=budget_gb,
        current_usage_gb=current_usage_gb,
        bitrate_gb_per_day=bitrate_gb_per_day,
        retention_days=retention_days,
        free_disk_gb=free_disk_gb,
        ready=ready,
    )
    _stats_cache = (now_mono, stats)
    return stats
