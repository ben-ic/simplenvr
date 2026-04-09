"""
Recordings API — list recordings, serve individual segment files,
and serve a simple HLS playlist for cross-segment day playback.

The recorder writes each ~60s segment as a self-contained
fragmented MP4 (ftyp + moov + moof/mdat*). Each file plays
directly in any modern browser via `<video src=file.mp4>`, which
is how the Inbox event clips work. The "Browse Footage" day view
stitches them together into a continuous timeline by handing
hls.js a minimal playlist that lists each segment file as its
own HLS segment with `#EXT-X-DISCONTINUITY` between them. hls.js
handles per-segment PTS normalization via its built-in
timestampOffset logic — no repackaging, no subprocess overhead,
no custom MP4 parsing. The ~400-line repackager this replaces
was built on the assumption that plain faststart MP4s couldn't
play as fragmented HLS, which was verified false on
2026-04-09 against real camera files.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse

from .. import db

router = APIRouter(tags=["recordings"])


@router.get("/recordings/dates")
async def list_dates(request: Request, camera_id: str | None = None):
    """List dates that have recordings, newest first."""
    conn = request.app.state.db
    dates = await db.get_recording_dates(conn, camera_id)
    return {"dates": dates}


@router.get("/recordings/timeline")
async def get_timeline(request: Request, camera_id: str, date: str):
    """
    Return a day's recordings as a single timeline payload.

    Each segment includes its second-of-day offset (0-86400) so the
    frontend can render a 24-hour timeline directly.

    In-progress segments: the stored `duration_s` is 0 because
    finalization hasn't happened yet. We compute an *effective*
    duration on the server so the timeline payload is correct
    without requiring the frontend to work around the zero.
    """
    from datetime import datetime, timezone

    conn = request.app.state.db
    recordings = await db.get_recordings_for_date(conn, camera_id, date)
    now_utc = datetime.now(timezone.utc)

    timeline = []
    total_duration = 0.0
    for rec in recordings:
        try:
            started = datetime.fromisoformat(rec["started_at"])
            second_of_day = (
                started.hour * 3600 + started.minute * 60 + started.second
            )
            in_progress = bool(rec["in_progress"])
            stored_duration = float(rec["duration_s"] or 0)
            if in_progress:
                # Extend to "right now" so the frontend can seek into
                # frames that exist on disk (or in the muxer buffer)
                # but haven't been finalized into the DB duration yet.
                live_duration = max(
                    0.0, (now_utc - started).total_seconds()
                )
                duration = max(stored_duration, live_duration)
            else:
                duration = stored_duration
            timeline.append(
                {
                    "id": rec["id"],
                    "started_at": rec["started_at"],
                    "second_of_day": second_of_day,
                    "duration_s": duration,
                    "file_bytes": rec["file_bytes"],
                    "in_progress": in_progress,
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


# ---------------------------------------------------------------------------
# Day-playback HLS playlist
# ---------------------------------------------------------------------------
#
# Each recorded .mp4 segment is a self-contained fragmented MP4
# produced by FFmpeg with `+frag_keyframe+empty_moov+default_base_moof`.
# We hand hls.js a tiny playlist that lists every segment as its
# own HLS segment URL pointing at the direct /file endpoint, with
# #EXT-X-DISCONTINUITY between them. hls.js handles per-segment
# PTS normalization via its built-in timestampOffset logic — each
# segment is treated as an independent unit with its own init,
# which works because every segment already carries a valid
# ftyp+moov prefix. No repackaging, no ffmpeg subprocess, no
# Python MP4 parsing — just a text playlist.
#
# This was verified on 2026-04-09 by testing direct
# <video src=fragment.mp4> seek latency in Chrome: instant jumps
# at any scrubber position on real 60-second camera files.


def _is_plausible_segment(r) -> bool:
    """Drop rows whose (duration, bytes) pair would produce a broken
    playlist. Guards against the finalize bug where a crashed
    recorder wrote `ended_at - started_at` into duration_s while
    the file on disk was tiny (canonical case: row aa773ec5 from
    2026-04-08, 10231s / 1.5 MB)."""
    dur = float(r["duration_s"] or 0)
    sz = int(r["file_bytes"] or 0)
    if dur <= 0 or sz <= 0:
        return False
    if dur > 600:
        return False
    if (sz * 8) / dur < 50_000:
        return False
    return True


@router.get("/recordings/hls/index.m3u8")
async def get_hls_index(request: Request, camera_id: str, date: str):
    """HLS VOD playlist stitching a camera-day's segments into one
    continuous timeline for hls.js.

    Each entry references the /file endpoint for that recording's
    id, with #EXT-X-DISCONTINUITY between them so hls.js treats
    each segment as an independent fragmented-MP4 unit with its
    own init and PTS normalization. The in-progress tail segment
    is excluded — "today" can lag real time by up to one segment
    (~60s) until the next finalize.
    """
    import math as _math

    conn = request.app.state.db
    recordings = await db.get_recordings_for_date(conn, camera_id, date)
    completed = [
        r for r in recordings
        if not bool(r["in_progress"]) and _is_plausible_segment(r)
    ]
    if not completed:
        raise HTTPException(
            status_code=404, detail="No completed recordings for this date"
        )

    max_dur = _math.ceil(
        max(float(r["duration_s"] or 0) for r in completed)
    ) + 1

    lines: list[str] = [
        "#EXTM3U",
        "#EXT-X-VERSION:6",
        f"#EXT-X-TARGETDURATION:{max_dur}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        "#EXT-X-INDEPENDENT-SEGMENTS",
    ]
    for idx, rec in enumerate(completed):
        if idx > 0:
            lines.append("#EXT-X-DISCONTINUITY")
        duration = float(rec["duration_s"] or 0)
        lines.append(f"#EXTINF:{duration:.3f},")
        lines.append(f"/api/recordings/{rec['id']}/file")
    lines.append("#EXT-X-ENDLIST")

    body = "\n".join(lines) + "\n"
    return Response(
        content=body,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------------------
# Direct file download (used by export + debugging)
# ---------------------------------------------------------------------------


@router.get("/recordings/{recording_id}/file")
async def get_recording_file(recording_id: str, request: Request):
    """Serve a recording file with HTTP range support for seeking."""
    conn = request.app.state.db
    rec = await db.get_recording_by_id(conn, recording_id)

    if not rec:
        raise HTTPException(status_code=404, detail="Recording not found")

    file_path = Path(rec["file_path"]).resolve()
    # Containment check: the stored file_path column has no schema-level
    # constraint, so an attacker who can write to the DB file (or, via
    # the former wildcard CORS, POST a crafted recording row) could
    # point this at /etc/passwd or any other readable file. Reject any
    # path that resolves outside the active recordings directory.
    recordings_dir = request.app.state.recorder.recordings_dir.resolve()
    try:
        file_path.relative_to(recordings_dir)
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File missing on disk")

    # FileResponse handles range requests automatically (Starlette).
    # Content-Disposition: inline so browsers play in a <video> or
    # in the address bar instead of forcing a download. Export
    # (later feature) will use its own endpoint that explicitly
    # sets attachment + filename.
    return FileResponse(
        path=str(file_path),
        media_type="video/mp4",
        headers={"Content-Disposition": "inline"},
    )
