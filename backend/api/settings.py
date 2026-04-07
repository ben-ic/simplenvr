"""
Settings and storage status API endpoints.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from .. import db
from ..models import Settings, StorageStats, StorageStatus
from ..recording.storage import (
    _invalidate_storage_stats_cache,
    compute_storage_stats,
    compute_storage_status,
)

router = APIRouter(tags=["settings"])


def _validate_recordings_path(raw: str | None) -> str | None:
    """
    Normalize and validate a user-supplied recordings path.

    Returns the cleaned absolute path string on success, or None if the
    user explicitly cleared the override (empty string or None). Raises
    HTTPException(400) with a human-readable message on any failure the
    user needs to fix in the UI.
    """
    if raw is None:
        return None
    stripped = raw.strip()
    if not stripped:
        return None

    candidate = Path(stripped).expanduser()
    try:
        candidate.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot create directory {candidate}: {e}",
        )
    if not candidate.is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"{candidate} exists but is not a directory",
        )
    if not os.access(candidate, os.W_OK):
        raise HTTPException(
            status_code=400,
            detail=f"{candidate} is not writable by SimpleNVR",
        )
    return str(candidate.resolve())


@router.get("/settings", response_model=Settings)
async def get_settings(request: Request):
    recorder = request.app.state.recorder
    return recorder.settings


@router.post("/settings", response_model=Settings)
async def update_settings(body: Settings, request: Request):
    conn = request.app.state.db
    recorder = request.app.state.recorder

    # Validate the recordings_path BEFORE committing any other setting.
    # If it fails we want to bail out with a clean 400 instead of
    # half-applying the change set.
    clean_recordings_path = _validate_recordings_path(body.recordings_path)

    await db.set_setting(conn, "max_storage_gb", str(body.max_storage_gb))
    await db.set_setting(
        conn, "segment_duration_minutes", str(body.segment_duration_minutes)
    )
    await db.set_setting(
        conn, "recording_enabled", "true" if body.recording_enabled else "false"
    )
    await db.set_setting(conn, "recording_fps", body.recording_fps)
    await db.set_setting(
        conn, "recordings_path", clean_recordings_path or ""
    )

    await recorder.apply_settings_change()
    _invalidate_storage_stats_cache()

    event_bus = request.app.state.event_bus
    await event_bus.emit(
        "settings_updated", recorder.settings.model_dump(mode="json")
    )

    return recorder.settings


@router.get("/storage", response_model=StorageStatus)
async def get_storage(request: Request):
    conn = request.app.state.db
    recorder = request.app.state.recorder
    return await compute_storage_status(conn, recorder, recorder.settings)


@router.get("/settings/storage-stats", response_model=StorageStats)
async def get_storage_stats(request: Request):
    """Retention-aware stats: budget / bitrate, not free-disk / bitrate."""
    conn = request.app.state.db
    recorder = request.app.state.recorder
    return await compute_storage_stats(conn, recorder, recorder.settings)
