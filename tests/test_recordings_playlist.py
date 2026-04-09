"""
Tests for the HLS VOD playlist endpoint at
`GET /api/recordings/playlist.m3u8?camera_id&date`.

This endpoint was added in 2026-04-08 session 4 as the engine for the
rewritten "Browse all footage" view (frontend/src/components/Recordings.tsx).
It replaces the old per-segment `<video>.src` loading pattern that had a
seek race condition. The frontend uses hls.js to consume this playlist,
which is what lets clicks land on the clicked position instantly across
segment boundaries.

What these tests pin down:
  1. In-progress recordings are excluded from the playlist (the
     in-progress tail is not playable from disk until the segment
     finalizes, so it must not appear in an HLS VOD playlist).
  2. Completed rows produce one #EXTINF line each, in chronological
     order, each pointing at /api/recordings/<id>/file.
  3. #EXT-X-DISCONTINUITY appears between segments but NOT before the
     first — each segment is an independent MP4 with its own moov
     header (faststart), so discontinuities tell hls.js to reinitialize
     the decoder cleanly at each boundary.
  4. TARGETDURATION is ceil(max duration) + 1 so it's always safely
     above the longest segment.
  5. Empty (no completed rows) returns 404, not a valid-but-empty
     playlist that would cause hls.js to misbehave.
  6. Media type is application/vnd.apple.mpegurl so browsers and
     hls.js recognize it as HLS.

Run with:
    .venv/bin/python -m pytest tests/test_recordings_playlist.py -v
"""

from __future__ import annotations

import asyncio

import aiosqlite
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import db
from backend.api.recordings import router


@pytest.fixture
def app_with_db():
    """Build a minimal FastAPI app with an in-memory sqlite DB.

    Mirrors the real backend.main startup: opens aiosqlite with
    row_factory=Row, runs the schema, stashes the connection on
    app.state.db so the router's `request.app.state.db` reads resolve.
    """

    async def _setup() -> tuple[FastAPI, aiosqlite.Connection]:
        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        await conn.executescript(db.SCHEMA)
        await conn.commit()
        app = FastAPI()
        app.include_router(router, prefix="/api")
        app.state.db = conn
        return app, conn

    loop = asyncio.new_event_loop()
    try:
        app, conn = loop.run_until_complete(_setup())
        yield app, conn, loop
    finally:
        loop.run_until_complete(conn.close())
        loop.close()


def _insert_recording(
    loop: asyncio.AbstractEventLoop,
    conn: aiosqlite.Connection,
    *,
    id: str,
    camera_id: str,
    started_at: str,
    file_path: str,
    duration_s: float | None,
    in_progress: bool,
) -> None:
    """Insert a recording row directly, bypassing db.insert_recording
    (which only creates in_progress rows). We need full control over
    duration_s and in_progress for these tests."""

    async def _run() -> None:
        await conn.execute(
            "INSERT INTO recordings "
            "(id, camera_id, started_at, ended_at, file_path, "
            "file_bytes, duration_s, bitrate_bps, in_progress) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                id,
                camera_id,
                started_at,
                None if in_progress else started_at,
                file_path,
                0 if in_progress else 1024,
                duration_s,
                None,
                1 if in_progress else 0,
            ),
        )
        await conn.commit()

    loop.run_until_complete(_run())


def test_playlist_excludes_in_progress(app_with_db):
    """Only finalized segments appear. The in-progress tail is invisible."""
    app, conn, loop = app_with_db
    cam = "cam-1"
    _insert_recording(
        loop, conn,
        id="r-completed-1", camera_id=cam,
        started_at="2026-04-08T10:00:00",
        file_path="/fake/r-completed-1.mp4",
        duration_s=60.0, in_progress=False,
    )
    _insert_recording(
        loop, conn,
        id="r-inprogress", camera_id=cam,
        started_at="2026-04-08T10:01:00",
        file_path="/fake/r-inprogress.mp4",
        duration_s=30.0, in_progress=True,
    )

    client = TestClient(app)
    resp = client.get("/api/recordings/playlist.m3u8", params={
        "camera_id": cam, "date": "2026-04-08",
    })

    assert resp.status_code == 200
    body = resp.text
    assert "/api/recordings/r-completed-1/file" in body
    assert "r-inprogress" not in body
    assert body.count("#EXTINF:") == 1


def test_playlist_discontinuity_placement(app_with_db):
    """EXT-X-DISCONTINUITY appears BETWEEN segments, never before the first.

    Each finalized segment is an independent MP4 with its own moov
    (see backend/recording/codec.py +faststart muxer). The discontinuity
    tag tells hls.js to reinitialize the decoder cleanly at each
    boundary; emitting one before the first segment is spec-incorrect
    and causes some players to choke.
    """
    app, conn, loop = app_with_db
    cam = "cam-1"
    for i in range(3):
        _insert_recording(
            loop, conn,
            id=f"r-{i}", camera_id=cam,
            started_at=f"2026-04-08T10:0{i}:00",
            file_path=f"/fake/r-{i}.mp4",
            duration_s=60.0, in_progress=False,
        )

    client = TestClient(app)
    resp = client.get("/api/recordings/playlist.m3u8", params={
        "camera_id": cam, "date": "2026-04-08",
    })
    body = resp.text
    lines = body.splitlines()

    # Find index of first #EXTINF line — the discontinuity count before
    # it must be zero.
    first_extinf = next(
        i for i, line in enumerate(lines) if line.startswith("#EXTINF:")
    )
    header = lines[:first_extinf]
    assert "#EXT-X-DISCONTINUITY" not in header

    # With 3 segments we expect exactly 2 discontinuities (between each
    # adjacent pair).
    assert body.count("#EXT-X-DISCONTINUITY") == 2
    assert body.count("#EXTINF:") == 3


def test_playlist_chronological_order(app_with_db):
    """Segments appear oldest-first, preserving the order from
    db.get_recordings_for_date (which sorts by started_at ASC)."""
    app, conn, loop = app_with_db
    cam = "cam-1"
    # Insert out of chronological order to confirm the query sorts.
    _insert_recording(
        loop, conn,
        id="r-later", camera_id=cam,
        started_at="2026-04-08T12:00:00",
        file_path="/fake/later.mp4",
        duration_s=60.0, in_progress=False,
    )
    _insert_recording(
        loop, conn,
        id="r-earlier", camera_id=cam,
        started_at="2026-04-08T09:00:00",
        file_path="/fake/earlier.mp4",
        duration_s=60.0, in_progress=False,
    )

    client = TestClient(app)
    resp = client.get("/api/recordings/playlist.m3u8", params={
        "camera_id": cam, "date": "2026-04-08",
    })
    body = resp.text
    earlier_idx = body.index("/api/recordings/r-earlier/file")
    later_idx = body.index("/api/recordings/r-later/file")
    assert earlier_idx < later_idx


def test_playlist_targetduration_rounds_up(app_with_db):
    """TARGETDURATION = ceil(max duration) + 1, safely above the longest
    segment so spec-strict players don't reject the playlist."""
    app, conn, loop = app_with_db
    cam = "cam-1"
    _insert_recording(
        loop, conn,
        id="r-short", camera_id=cam,
        started_at="2026-04-08T10:00:00",
        file_path="/fake/short.mp4",
        duration_s=57.9, in_progress=False,
    )
    _insert_recording(
        loop, conn,
        id="r-long", camera_id=cam,
        started_at="2026-04-08T10:01:00",
        file_path="/fake/long.mp4",
        duration_s=62.0, in_progress=False,
    )

    client = TestClient(app)
    resp = client.get("/api/recordings/playlist.m3u8", params={
        "camera_id": cam, "date": "2026-04-08",
    })
    body = resp.text
    # ceil(62.0) + 1 == 63
    assert "#EXT-X-TARGETDURATION:63" in body


def test_playlist_empty_returns_404(app_with_db):
    """Zero completed rows → 404. A valid-but-empty playlist would
    cause hls.js to silently render a zero-length video, which is a
    worse UX than an explicit error."""
    app, conn, _loop = app_with_db
    client = TestClient(app)
    resp = client.get("/api/recordings/playlist.m3u8", params={
        "camera_id": "cam-nothing-here", "date": "2026-04-08",
    })
    assert resp.status_code == 404


def test_playlist_empty_when_only_inprogress_returns_404(app_with_db):
    """A camera with ONLY an in-progress recording (common on first
    launch) must 404, not emit an empty playlist. The frontend
    interprets 404 as 'nothing to scrub yet' and shows the empty state."""
    app, conn, loop = app_with_db
    cam = "cam-1"
    _insert_recording(
        loop, conn,
        id="r-only-inprogress", camera_id=cam,
        started_at="2026-04-08T10:00:00",
        file_path="/fake/r.mp4",
        duration_s=None, in_progress=True,
    )

    client = TestClient(app)
    resp = client.get("/api/recordings/playlist.m3u8", params={
        "camera_id": cam, "date": "2026-04-08",
    })
    assert resp.status_code == 404


def test_playlist_content_type(app_with_db):
    """Response media type is application/vnd.apple.mpegurl so hls.js
    and native Safari both recognize the payload as HLS."""
    app, conn, loop = app_with_db
    cam = "cam-1"
    _insert_recording(
        loop, conn,
        id="r-1", camera_id=cam,
        started_at="2026-04-08T10:00:00",
        file_path="/fake/r-1.mp4",
        duration_s=60.0, in_progress=False,
    )

    client = TestClient(app)
    resp = client.get("/api/recordings/playlist.m3u8", params={
        "camera_id": cam, "date": "2026-04-08",
    })
    assert resp.headers["content-type"].startswith(
        "application/vnd.apple.mpegurl"
    )


def test_playlist_header_structure(app_with_db):
    """Playlist starts with #EXTM3U and contains the VOD header tags."""
    app, conn, loop = app_with_db
    cam = "cam-1"
    _insert_recording(
        loop, conn,
        id="r-1", camera_id=cam,
        started_at="2026-04-08T10:00:00",
        file_path="/fake/r-1.mp4",
        duration_s=60.0, in_progress=False,
    )

    client = TestClient(app)
    resp = client.get("/api/recordings/playlist.m3u8", params={
        "camera_id": cam, "date": "2026-04-08",
    })
    body = resp.text
    assert body.startswith("#EXTM3U")
    assert "#EXT-X-VERSION:7" in body
    assert "#EXT-X-MEDIA-SEQUENCE:0" in body
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in body
    assert body.rstrip().endswith("#EXT-X-ENDLIST")
