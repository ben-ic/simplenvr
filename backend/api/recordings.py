"""
Recordings API — list and serve recorded video segments.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from .. import db

router = APIRouter(tags=["recordings"])


@router.get("/recordings/dates")
async def list_dates(request: Request, camera_id: str | None = None):
    """List dates that have recordings, newest first."""
    conn = request.app.state.db
    dates = await db.get_recording_dates(conn, camera_id)
    return {"dates": dates}


@router.get("/recordings")
async def list_recordings(request: Request, camera_id: str, date: str):
    """List all recording segments for a camera on a specific date."""
    conn = request.app.state.db
    recordings = await db.get_recordings_for_date(conn, camera_id, date)
    return {"recordings": recordings}


@router.get("/recordings/timeline")
async def get_timeline(request: Request, camera_id: str, date: str):
    """
    Return a day's recordings as a single timeline payload.

    Each segment includes its second-of-day offset (0-86400) so the
    frontend can render a 24-hour timeline directly.
    """
    from datetime import datetime

    conn = request.app.state.db
    recordings = await db.get_recordings_for_date(conn, camera_id, date)

    timeline = []
    total_duration = 0.0
    for rec in recordings:
        try:
            started = datetime.fromisoformat(rec["started_at"])
            second_of_day = (
                started.hour * 3600 + started.minute * 60 + started.second
            )
            duration = float(rec["duration_s"] or 0)
            timeline.append(
                {
                    "id": rec["id"],
                    "started_at": rec["started_at"],
                    "second_of_day": second_of_day,
                    "duration_s": duration,
                    "file_bytes": rec["file_bytes"],
                    "in_progress": bool(rec["in_progress"]),
                }
            )
            total_duration += duration
        except Exception:
            continue

    return {
        "date": date,
        "camera_id": camera_id,
        "segments": timeline,
        "total_duration_s": total_duration,
    }


@router.get("/recordings/{recording_id}/file")
async def get_recording_file(recording_id: str, request: Request):
    """Serve a recording file with HTTP range support for seeking."""
    conn = request.app.state.db
    rec = await db.get_recording_by_id(conn, recording_id)

    if not rec:
        raise HTTPException(status_code=404, detail="Recording not found")

    file_path = Path(rec["file_path"])
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File missing on disk")

    # FileResponse handles range requests automatically (Starlette)
    return FileResponse(
        path=str(file_path),
        media_type="video/mp4",
        filename=file_path.name,
    )
