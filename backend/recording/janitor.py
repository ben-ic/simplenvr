"""
Storage janitor — enforces the user's storage budget by deleting oldest
completed recordings until total usage is under the limit.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from .. import db

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)


async def enforce_storage_limit(
    conn: "aiosqlite.Connection", limit_bytes: int
) -> tuple[int, list[dict]]:
    """
    Delete oldest completed recordings until total disk usage <= limit_bytes.

    Returns (total_bytes_freed, deleted_rows). `deleted_rows` is a list of
    `{"id": recording_id, "camera_id": camera_id}` dicts so the caller can
    emit a `recordings_deleted` event that lets the frontend invalidate
    any cached timelines for the affected cameras.
    """
    used = await db.get_total_used_bytes(conn)
    if used <= limit_bytes:
        return 0, []

    bytes_to_free = used - limit_bytes
    freed = 0
    deleted: list[dict] = []

    while freed < bytes_to_free:
        oldest = await db.get_oldest_recordings(conn, limit=20)
        if not oldest:
            break

        for rec in oldest:
            file_path = Path(rec["file_path"])
            file_bytes = rec["file_bytes"] or 0

            try:
                if file_path.exists():
                    file_path.unlink()
            except Exception as e:
                logger.warning("Failed to delete %s: %s", file_path, e)

            await db.delete_recording(conn, rec["id"])
            deleted.append({"id": rec["id"], "camera_id": rec["camera_id"]})
            freed += file_bytes

            if freed >= bytes_to_free:
                break

    if freed > 0:
        logger.info(
            "Janitor freed %.1f MB to stay under %.1f GB limit",
            freed / 1024 / 1024,
            limit_bytes / 1024 / 1024 / 1024,
        )

    return freed, deleted


async def cleanup_orphan_files(recordings_dir: Path, conn: "aiosqlite.Connection") -> int:
    """
    Find .mp4 files on disk that aren't in the DB (from crashes etc.)
    and delete them. Returns count of files removed.

    Batches the disk-vs-DB check so we never materialize every
    `file_path` row into memory at once. At 32 cams × 30 days × 1440
    segments/day, the old "pull all paths into a Python set" approach
    was ~140 MB of strings and a multi-second startup pause on
    low-RAM devices.
    """
    if not recordings_dir.exists():
        return 0

    BATCH = 500
    removed = 0
    batch: list[Path] = []

    async def _flush(chunk: list[Path]) -> int:
        if not chunk:
            return 0
        placeholders = ",".join("?" * len(chunk))
        cursor = await conn.execute(
            f"SELECT file_path FROM recordings WHERE file_path IN ({placeholders})",
            [str(p) for p in chunk],
        )
        rows = await cursor.fetchall()
        known = {r["file_path"] for r in rows}
        n = 0
        for mp4 in chunk:
            if str(mp4) in known:
                continue
            try:
                mp4.unlink()
                n += 1
            except Exception as e:
                # Silent swallow here masked real FS errors (permissions,
                # stale handles) during retention runs. Log at debug so
                # noise stays out of INFO but crash investigations have
                # a trail.
                logger.debug("Janitor could not unlink orphan %s: %s", mp4, e)
        return n

    for mp4 in recordings_dir.rglob("*.mp4"):
        batch.append(mp4)
        if len(batch) >= BATCH:
            removed += await _flush(batch)
            batch = []
    removed += await _flush(batch)

    if removed:
        logger.info("Janitor removed %d orphan files", removed)
    return removed
