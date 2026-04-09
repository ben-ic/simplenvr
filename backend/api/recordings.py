"""
Recordings API — list and serve recorded video segments.
"""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from .. import db
from ..ffmpeg_path import get_ffmpeg

# Per-(camera, date) locks so concurrent requests for the same day.mp4
# don't spawn duplicate ffmpeg concat jobs. Keyed by the absolute cache
# path. Entries live for process lifetime — cheap (one lock per
# camera-day ever requested).
_day_build_locks: dict[str, asyncio.Lock] = {}

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


def _scan_fmp4_init_size(file_path: Path) -> int:
    """Return the byte offset where the fMP4 init segment ends and
    media data begins. Walks the top-level box list, summing the sizes
    of `ftyp` and `moov` boxes (the init data), and stops at the first
    box that isn't one of those — i.e. the first `moof` or `mdat`,
    where media payload starts.

    For our recorder's output (FFmpeg `+faststart +frag_keyframe
    +empty_moov`), this is reliably ~830 bytes — one ftyp + one moov.
    Reads at most a few box headers (~32 bytes) and never decodes any
    media.

    Returns 0 on any unexpected structure so the caller can fall back
    to streaming the whole file (degraded but not broken).
    """
    try:
        with open(file_path, "rb") as f:
            offset = 0
            while True:
                header = f.read(8)
                if len(header) < 8:
                    return offset
                size = int.from_bytes(header[:4], "big")
                name = header[4:8]
                if name in (b"ftyp", b"moov"):
                    if size < 8:
                        return 0
                    offset += size
                    f.seek(offset)
                    continue
                # First non-init box (moof / mdat / sidx) — init ends here.
                return offset
    except OSError:
        return 0


@router.get("/recordings/playlist.m3u8")
async def get_playlist(request: Request, camera_id: str, date: str):
    """HLS VOD playlist for a camera's completed segments on a given date.

    Every file on disk is a self-contained fragmented MP4 produced by
    FFmpeg with `+empty_moov +frag_keyframe` — layout is
    `ftyp + moov + (moof+mdat)*`. Because all segments on a camera-day
    share identical encoder params, a single init (ftyp+moov) describes
    every fragment in the day. We emit ONE `#EXT-X-MAP` at the top of
    the playlist pointing at the first segment's init byterange, then
    reference each segment's media bytes via `#EXT-X-BYTERANGE`.

    The previous version of this endpoint emitted a fresh `#EXT-X-MAP`
    and a `#EXT-X-DISCONTINUITY` for every segment, which forced hls.js
    to reset its decoder once per minute and desynced the seek clock.
    Now discontinuities are emitted ONLY where the wall-clock timestamps
    reveal an actual recording gap — hls.js concatenates contiguous
    fragments into the MSE buffer with no reset, giving sub-250ms seek
    and seamless 60s-boundary playback.
    """
    from datetime import datetime, timedelta

    conn = request.app.state.db
    recordings = await db.get_recordings_for_date(conn, camera_id, date)

    # Filter out rows that survived as `in_progress=0` but have
    # implausible (duration, file_bytes) — these come from recorder
    # crashes where finalization computed duration from
    # `ended_at - started_at` instead of the actual playable length
    # (see recording row aa773ec5 from 2026-04-08 for the canonical
    # case: 10231s duration, 1.5 MB file). hls.js chokes on a
    # playlist that claims 3 hours of video for a 30 KB/s "stream",
    # so we drop them here. The real fix lives in the recorder
    # finalize path; this is the playback-side defense.
    def _is_plausible(r) -> bool:
        dur = float(r["duration_s"] or 0)
        sz = int(r["file_bytes"] or 0)
        if dur <= 0 or sz <= 0:
            return False
        # Anything claiming more than 10 minutes is suspicious — real
        # segments are 60s by recorder design.
        if dur > 600:
            return False
        # Implied bitrate floor: ~50 kbps. Anything below that for
        # H.264 is unplayable garbage.
        if (sz * 8) / dur < 50_000:
            return False
        return True

    completed = [
        r for r in recordings
        if not bool(r["in_progress"]) and _is_plausible(r)
    ]
    if not completed:
        raise HTTPException(
            status_code=404, detail="No completed recordings for this date"
        )

    max_dur = math.ceil(max(float(r["duration_s"] or 0) for r in completed)) + 1

    # Scan the first segment that has readable init bytes. All segments
    # in a camera-day share encoder params, so one init describes them
    # all. If no file is readable (test fixtures with fake paths), we
    # fall through to a MAP-less playlist that just references whole
    # files — well-formed but not byte-range optimized.
    shared_init_uri: str | None = None
    shared_init_size: int = 0
    for rec in completed:
        try:
            sz = _scan_fmp4_init_size(Path(rec["file_path"]))
        except Exception:
            sz = 0
        if sz > 0:
            shared_init_uri = f"/api/recordings/{rec['id']}/file.m4s"
            shared_init_size = sz
            break

    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:7",
        f"#EXT-X-TARGETDURATION:{max_dur}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
    ]
    if shared_init_uri is not None:
        lines.append(
            f'#EXT-X-MAP:URI="{shared_init_uri}",'
            f'BYTERANGE="{shared_init_size}@0"'
        )

    # Gap tolerance: wall-clock slop between a segment's computed end
    # and the next segment's start. FFmpeg segmenter rounds to keyframe
    # boundaries so a 60.000s segment can nominally start 0.2-0.8s off
    # its predecessor's end. Anything larger than this is a real
    # camera-disconnect / recorder-restart gap and gets a discontinuity.
    GAP_TOLERANCE_S = 2.0

    prev_end: datetime | None = None
    for idx, rec in enumerate(completed):
        duration = float(rec["duration_s"] or 0)
        try:
            started = datetime.fromisoformat(rec["started_at"])
        except Exception:
            started = None

        if (
            idx > 0
            and started is not None
            and prev_end is not None
        ):
            gap = (started - prev_end).total_seconds()
            if gap > GAP_TOLERANCE_S:
                lines.append("#EXT-X-DISCONTINUITY")

        # Use the .m4s suffix so hls.js's container detection takes
        # the fragmented-MP4 path instead of the byte-pattern probe
        # (which misidentifies H.264 NAL bytes as MP3 audio sync
        # words). Backend has a route alias `/file.m4s` that maps to
        # the same handler as `/file`.
        file_url = f"/api/recordings/{rec['id']}/file.m4s"
        lines.append(f"#EXTINF:{duration:.3f},")
        if shared_init_uri is not None:
            # Every segment carries its own init at the same offset
            # because the encoder params are stable; request only the
            # media bytes so hls.js doesn't try to re-parse a second
            # moov per fragment.
            try:
                seg_init_size = _scan_fmp4_init_size(Path(rec["file_path"]))
            except Exception:
                seg_init_size = 0
            file_bytes = int(rec["file_bytes"] or 0)
            if seg_init_size > 0 and file_bytes > seg_init_size:
                media_size = file_bytes - seg_init_size
                lines.append(
                    f"#EXT-X-BYTERANGE:{media_size}@{seg_init_size}"
                )
        lines.append(file_url)

        if started is not None:
            prev_end = started + timedelta(seconds=duration)
        else:
            prev_end = None

    lines.append("#EXT-X-ENDLIST")

    body = "\n".join(lines)
    return Response(content=body, media_type="application/vnd.apple.mpegurl")


@router.get("/recordings/{recording_id}/file.m4s")
async def get_recording_file_m4s(recording_id: str, request: Request):
    """Alias of /file with an `.m4s` suffix so hls.js's container
    detection picks the fragmented-MP4 demuxer. Same handler, same
    bytes — only the URL differs."""
    return await get_recording_file(recording_id, request)


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

    # FileResponse handles range requests automatically (Starlette)
    return FileResponse(
        path=str(file_path),
        media_type="video/mp4",
        filename=file_path.name,
    )
