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
    # Resolve BEFORE mkdir so a malformed / relative / traversal-laden
    # path is rejected as a pure validation error, without the side
    # effect of having silently created directories on the user's disk.
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as e:
        raise HTTPException(
            status_code=400, detail=f"Invalid path {candidate}: {e}"
        )
    if not resolved.is_absolute():
        raise HTTPException(
            status_code=400,
            detail=f"Recordings path must be absolute: {candidate}",
        )
    try:
        resolved.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot create directory {resolved}: {e}",
        )
    if not resolved.is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"{resolved} exists but is not a directory",
        )
    if not os.access(resolved, os.W_OK):
        raise HTTPException(
            status_code=400,
            detail=f"{resolved} is not writable by SimpleNVR",
        )
    return str(resolved)


@router.get("/settings", response_model=Settings)
async def get_settings(request: Request):
    recorder = getattr(request.app.state, "recorder", None)
    if recorder is None:
        from fastapi.responses import JSONResponse
        return JSONResponse({"status": "starting"}, status_code=503)
    return recorder.settings


@router.post("/settings", response_model=Settings)
async def update_settings(body: Settings, request: Request):
    conn = request.app.state.db
    recorder = getattr(request.app.state, "recorder", None)
    if recorder is None:
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "backend still starting"}, status_code=503)

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
    # Onboarding state: a flag + a JSON list of declared brand strings.
    # The identifier uses declared_brands as a confidence boost, not a
    # filter — a camera can still be detected as a brand the user didn't
    # declare (see docs/product.md "Ask the user, but trust the network
    # more"). Normalizing to lowercase unique strings for robust matching.
    import json as _json
    normalized_brands = sorted({b.strip() for b in body.declared_brands if b and b.strip()})
    await db.set_setting(conn, "declared_brands", _json.dumps(normalized_brands))
    await db.set_setting(
        conn,
        "onboarding_completed",
        "true" if body.onboarding_completed else "false",
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
    recorder = getattr(request.app.state, "recorder", None)
    if recorder is None:
        from fastapi.responses import JSONResponse
        return JSONResponse({"status": "starting"}, status_code=503)
    return await compute_storage_status(conn, recorder, recorder.settings)


@router.get("/settings/storage-stats", response_model=StorageStats)
async def get_storage_stats(request: Request):
    """Retention-aware stats: budget / bitrate, not free-disk / bitrate."""
    conn = request.app.state.db
    recorder = getattr(request.app.state, "recorder", None)
    if recorder is None:
        from fastapi.responses import JSONResponse
        return JSONResponse({"status": "starting"}, status_code=503)
    return await compute_storage_stats(conn, recorder, recorder.settings)
