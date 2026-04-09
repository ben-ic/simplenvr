"""Motion event REST endpoints."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from .. import db
from ..config import MOTION_THUMBNAILS_DIR

router = APIRouter(tags=["motion"])


def _row_to_event(row: dict) -> dict:
    return {
        "id": row["id"],
        "camera_id": row["camera_id"],
        "started_at": row["started_at"],
        "ended_at": row.get("ended_at"),
        "thumbnail_url": f"/api/motion_events/{row['id']}/thumbnail.jpg"
        if row.get("thumbnail_path")
        else None,
        # Phase 2 classifier verdict. Null is a first-class silent-
        # fallback value — the Inbox renders "Motion at X" in that
        # case. The confidence is returned but the frontend never
        # surfaces it; it exists for future UI debug tooling only.
        "object_class": row.get("object_class"),
        "object_confidence": row.get("object_confidence"),
    }


@router.get("/motion_events")
async def list_motion_events(request: Request, camera_id: str, date: str):
    conn = request.app.state.db
    rows = await db.get_motion_events_for_date(conn, camera_id, date)
    return {"events": [_row_to_event(r) for r in rows]}


@router.get("/motion_events/recent")
async def recent_motion_events(
    request: Request,
    # Cap the limit so a LAN peer can't request millions of rows and
    # OOM the backend. 500 is larger than any real UI query but small
    # enough that full serialization stays bounded.
    limit: int = Query(default=20, ge=1, le=500),
):
    conn = request.app.state.db
    rows = await db.get_recent_motion_events(conn, limit)
    return {"events": [_row_to_event(r) for r in rows]}


@router.get("/motion_events/timeline")
async def motion_timeline(request: Request, camera_id: str, date: str):
    conn = request.app.state.db
    rows = await db.get_motion_events_for_date(conn, camera_id, date)
    out = []
    for r in rows:
        started = r["started_at"]
        ended = r.get("ended_at")
        # Parse seconds-of-day from ISO timestamp HH:MM:SS at index 11..19
        try:
            from datetime import datetime

            s_dt = datetime.fromisoformat(started)
            second_of_day = s_dt.hour * 3600 + s_dt.minute * 60 + s_dt.second
            if ended:
                e_dt = datetime.fromisoformat(ended)
                duration_s = max((e_dt - s_dt).total_seconds(), 1.0)
            else:
                duration_s = 1.0
        except Exception:
            second_of_day = 0
            duration_s = 1.0
        out.append(
            {
                "second_of_day": second_of_day,
                "duration_s": duration_s,
                "event_id": r["id"],
                "thumbnail_url": f"/api/motion_events/{r['id']}/thumbnail.jpg"
                if r.get("thumbnail_path")
                else None,
            }
        )
    return out


@router.get("/motion_events/{event_id}/thumbnail.jpg")
async def motion_thumbnail(request: Request, event_id: str):
    conn = request.app.state.db
    row = await db.get_motion_event_by_id(conn, event_id)
    if not row or not row.get("thumbnail_path"):
        return Response(status_code=404, content=b"Not found")
    path = Path(row["thumbnail_path"]).resolve()
    # Containment: thumbnail_path is stored verbatim in the DB with no
    # schema-level constraint. Reject anything that resolves outside
    # the known motion-thumbnails directory so a tampered row cannot
    # turn this endpoint into an arbitrary-file-read primitive.
    try:
        path.relative_to(MOTION_THUMBNAILS_DIR.resolve())
    except ValueError:
        return Response(status_code=403, content=b"Access denied")
    if not path.exists():
        return Response(status_code=404, content=b"Thumbnail file missing")
    return FileResponse(str(path), media_type="image/jpeg")
