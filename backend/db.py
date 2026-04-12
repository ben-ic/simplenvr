from __future__ import annotations

import json
import re
import shutil

import aiosqlite

from .config import DATA_DIR, DB_PATH
from .models import Camera

# Strict identifier allowlist for DDL interpolation. SQLite does not
# support parameterized DDL, so we validate identifiers against this
# regex before interpolating them into ALTER TABLE / PRAGMA statements.
# Any future migration that passes a non-literal identifier will now
# fail loudly at assert time instead of silently enabling SQL injection.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DECL_RE = re.compile(r"^[A-Za-z0-9_ '\"().,=]+$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cameras (
    id TEXT PRIMARY KEY,
    ip TEXT NOT NULL UNIQUE,
    xaddr TEXT NOT NULL,
    manufacturer TEXT,
    model TEXT,
    firmware TEXT,
    serial_number TEXT,
    hardware_id TEXT,
    resolutions TEXT NOT NULL DEFAULT '[]',
    rtsp_uri TEXT,
    substream_uri TEXT,
    status TEXT NOT NULL DEFAULT 'online',
    username TEXT,
    password TEXT,
    name TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    device_type TEXT NOT NULL DEFAULT 'camera',
    parent_hub_id TEXT,
    hostname TEXT,
    mac_address TEXT,
    identification_source TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recordings (
    id          TEXT PRIMARY KEY,
    camera_id   TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    file_path   TEXT NOT NULL UNIQUE,
    file_bytes  INTEGER NOT NULL DEFAULT 0,
    duration_s  REAL,
    bitrate_bps INTEGER,
    in_progress INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (camera_id) REFERENCES cameras(id)
);
CREATE INDEX IF NOT EXISTS recordings_started ON recordings(started_at);
CREATE INDEX IF NOT EXISTS recordings_camera ON recordings(camera_id, started_at);
-- Expression index on (camera_id, date) so get_recording_dates can
-- satisfy its DISTINCT substr(started_at,1,10) filter via index seek
-- instead of scanning every per-camera row. At 32 cams × 30 days × 1440
-- segments/day this turns a ~43k-row scan per camera switch into O(log n).
CREATE INDEX IF NOT EXISTS recordings_camera_date
    ON recordings(camera_id, substr(started_at, 1, 10));
-- Composite index for the janitor's "oldest completed" query. Without
-- this, enforce_storage_limit's per-iteration SELECT ... WHERE
-- in_progress=0 ORDER BY started_at scans forward through recordings_started
-- re-evaluating the in_progress filter. With it, the query becomes an
-- index seek at (0, min started_at).
CREATE INDEX IF NOT EXISTS recordings_oldest
    ON recordings(in_progress, started_at);

CREATE TABLE IF NOT EXISTS motion_events (
  id            TEXT PRIMARY KEY,
  camera_id     TEXT NOT NULL,
  started_at    TEXT NOT NULL,
  ended_at      TEXT,
  thumbnail_path TEXT,
  FOREIGN KEY (camera_id) REFERENCES cameras(id)
);
CREATE INDEX IF NOT EXISTS motion_events_started ON motion_events(started_at);
CREATE INDEX IF NOT EXISTS motion_events_camera ON motion_events(camera_id, started_at);

-- tracked_events is the IOU tracker's per-object output. One row per
-- promoted track: a blob that survived the 5-frame gate and then went
-- idle for 2 seconds. Distinct from motion_events because a single
-- motion window can produce multiple tracked rows (person + car in
-- the same scene → 2 tracked_events rows, 1 motion_events row). This
-- is what the classifier consumes to emit object_class per track,
-- and what the summarizer consumes to build per-object daily digest
-- sentences like "a delivery person dropped a package at 10:32".
CREATE TABLE IF NOT EXISTS tracked_events (
  id                TEXT PRIMARY KEY,
  camera_id         TEXT NOT NULL,
  motion_event_id   TEXT,
  started_at        TEXT NOT NULL,
  ended_at          TEXT NOT NULL,
  frame_count       INTEGER NOT NULL DEFAULT 0,
  bbox_json         TEXT,
  bbox_history_json TEXT,
  object_class      TEXT,
  object_confidence REAL,
  thumbnail_path    TEXT,
  FOREIGN KEY (camera_id) REFERENCES cameras(id),
  FOREIGN KEY (motion_event_id) REFERENCES motion_events(id)
);
CREATE INDEX IF NOT EXISTS tracked_events_started ON tracked_events(started_at);
CREATE INDEX IF NOT EXISTS tracked_events_camera ON tracked_events(camera_id, started_at);
CREATE INDEX IF NOT EXISTS tracked_events_motion ON tracked_events(motion_event_id);
"""

def _default_storage_gb() -> int:
    """Pick a sensible first-launch storage cap based on the user's disk.

    Target: 10% of total disk, clamped to [50, 100] GB, then capped so
    we never promise more than (free − 20 GB OS headroom).  If the disk
    is too small for even 10 GB, fall back to 10.
    """
    try:
        usage = shutil.disk_usage(DATA_DIR)
        total_gb = usage.total / (1024 ** 3)
        free_gb = usage.free / (1024 ** 3)

        target = max(50, min(100, int(total_gb * 0.10)))
        # Never exceed what's actually free minus a 20 GB OS buffer
        safe_ceiling = max(10, int(free_gb - 20))
        return max(10, min(target, safe_ceiling))
    except OSError:
        return 50


DEFAULT_SETTINGS = {
    "max_storage_gb": str(_default_storage_gb()),
    "segment_duration_minutes": "1",
    "recording_enabled": "true",
    "recording_fps": "original",
}


async def _migrate_add_column(
    conn: aiosqlite.Connection, table: str, column: str, decl: str
) -> None:
    """Idempotently add a column to an existing table."""
    # DDL cannot be parameterized in SQLite, so validate identifiers
    # against a strict allowlist before interpolating. All current call
    # sites pass hardcoded string literals, but this guard keeps a future
    # developer from silently introducing SQL injection by plumbing a
    # runtime value through `table`, `column`, or `decl`.
    if not _IDENT_RE.match(table) or not _IDENT_RE.match(column):
        raise ValueError(f"unsafe identifier in migration: {table}.{column}")
    if not _DECL_RE.match(decl):
        raise ValueError(f"unsafe column declaration: {decl!r}")
    cursor = await conn.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    if any(r["name"] == column for r in rows):
        return
    await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


async def init_db() -> aiosqlite.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(str(DB_PATH))
    conn.row_factory = aiosqlite.Row
    # WAL mode for concurrent reads while the recorder writes
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA synchronous=NORMAL")
    await conn.executescript(SCHEMA)
    # Idempotent migrations for existing DBs (SQLite has no ADD COLUMN IF NOT EXISTS)
    await _migrate_add_column(conn, "cameras", "substream_uri", "TEXT")
    # device_type distinguishes direct cameras from hubs (Eufy HomeBase,
    # Reolink Home Hub, Arlo SmartHub) and from cameras-behind-hubs.
    # Default is "camera" so existing rows stay correct.
    await _migrate_add_column(
        conn, "cameras", "device_type", "TEXT NOT NULL DEFAULT 'camera'"
    )
    # parent_hub_id points a hub_camera at its hub. NULL for direct
    # cameras and for hubs themselves.
    await _migrate_add_column(conn, "cameras", "parent_hub_id", "TEXT")
    # hostname: DHCP-registered name from reverse DNS on the camera IP.
    # mac_address: lowercase colon-separated from ARP + ONVIF.
    # identification_source: which signal populated manufacturer/model.
    await _migrate_add_column(conn, "cameras", "hostname", "TEXT")
    await _migrate_add_column(conn, "cameras", "mac_address", "TEXT")
    await _migrate_add_column(conn, "cameras", "identification_source", "TEXT")
    # --- Classification subsystem migrations ---
    # object_class: the YOLOX-S classifier's verdict for this motion
    # event (person/vehicle/animal) or NULL for silent fallback to
    # "Motion at X". Written by classification.manager after the IOU
    # tracker closes a promoted track and median-scoring chooses a
    # winning label. NULL is a first-class result — it preserves the
    # honest "Motion at X" Inbox sentence when no class crosses the
    # confidence threshold.
    await _migrate_add_column(conn, "motion_events", "object_class", "TEXT")
    # object_confidence: median-of-track confidence that accompanied
    # object_class. Used by the trust strip in A2 Increment 3 and by
    # the debug panel when tuning per-tier thresholds.
    await _migrate_add_column(conn, "motion_events", "object_confidence", "REAL")
    # sound_class: the YAMNet audio classifier's verdict (bark,
    # glass_break, siren, car_horn, etc.) or NULL. Populated
    # independently of object_class — a single event can have
    # vision-only, audio-only, or both labels, and the Inbox sentence
    # template picks whichever is present.
    await _migrate_add_column(conn, "motion_events", "sound_class", "TEXT")
    await _migrate_add_column(conn, "motion_events", "sound_confidence", "REAL")
    # source: which subsystem created this event row.
    #   'vision' — created by the motion detector on a MOG2 blob
    #              (the default for backwards compatibility with
    #               every pre-classifier row).
    #   'audio'  — created by the audio classifier on a high-priority
    #              sound (glass_break/gunshot/scream/siren) firing
    #              independently of any motion. May have an empty-
    #              frame thumbnail — the thumbnail is the receipt of
    #              what the camera could see at that moment, even
    #              when the answer is "nothing."
    #   'fused'  — created by the event fusion layer when a vision
    #              event and an audio event within ~2 seconds of each
    #              other collapsed into a single Inbox row.
    await _migrate_add_column(
        conn, "motion_events", "source",
        "TEXT NOT NULL DEFAULT 'vision'",
    )
    # fused_parent_id: when fusion collapses two source events into
    # a fused row, the source rows point at the fused parent via this
    # column. Inbox queries filter out rows where this is non-null so
    # the user sees the fused view rather than two duplicated rows.
    await _migrate_add_column(conn, "motion_events", "fused_parent_id", "TEXT")
    # summary: brief one-liner for the Inbox row ("Blue sedan drove past").
    # description: detailed CSV for search + template engine ("blue sedan,
    # driving left to right, residential street, sunny"). Both written
    # async by the summarizer manager after YOLOX labeling completes.
    # NULL when the summarizer is unavailable or the IR gate fired.
    await _migrate_add_column(conn, "motion_events", "summary", "TEXT")
    await _migrate_add_column(conn, "motion_events", "description", "TEXT")
    # Seed default settings if not present
    for key, value in DEFAULT_SETTINGS.items():
        await conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )
    await conn.commit()
    return conn


async def get_setting(conn: aiosqlite.Connection, key: str) -> str | None:
    cursor = await conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = await cursor.fetchone()
    return row["value"] if row else None


async def set_setting(conn: aiosqlite.Connection, key: str, value: str) -> None:
    await conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    await conn.commit()


async def get_all_settings(conn: aiosqlite.Connection) -> dict[str, str]:
    cursor = await conn.execute("SELECT key, value FROM settings")
    rows = await cursor.fetchall()
    return {r["key"]: r["value"] for r in rows}


def _row_to_camera(row: aiosqlite.Row) -> Camera:
    d = dict(row)
    d["resolutions"] = json.loads(d["resolutions"])
    return Camera(**d)


async def get_all_cameras(conn: aiosqlite.Connection) -> list[Camera]:
    cursor = await conn.execute("SELECT * FROM cameras ORDER BY first_seen")
    rows = await cursor.fetchall()
    return [_row_to_camera(r) for r in rows]


async def get_camera(conn: aiosqlite.Connection, camera_id: str) -> Camera | None:
    cursor = await conn.execute("SELECT * FROM cameras WHERE id = ?", (camera_id,))
    row = await cursor.fetchone()
    return _row_to_camera(row) if row else None


async def get_camera_by_ip(conn: aiosqlite.Connection, ip: str) -> Camera | None:
    cursor = await conn.execute("SELECT * FROM cameras WHERE ip = ?", (ip,))
    row = await cursor.fetchone()
    return _row_to_camera(row) if row else None


async def upsert_camera(conn: aiosqlite.Connection, camera: Camera) -> Camera:
    await conn.execute(
        """
        INSERT INTO cameras (
            id, ip, xaddr, manufacturer, model, firmware, serial_number,
            hardware_id, resolutions, rtsp_uri, substream_uri, status, username, password,
            name, first_seen, last_seen, device_type, parent_hub_id,
            hostname, mac_address, identification_source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ip) DO UPDATE SET
            xaddr = excluded.xaddr,
            manufacturer = COALESCE(excluded.manufacturer, cameras.manufacturer),
            model = COALESCE(excluded.model, cameras.model),
            firmware = COALESCE(excluded.firmware, cameras.firmware),
            serial_number = COALESCE(excluded.serial_number, cameras.serial_number),
            hardware_id = COALESCE(excluded.hardware_id, cameras.hardware_id),
            resolutions = CASE WHEN excluded.resolutions != '[]'
                          THEN excluded.resolutions ELSE cameras.resolutions END,
            rtsp_uri = COALESCE(excluded.rtsp_uri, cameras.rtsp_uri),
            substream_uri = COALESCE(excluded.substream_uri, cameras.substream_uri),
            status = excluded.status,
            username = COALESCE(excluded.username, cameras.username),
            password = COALESCE(excluded.password, cameras.password),
            name = COALESCE(cameras.name, excluded.name),
            last_seen = excluded.last_seen,
            device_type = excluded.device_type,
            parent_hub_id = COALESCE(excluded.parent_hub_id, cameras.parent_hub_id),
            hostname = COALESCE(excluded.hostname, cameras.hostname),
            mac_address = COALESCE(excluded.mac_address, cameras.mac_address),
            identification_source = COALESCE(excluded.identification_source, cameras.identification_source)
        """,
        (
            camera.id,
            camera.ip,
            camera.xaddr,
            camera.manufacturer,
            camera.model,
            camera.firmware,
            camera.serial_number,
            camera.hardware_id,
            json.dumps(camera.resolutions),
            camera.rtsp_uri,
            camera.substream_uri,
            camera.status,
            camera.username,
            camera.password,
            camera.name,
            camera.first_seen.isoformat(),
            camera.last_seen.isoformat(),
            camera.device_type,
            camera.parent_hub_id,
            camera.hostname,
            camera.mac_address,
            camera.identification_source,
        ),
    )
    await conn.commit()
    # Re-read to get the merged result
    return (await get_camera_by_ip(conn, camera.ip)) or camera


async def update_camera_auth(
    conn: aiosqlite.Connection,
    camera_id: str,
    username: str,
    password: str,
    status: str = "online",
) -> Camera | None:
    await conn.execute(
        "UPDATE cameras SET username = ?, password = ?, status = ? WHERE id = ?",
        (username, password, status, camera_id),
    )
    await conn.commit()
    return await get_camera(conn, camera_id)


async def update_camera_name(
    conn: aiosqlite.Connection, camera_id: str, name: str
) -> Camera | None:
    await conn.execute(
        "UPDATE cameras SET name = ? WHERE id = ?", (name, camera_id)
    )
    await conn.commit()
    return await get_camera(conn, camera_id)


async def mark_camera_offline(conn: aiosqlite.Connection, camera_id: str) -> None:
    await conn.execute(
        "UPDATE cameras SET status = 'offline' WHERE id = ?", (camera_id,)
    )
    await conn.commit()


async def delete_camera(conn: aiosqlite.Connection, camera_id: str) -> bool:
    """Permanently remove a camera row and its dependent bookkeeping.

    Returns True if a row existed and was deleted, False if the id
    was not found.

    SQLite doesn't ship with ON DELETE CASCADE enabled by default and
    we rely on explicit cleanup here so the caller doesn't need to
    know about the dependent tables. Kept in-sync with init_db() —
    any new table with a camera_id FK needs a companion delete
    here.
    """
    row = await (
        await conn.execute("SELECT id FROM cameras WHERE id = ?", (camera_id,))
    ).fetchone()
    if row is None:
        return False
    # Delete dependents before the parent. recordings + motion_events
    # hold historical rows; tracked_events is the classifier output.
    await conn.execute(
        "DELETE FROM tracked_events WHERE camera_id = ?", (camera_id,)
    )
    await conn.execute(
        "DELETE FROM motion_events WHERE camera_id = ?", (camera_id,)
    )
    await conn.execute(
        "DELETE FROM recordings WHERE camera_id = ?", (camera_id,)
    )
    await conn.execute("DELETE FROM cameras WHERE id = ?", (camera_id,))
    await conn.commit()
    return True


# ----- Recording helpers -----

async def insert_recording(
    conn: aiosqlite.Connection,
    recording_id: str,
    camera_id: str,
    started_at: str,
    file_path: str,
) -> None:
    await conn.execute(
        "INSERT INTO recordings (id, camera_id, started_at, file_path, in_progress) "
        "VALUES (?, ?, ?, ?, 1)",
        (recording_id, camera_id, started_at, file_path),
    )
    await conn.commit()


async def complete_recording(
    conn: aiosqlite.Connection,
    file_path: str,
    ended_at: str,
    file_bytes: int,
    duration_s: float,
    bitrate_bps: int,
) -> None:
    await conn.execute(
        "UPDATE recordings SET ended_at = ?, file_bytes = ?, duration_s = ?, "
        "bitrate_bps = ?, in_progress = 0 WHERE file_path = ?",
        (ended_at, file_bytes, duration_s, bitrate_bps, file_path),
    )
    await conn.commit()


async def get_oldest_recordings(
    conn: aiosqlite.Connection, limit: int = 50
) -> list[dict]:
    cursor = await conn.execute(
        "SELECT * FROM recordings WHERE in_progress = 0 "
        "ORDER BY started_at ASC LIMIT ?",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def delete_recording(conn: aiosqlite.Connection, recording_id: str) -> None:
    await conn.execute("DELETE FROM recordings WHERE id = ?", (recording_id,))
    await conn.commit()


async def get_total_used_bytes(conn: aiosqlite.Connection) -> int:
    cursor = await conn.execute(
        "SELECT COALESCE(SUM(file_bytes), 0) AS total FROM recordings"
    )
    row = await cursor.fetchone()
    return int(row["total"]) if row else 0


async def get_recording_for_track(
    conn: aiosqlite.Connection,
    camera_id: str,
    track_timestamp: str,
) -> dict | None:
    """Return the recording segment that covers *track_timestamp*.

    Finds the segment that started at or before the timestamp and either
    ended after it or is still being written (in_progress=1).
    """
    cursor = await conn.execute(
        "SELECT id, camera_id, started_at, ended_at, file_path, "
        "       in_progress, duration_s "
        "FROM recordings "
        "WHERE camera_id = ? "
        "  AND started_at <= ? "
        "  AND (ended_at >= ? OR in_progress = 1) "
        "ORDER BY started_at DESC "
        "LIMIT 1",
        (camera_id, track_timestamp, track_timestamp),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_recent_completed_recordings(
    conn: aiosqlite.Connection, limit: int
) -> list[dict]:
    """Last N completed segments across all cameras, newest first.

    Used for empirical bitrate estimation. Only returns rows that have a
    duration so the caller can divide bytes by seconds safely.
    """
    cursor = await conn.execute(
        "SELECT file_bytes, duration_s, started_at, ended_at "
        "FROM recordings "
        "WHERE in_progress = 0 AND duration_s IS NOT NULL AND duration_s > 0 "
        "ORDER BY ended_at DESC LIMIT ?",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_recording_count(conn: aiosqlite.Connection) -> int:
    cursor = await conn.execute("SELECT COUNT(*) AS n FROM recordings")
    row = await cursor.fetchone()
    return int(row["n"]) if row else 0


async def cleanup_orphan_in_progress(conn: aiosqlite.Connection) -> None:
    """On startup: any in_progress=1 rows are from a crashed previous run."""
    await conn.execute("DELETE FROM recordings WHERE in_progress = 1")
    await conn.commit()


async def get_recording_dates(
    conn: aiosqlite.Connection, camera_id: str | None = None
) -> list[str]:
    """Return distinct dates (YYYY-MM-DD) that have recordings, newest first."""
    if camera_id:
        cursor = await conn.execute(
            "SELECT DISTINCT substr(started_at, 1, 10) AS date "
            "FROM recordings WHERE camera_id = ? ORDER BY date DESC",
            (camera_id,),
        )
    else:
        cursor = await conn.execute(
            "SELECT DISTINCT substr(started_at, 1, 10) AS date "
            "FROM recordings ORDER BY date DESC"
        )
    rows = await cursor.fetchall()
    return [r["date"] for r in rows]


async def get_recordings_for_date(
    conn: aiosqlite.Connection, camera_id: str, date: str
) -> list[dict]:
    """Get all recordings for a camera on a specific date, oldest first."""
    cursor = await conn.execute(
        "SELECT * FROM recordings "
        "WHERE camera_id = ? AND substr(started_at, 1, 10) = ? "
        "ORDER BY started_at ASC",
        (camera_id, date),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def insert_motion_event(
    conn: aiosqlite.Connection,
    event_id: str,
    camera_id: str,
    started_at: str,
    thumbnail_path: str | None,
) -> None:
    await conn.execute(
        "INSERT INTO motion_events (id, camera_id, started_at, thumbnail_path) "
        "VALUES (?, ?, ?, ?)",
        (event_id, camera_id, started_at, thumbnail_path),
    )
    await conn.commit()


async def complete_motion_event(
    conn: aiosqlite.Connection, event_id: str, ended_at: str
) -> None:
    await conn.execute(
        "UPDATE motion_events SET ended_at = ? WHERE id = ?",
        (ended_at, event_id),
    )
    await conn.commit()


async def get_motion_events_for_date(
    conn: aiosqlite.Connection, camera_id: str, date: str
) -> list[dict]:
    cursor = await conn.execute(
        "SELECT * FROM motion_events "
        "WHERE camera_id = ? AND substr(started_at, 1, 10) = ? "
        "ORDER BY started_at ASC",
        (camera_id, date),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_recent_motion_events(
    conn: aiosqlite.Connection,
    limit: int = 20,
    labeled_only: bool = True,
    object_class: str | None = None,
    camera_id: str | None = None,
    started_after: str | None = None,
    ended_before: str | None = None,
) -> list[dict]:
    clauses: list[str] = []
    params: list[object] = []
    if labeled_only:
        clauses.append("object_class IS NOT NULL")
    if object_class is not None:
        clauses.append("object_class = ?")
        params.append(object_class)
    if camera_id is not None:
        clauses.append("camera_id = ?")
        params.append(camera_id)
    if started_after is not None:
        clauses.append("started_at >= ?")
        params.append(started_after)
    if ended_before is not None:
        clauses.append("started_at < ?")
        params.append(ended_before)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    cursor = await conn.execute(
        f"SELECT * FROM motion_events {where} ORDER BY started_at DESC LIMIT ?",
        params,
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_motion_event_by_id(
    conn: aiosqlite.Connection, event_id: str
) -> dict | None:
    cursor = await conn.execute(
        "SELECT * FROM motion_events WHERE id = ?", (event_id,)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_recording_by_id(
    conn: aiosqlite.Connection, recording_id: str
) -> dict | None:
    cursor = await conn.execute(
        "SELECT * FROM recordings WHERE id = ?", (recording_id,)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


# ----- Classification helpers -----

async def update_motion_event_classification(
    conn: aiosqlite.Connection,
    event_id: str,
    object_class: str | None,
    object_confidence: float | None,
) -> None:
    """Write the classifier's verdict onto an existing motion event.

    Called by classification.manager after median-of-track scoring
    picks a winning label. object_class=None + confidence=None is a
    first-class result — it signals silent fallback to "Motion at X"
    and the Inbox renders accordingly.
    """
    await conn.execute(
        "UPDATE motion_events SET object_class = ?, object_confidence = ? "
        "WHERE id = ?",
        (object_class, object_confidence, event_id),
    )
    await conn.commit()


async def update_motion_event_sound(
    conn: aiosqlite.Connection,
    event_id: str,
    sound_class: str | None,
    sound_confidence: float | None,
) -> None:
    """Write the audio classifier's verdict onto an existing event.

    Audio labels can land on an event that was originally created
    by the vision path (enrichment — low-priority classes only).
    For high-priority audio-only events (glass_break/gunshot/scream/
    siren firing with no concurrent motion), the audio.manager
    creates a fresh motion_events row with source='audio' first and
    then calls this to populate the label.
    """
    await conn.execute(
        "UPDATE motion_events SET sound_class = ?, sound_confidence = ? "
        "WHERE id = ?",
        (sound_class, sound_confidence, event_id),
    )
    await conn.commit()


async def insert_tracked_event(
    conn: aiosqlite.Connection,
    tracked_id: str,
    camera_id: str,
    motion_event_id: str | None,
    started_at: str,
    ended_at: str,
    frame_count: int,
    bbox_json: str | None,
    bbox_history_json: str | None,
    thumbnail_path: str | None,
) -> None:
    """Persist one IOU-tracker-closed track. Called at track-close
    time, before the classifier runs — object_class is filled in
    later via update_tracked_event_classification().
    """
    await conn.execute(
        "INSERT INTO tracked_events "
        "(id, camera_id, motion_event_id, started_at, ended_at, "
        " frame_count, bbox_json, bbox_history_json, thumbnail_path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            tracked_id, camera_id, motion_event_id,
            started_at, ended_at, frame_count,
            bbox_json, bbox_history_json, thumbnail_path,
        ),
    )
    await conn.commit()


async def update_tracked_event_classification(
    conn: aiosqlite.Connection,
    tracked_id: str,
    object_class: str | None,
    object_confidence: float | None,
) -> None:
    """Populate a tracked_events row with the classifier's label."""
    await conn.execute(
        "UPDATE tracked_events SET object_class = ?, object_confidence = ? "
        "WHERE id = ?",
        (object_class, object_confidence, tracked_id),
    )
    await conn.commit()


async def get_tracked_events_for_motion_event(
    conn: aiosqlite.Connection, motion_event_id: str
) -> list[dict]:
    """All tracks that belonged to a given motion window. A single
    motion event can have 0, 1, or many tracked rows — 0 if the
    motion was below the promotion gate (leaf-jiggle), many if the
    scene had multiple concurrent moving objects.
    """
    cursor = await conn.execute(
        "SELECT * FROM tracked_events WHERE motion_event_id = ? "
        "ORDER BY started_at ASC",
        (motion_event_id,),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_recent_tracked_events(
    conn: aiosqlite.Connection, limit: int = 50
) -> list[dict]:
    cursor = await conn.execute(
        "SELECT * FROM tracked_events ORDER BY started_at DESC LIMIT ?",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]
