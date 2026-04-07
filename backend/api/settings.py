"""
Settings and storage status API endpoints.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from .. import db
from ..models import Settings, StorageStats, StorageStatus
from ..recording.storage import (
    _invalidate_storage_stats_cache,
    compute_storage_stats,
    compute_storage_status,
)

router = APIRouter(tags=["settings"])


@router.get("/settings", response_model=Settings)
async def get_settings(request: Request):
    recorder = request.app.state.recorder
    return recorder.settings


@router.post("/settings", response_model=Settings)
async def update_settings(body: Settings, request: Request):
    conn = request.app.state.db
    recorder = request.app.state.recorder

    await db.set_setting(conn, "max_storage_gb", str(body.max_storage_gb))
    await db.set_setting(
        conn, "segment_duration_minutes", str(body.segment_duration_minutes)
    )
    await db.set_setting(
        conn, "recording_enabled", "true" if body.recording_enabled else "false"
    )
    await db.set_setting(conn, "recording_fps", body.recording_fps)

    await recorder.apply_settings_change()
    _invalidate_storage_stats_cache()

    event_bus = request.app.state.event_bus
    await event_bus.emit("settings_updated", body.model_dump(mode="json"))

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
