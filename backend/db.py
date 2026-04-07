from __future__ import annotations

import json

import aiosqlite

from .config import DATA_DIR, DB_PATH
from .models import Camera

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
    last_seen TEXT NOT NULL
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
"""

DEFAULT_SETTINGS = {
    "max_storage_gb": "10",
    "segment_duration_minutes": "15",
    "recording_enabled": "true",
    "recording_fps": "original",
}


async def _migrate_add_column(
    conn: aiosqlite.Connection, table: str, column: str, decl: str
) -> None:
    """Idempotently add a column to an existing table."""
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
            name, first_seen, last_seen
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            last_seen = excluded.last_seen
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
    conn: aiosqlite.Connection, limit: int = 20
) -> list[dict]:
    cursor = await conn.execute(
        "SELECT * FROM motion_events ORDER BY started_at DESC LIMIT ?",
        (limit,),
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
