"""Motion event REST endpoints."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import asyncio
import logging
import os
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from .. import db
from ..config import MOTION_THUMBNAILS_DIR
from ..config import MOTION_CLIPS_DIR
from ..ffmpeg_path import get_ffmpeg

router = APIRouter(tags=["motion"])
logger = logging.getLogger(__name__)
_CODEC_CACHE: dict[str, tuple[int, int, str | None]] = {}
_TRANSCODE_LOCKS: dict[str, asyncio.Lock] = {}


def _transcode_lock(path: Path) -> asyncio.Lock:
    key = str(path)
    lock = _TRANSCODE_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _TRANSCODE_LOCKS[key] = lock
    return lock

# Maximum gap (seconds) between consecutive events on the same camera
# before they're split into separate episodes.
_EPISODE_GAP_S = 300  # 5 minutes


def _row_to_event(row: dict) -> dict:
    return {
        "id": row["id"],
        "camera_id": row["camera_id"],
        "started_at": row["started_at"],
        "ended_at": row.get("ended_at"),
        "thumbnail_url": f"/api/motion_events/{row['id']}/thumbnail.jpg"
        if row.get("thumbnail_path")
        else None,
        "clip_url": f"/api/motion_events/{row['id']}/clip.mp4" if row.get("clip_path") else None,
        # Phase 2 classifier verdict. Null is a first-class silent-
        # fallback value — the Inbox renders "Motion at X" in that
        # case. The confidence is returned but the frontend never
        # surfaces it; it exists for future UI debug tooling only.
        "object_class": row.get("object_class"),
        "object_confidence": row.get("object_confidence"),
        "summary": row.get("summary"),
        "description": row.get("description"),
    }


async def _ensure_web_clip(src: Path, dst: Path) -> bool:
    """Create a webview-friendly H.264 copy for playback if missing.

    Returns True when dst exists and should be served, False to fall back
    to src.
    """
    # Escape hatch for troubleshooting CPU spikes: never transcode on read.
    if os.environ.get("SIMPLENVR_DISABLE_CLIP_TRANSCODE") == "1":
        return False

    if dst.exists() and dst.is_file():
        return True

    # The player may issue several requests in parallel for the same clip
    # (initial probe + range fetches). Without this lock each request could
    # spawn its own ffmpeg transcode, causing sustained CPU spikes.
    async with _transcode_lock(dst):
        if dst.exists() and dst.is_file():
            return True
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            return False

        ffmpeg = get_ffmpeg()
        cmd = [
            ffmpeg,
            "-y",
            "-i",
            str(src),
            "-an",
            "-c:v",
            "h264_videotoolbox",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(dst),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                logger.warning(
                    "Web clip transcode failed: src=%s dst=%s rc=%s stderr=%s",
                    src,
                    dst,
                    proc.returncode,
                    stderr.decode(errors="ignore")[:512],
                )
                try:
                    dst.unlink(missing_ok=True)
                except Exception:
                    pass
                return False
            return dst.exists()
        except Exception:
            logger.exception("Web clip transcode exception for %s", src)
            return False


async def _probe_video_codec(path: Path) -> str | None:
    """Return the primary video codec name (e.g. h264, hevc), if known."""
    try:
        st = path.stat()
    except Exception:
        return None

    key = str(path)
    cached = _CODEC_CACHE.get(key)
    mtime_ns = st.st_mtime_ns
    size = st.st_size
    if cached and cached[0] == mtime_ns and cached[1] == size:
        return cached[2]

    ffmpeg = Path(get_ffmpeg())
    probe_bin = ffmpeg.with_name(ffmpeg.name.replace("ffmpeg", "ffprobe"))
    probe_cmd = str(probe_bin if probe_bin.exists() else "ffprobe")
    cmd = [
        probe_cmd,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name",
        "-of",
        "default=nw=1:nk=1",
        str(path),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await proc.communicate()
        if proc.returncode != 0:
            _CODEC_CACHE[key] = (mtime_ns, size, None)
            return None
        text = stdout.decode(errors="ignore").strip().splitlines()
        codec = text[0].strip().lower() if text else None
        _CODEC_CACHE[key] = (mtime_ns, size, codec)
        return codec
    except Exception:
        logger.exception("Failed to probe codec for %s", path)
        _CODEC_CACHE[key] = (mtime_ns, size, None)
        return None


@router.get("/motion_events")
async def list_motion_events(request: Request, camera_id: str, date: str):
    conn = request.app.state.db
    rows = await db.get_motion_events_for_date(conn, camera_id, date)
    return {"events": [_row_to_event(r) for r in rows]}


@router.get("/motion_events/search")
async def search_motion_events(
    request: Request,
    q: str = Query(min_length=1, max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
):
    """Search motion events by description (CSV) and summary text."""
    conn = request.app.state.db
    # Simple LIKE search across both text fields. SQLite LIKE is
    # case-insensitive for ASCII, which is fine for our use case.
    # Escape LIKE wildcards (% and _) so user input is treated as
    # literal text, not pattern syntax.
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    term = f"%{escaped}%"
    cursor = await conn.execute(
        "SELECT * FROM motion_events "
        "WHERE (description LIKE ? ESCAPE '\\' OR summary LIKE ? ESCAPE '\\') "
        "AND object_class IS NOT NULL "
        "ORDER BY started_at DESC LIMIT ?",
        (term, term, limit),
    )
    rows = await cursor.fetchall()
    return {"events": [_row_to_event(dict(r)) for r in rows], "query": q}


@router.get("/motion_events/recent")
async def recent_motion_events(
    request: Request,
    # Cap the limit so a LAN peer can't request millions of rows and
    # OOM the backend. 500 is larger than any real UI query but small
    # enough that full serialization stays bounded.
    limit: int = Query(default=20, ge=1, le=500),
    # Noise gate: only return events the classifier labeled (person /
    # vehicle / animal). Unlabeled "Motion at X" events are suppressed.
    # Pass all=true to see everything (debug / future "show all" toggle).
    all: bool = Query(default=False),
    # Optional filters — all applied server-side in SQL so the LIMIT
    # applies after filtering, not before.
    object_class: str | None = Query(default=None),
    camera_id: str | None = Query(default=None),
    started_after: str | None = Query(default=None),
    ended_before: str | None = Query(default=None),
):
    # Validate date params are plausible ISO strings.
    for param_name, val in [("started_after", started_after), ("ended_before", ended_before)]:
        if val is not None:
            try:
                datetime.fromisoformat(val)
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={"detail": f"Invalid ISO date for {param_name}: {val}"},
                )
    # When filtering by object_class, the user explicitly wants that
    # class — bypass the labeled_only gate so "motion" (null class)
    # works. When no object_class filter is set, respect the all flag.
    labeled_only = not all and object_class is None
    conn = request.app.state.db
    rows = await db.get_recent_motion_events(
        conn,
        limit,
        labeled_only=labeled_only,
        object_class=object_class,
        camera_id=camera_id,
        started_after=started_after,
        ended_before=ended_before,
    )
    return {"events": [_row_to_event(r) for r in rows]}


def _group_into_episodes(rows: list[dict]) -> list[dict]:
    """Group labeled motion events into episodes.

    Events on the same camera within _EPISODE_GAP_S of each other are
    collapsed into one episode. The episode carries the best label
    (highest confidence), the time span, event count, and the thumbnail
    from the highest-confidence event.
    """
    if not rows:
        return []

    episodes: list[dict] = []
    current: dict | None = None

    for row in rows:
        try:
            started = datetime.fromisoformat(row["started_at"])
        except Exception:
            continue

        if (
            current is not None
            and row["camera_id"] == current["camera_id"]
            and abs((started - current["_last_time"]).total_seconds()) <= _EPISODE_GAP_S
        ):
            # Extend current episode
            current["event_count"] += 1
            if started < current["_first_time"]:
                current["_first_time"] = started
                current["started_at"] = row["started_at"]
            if started > current["_last_time"]:
                current["_last_time"] = started
            if row.get("ended_at"):
                try:
                    ended = datetime.fromisoformat(row["ended_at"])
                    if current["_end_time"] is None or ended > current["_end_time"]:
                        current["_end_time"] = ended
                        current["ended_at"] = row["ended_at"]
                except Exception:
                    pass
            conf = row.get("object_confidence") or 0
            if conf > current["_best_conf"]:
                current["_best_conf"] = conf
                current["object_class"] = row.get("object_class")
                current["thumbnail_url"] = (
                    f"/api/motion_events/{row['id']}/thumbnail.jpg"
                    if row.get("thumbnail_path") else current["thumbnail_url"]
                )
            current["event_ids"].append(row["id"])
            if row.get("object_class"):
                current["_labels"].add(row["object_class"])
            if not current.get("description") and row.get("description"):
                current["description"] = row["description"]
        else:
            # Start new episode
            if current is not None:
                episodes.append(_finalize_episode(current))
            ended_at = row.get("ended_at")
            end_time = None
            if ended_at:
                try:
                    end_time = datetime.fromisoformat(ended_at)
                except Exception:
                    pass
            current = {
                "id": row["id"],  # primary event id (for clip playback)
                "camera_id": row["camera_id"],
                "started_at": row["started_at"],
                "ended_at": ended_at,
                "object_class": row.get("object_class"),
                "thumbnail_url": (
                    f"/api/motion_events/{row['id']}/thumbnail.jpg"
                    if row.get("thumbnail_path") else None
                ),
                "description": row.get("description"),
                "event_count": 1,
                "event_ids": [row["id"]],
                "_first_time": started,
                "_last_time": started,
                "_end_time": end_time,
                "_best_conf": row.get("object_confidence") or 0,
                "_labels": {row.get("object_class")} if row.get("object_class") else set(),
            }

    if current is not None:
        episodes.append(_finalize_episode(current))

    return episodes


def _finalize_episode(ep: dict) -> dict:
    """Strip internal fields and compute duration."""
    first = ep["_first_time"]
    last = ep["_end_time"] or ep["_last_time"]
    duration_s = max(1, int((last - first).total_seconds()))
    # All distinct labels seen across the episode's events.
    labels = sorted(ep.get("_labels", set()))
    return {
        "id": ep["id"],
        "camera_id": ep["camera_id"],
        "started_at": ep["started_at"],
        "ended_at": ep["ended_at"],
        "object_class": ep["object_class"],  # best single label (for compat)
        "labels": labels,                     # all labels in the episode
        "thumbnail_url": ep["thumbnail_url"],
        "description": ep.get("description"),
        "event_count": ep["event_count"],
        "duration_s": duration_s,
        "event_ids": ep["event_ids"],
    }


@router.get("/episodes/recent")
async def recent_episodes(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
):
    """Grouped motion events for the story Inbox.

    Fetches recent labeled events, groups consecutive events on the same
    camera within 5 minutes into episodes, and returns them newest-first.
    """
    conn = request.app.state.db
    rows = await db.get_recent_motion_events(conn, limit, labeled_only=True)
    episodes = _group_into_episodes(rows)
    return {"episodes": episodes}


@router.get("/story/today")
async def story_today(
    request: Request,
    limit: int = Query(default=200, ge=1, le=1000),
):
    """Compiled story digest for today.

    Runs the template compiler over today's labeled events and returns
    per-camera summaries with collapsed counts and formatted sentences.
    """
    from ..story.compiler import EventInput, compile_story, parse_structured_field

    conn = request.app.state.db
    rows = await db.get_recent_motion_events(conn, limit, labeled_only=True)
    cameras = await db.get_all_cameras(conn)

    camera_names = {}
    for cam in cameras:
        camera_names[cam.id] = cam.name or cam.manufacturer or cam.ip

    events = [
        EventInput(
            id=row["id"],
            camera_id=row["camera_id"],
            started_at=row["started_at"],
            ended_at=row.get("ended_at"),
            object_class=row.get("object_class"),
            description=row.get("description"),
            structured=parse_structured_field(row.get("structured")),
        )
        for row in rows
    ]

    digest = compile_story(events, camera_names, period_label="Today")
    return {
        "period_label": digest.period_label,
        "total_events": digest.total_events,
        "is_quiet": digest.is_quiet,
        "overall_summary": digest.overall_summary,
        "cameras": [
            {
                "camera_id": cam.camera_id,
                "camera_name": cam.camera_name,
                "is_quiet": cam.is_quiet,
                "total_events": cam.total_events,
                "lines": [
                    {
                        "text": line.text,
                        "event_count": line.event_count,
                        "started_at": line.started_at,
                        "event_ids": line.event_ids,
                    }
                    for line in cam.lines
                ],
            }
            for cam in digest.cameras
        ],
    }


@router.get("/today")
async def today_summary(request: Request):
    """Today view: notable events as cards + per-camera routine counts.

    Person events are returned individually (cards with thumbnails).
    Vehicle/animal events are aggregated into per-camera counts.
    """
    conn = request.app.state.db

    # Per-class counts from tracked_events (one row per tracked object).
    cursor = await conn.execute(
        "SELECT camera_id, object_class, COUNT(*) AS cnt "
        "FROM tracked_events "
        "WHERE object_class IS NOT NULL "
        "AND date(started_at, 'localtime') = date('now', 'localtime') "
        "GROUP BY camera_id, object_class"
    )
    camera_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in await cursor.fetchall():
        camera_counts[row["camera_id"]][row["object_class"]] = row["cnt"]

    # Person cards: join tracked_events to motion_events. SELECT m.* so
    # _row_to_event gets object_class / clip_path / confidence — otherwise
    # the card title falls through to "Motion at X" instead of "person at
    # X" because motionEventToInboxEvent keys off object_class.
    cursor = await conn.execute(
        "SELECT m.* "
        "FROM tracked_events t "
        "JOIN motion_events m ON m.id = t.motion_event_id "
        "WHERE t.object_class = 'person' "
        "AND date(t.started_at, 'localtime') = date('now', 'localtime') "
        "GROUP BY m.id "
        "ORDER BY MAX(t.started_at) DESC"
    )
    notable: list[dict] = [
        _row_to_event(dict(r)) for r in await cursor.fetchall()
    ]

    # Build per-camera summaries.
    cameras_db = await db.get_all_cameras(conn)
    cameras = []
    for cam in cameras_db:
        counts = dict(camera_counts.get(cam.id, {}))
        cameras.append({
            "camera_id": cam.id,
            "camera_name": cam.name or cam.manufacturer or cam.ip,
            "person_count": counts.get("person", 0),
            "vehicle_count": counts.get("vehicle", 0),
            "animal_count": counts.get("animal", 0),
            "total": sum(counts.values()),
        })

    cameras.sort(key=lambda c: (-c["total"], c["camera_name"]))

    return {
        "notable": notable,
        "cameras": cameras,
    }


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


@router.get("/motion_events/{event_id}/clip.mp4")
async def motion_clip(request: Request, event_id: str):
    conn = request.app.state.db
    row = await db.get_motion_event_by_id(conn, event_id)
    # If no DB row, 404 — event must exist
    if not row:
        return Response(status_code=404, content=b"Not found")

    root = MOTION_CLIPS_DIR.resolve()

    # Prefer authoritative DB path when present; this survives any future
    # layout changes better than reconstructing camera_id/event_id.
    candidates: list[Path] = []
    raw_clip_path = row.get("clip_path")
    if raw_clip_path:
        try:
            db_path = Path(str(raw_clip_path)).expanduser()
            if not db_path.is_absolute():
                db_path = root / db_path
            candidates.append(db_path)
        except Exception:
            pass

    # Current deterministic layout.
    candidates.append(MOTION_CLIPS_DIR / row["camera_id"] / f"{event_id}.mp4")

    # Legacy fallback: if files were moved or generated in a different camera
    # subfolder, search by event id across the clip tree.
    try:
        for p in MOTION_CLIPS_DIR.rglob(f"{event_id}.mp4"):
            candidates.append(p)
    except Exception:
        pass

    seen: set[str] = set()
    for candidate in candidates:
        try:
            clip_path = candidate.resolve()
        except Exception:
            continue
        key = str(clip_path)
        if key in seen:
            continue
        seen.add(key)

        # Containment: only serve files within the clips dir
        try:
            clip_path.relative_to(root)
        except ValueError:
            continue

        if clip_path.exists() and clip_path.is_file():
            # Fast path: if a pre-generated web-compatible clip already
            # exists, serve it immediately.
            web_path = clip_path.with_name(f"{clip_path.stem}.web.mp4")
            if web_path.exists() and web_path.is_file():
                return FileResponse(str(web_path), media_type="video/mp4")

            # Only transcode legacy HEVC clips on demand. New clips are
            # generated as H.264 in backend.motion.clip and should be served
            # directly to keep UI clip-open latency low.
            codec = await _probe_video_codec(clip_path)
            if codec == "hevc" and await _ensure_web_clip(clip_path, web_path):
                return FileResponse(str(web_path), media_type="video/mp4")
            return FileResponse(str(clip_path), media_type="video/mp4")

    logger.info(
        "Motion clip not found: event=%s cam=%s db_clip_path=%s",
        event_id,
        row.get("camera_id"),
        row.get("clip_path"),
    )
    return Response(status_code=404, content=b"Clip file missing")
