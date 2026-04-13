from __future__ import annotations

import asyncio
import logging
import os
import shlex
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from .. import db
from ..ffmpeg_path import get_ffmpeg
from ..config import MOTION_CLIPS_DIR

logger = logging.getLogger(__name__)


async def create_motion_clip(
    conn,
    camera_id: str,
    event_id: str,
    started_at_iso: str,
    ended_at_iso: str | None,
) -> Optional[Path]:
    """Create an MP4 clip for a completed motion event by stitching
    existing recording segments that overlap the event time window.

    Returns the Path to the created clip on success, or None on failure.
    """
    try:
        start_dt = datetime.fromisoformat(started_at_iso)
        end_dt = datetime.fromisoformat(ended_at_iso) if ended_at_iso else None
    except Exception as e:
        logger.warning("Invalid ISO times for clip creation: %s", e)
        return None

    # Candidate dates to probe for recordings
    dates = {start_dt.date().isoformat()}
    if end_dt:
        dates.add(end_dt.date().isoformat())
    # also probe previous/next day in case of timezone/edge cases
    prev_day = (start_dt.date().toordinal() - 1)
    next_day = (start_dt.date().toordinal() + 1)
    try:
        from datetime import date

        dates.add(date.fromordinal(prev_day).isoformat())
        dates.add(date.fromordinal(next_day).isoformat())
    except Exception:
        pass

    # Collect candidate segments
    segments = []
    for d in sorted(dates):
        try:
            rows = await db.get_recordings_for_date(conn, camera_id, d)
        except Exception:
            continue
        for r in rows:
            if not r.get("started_at"):
                continue
            try:
                r_start = datetime.fromisoformat(r["started_at"])
            except Exception:
                continue
            r_end = None
            if r.get("ended_at"):
                try:
                    r_end = datetime.fromisoformat(r["ended_at"])
                except Exception:
                    pass
            # If no end time, treat as present but less preferable
            segments.append((r_start, r_end, Path(r["file_path"])))

    if not segments:
        logger.info("No recording segments found for event %s cam=%s", event_id, camera_id)
        return None

    # Filter segments that overlap the time window (or are nearby)
    def overlaps(seg_start, seg_end):
        seg_end_eff = seg_end if seg_end is not None else seg_start
        if end_dt:
            return not (seg_end_eff < start_dt or seg_start > end_dt)
        return not (seg_end_eff < start_dt)

    matched = [s for s in segments if overlaps(s[0], s[1])]
    if not matched:
        # fallback to nearest segment by start time
        segments.sort(key=lambda s: abs((s[0] - start_dt).total_seconds()))
        matched = [segments[0]]

    # Order matched by start time
    matched.sort(key=lambda s: s[0])

    # Ensure output dir
    cam_dir = MOTION_CLIPS_DIR / camera_id
    cam_dir.mkdir(parents=True, exist_ok=True)
    out_path = cam_dir / f"{event_id}.mp4"

    # Build concat list file
    with tempfile.NamedTemporaryFile("w", delete=False) as listf:
        list_path = Path(listf.name)
        for s in matched:
            # ffmpeg concat demuxer requires POSIX-style paths quoted
            listf.write("file '")
            listf.write(str(s[2].resolve()).replace("'", "'\\''"))
            listf.write("'\n")
    # Seconds of extra footage to include before and after the motion
    # event for context.  This also prevents zero-length / identical
    # -ss/-to ranges when the event spans only a single frame.
    CLIP_PRE_PAD_S = 3.0
    CLIP_POST_PAD_S = 3.0
    CLIP_MIN_DURATION_S = 8.0

    try:
        ffmpeg = get_ffmpeg()
        # Determine reference start (first segment start)
        ref_start = matched[0][0]

        # Raw event offsets relative to the first matched segment
        raw_start = (start_dt - ref_start).total_seconds()
        raw_end = (end_dt - ref_start).total_seconds() if end_dt else raw_start

        # Apply pre/post padding and enforce a minimum duration so that
        # instant single-frame events never produce a zero-length clip
        # (which causes ffmpeg to exit with rc=234).
        start_offset = max(0.0, raw_start - CLIP_PRE_PAD_S)
        end_offset = raw_end + CLIP_POST_PAD_S

        # Ensure the clip is at least CLIP_MIN_DURATION_S long
        if end_offset - start_offset < CLIP_MIN_DURATION_S:
            end_offset = start_offset + CLIP_MIN_DURATION_S

        # Build ffmpeg args. Use concat demuxer then seek to the desired window.
        # Emit diagnostic logs so we can debug range calculation issues.
        try:
            seg_info = [f"{s[0].isoformat()}->{s[1].isoformat() if s[1] else 'None'}:{s[2]}" for s in matched]
        except Exception:
            seg_info = [str(s[2]) for s in matched]
        logger.info(
            "Clip create: event=%s cam=%s ref_start=%s start_offset=%.3f end_offset=%.3f raw_start=%.3f raw_end=%.3f segments=%s",
            event_id,
            camera_id,
            ref_start.isoformat(),
            start_offset,
            end_offset,
            raw_start,
            raw_end,
            ",".join(seg_info),
        )
        cmd = [
            ffmpeg,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_path),
            "-ss", str(start_offset),
            "-to", str(end_offset),
            # Keep clip generation lightweight: stream-copy the camera's
            # original video bitstream with no re-encode. This avoids per-event
            # encoder load that can starve live view / motion processing under
            # heavy activity. Web-compat transcoding (for HEVC clips) happens
            # lazily at playback time in backend/api/motion.py.
            "-c:v", "copy",
            "-an",
            str(out_path),
        ]

        logger.info("Running ffmpeg for event clip: %s", shlex.join(cmd))

        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning(
                "ffmpeg failed for event %s cam=%s rc=%s stderr=%s",
                event_id,
                camera_id,
                proc.returncode,
                stderr.decode(errors="ignore")[:1024],
            )
            try:
                out_path.unlink(missing_ok=True)
            except Exception:
                pass
            return None

        logger.info("Created motion clip %s for event %s", out_path, event_id)
        try:
            # Persist clip path in DB so metadata is authoritative
            await db.update_motion_event_clip(conn, event_id, str(out_path))
        except Exception:
            logger.exception("Failed to write clip_path to DB for event %s", event_id)
        return out_path
    finally:
        try:
            list_path.unlink()
        except Exception:
            pass
