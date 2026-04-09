"""
Live preview stream endpoints — proxy go2rtc's live stream outputs
through to the browser as native-playable HTTP streams.

Lineage:
  - v1 (pre-unified): one ffmpeg per browser client, each opening its
    own RTSP connection. Failed on single-client cameras.
  - v2 (unified MJPEG): the recorder owned the sole RTSP connection and
    fanned 10fps MJPEG frames out to a FrameBroadcaster, which
    browsers consumed as multipart/x-mixed-replace. Worked most of the
    time but was fragile: any sync loss in the hand-rolled SOI/EOI
    demuxer or the browser's multipart parser would wedge a tile
    indefinitely with no recovery. Observed live 2026-04-09.
  - v3 (this module, current): the recorder still owns recording +
    motion-detection paths, but live preview is handed off to go2rtc.
    Every camera is registered with go2rtc at scanner startup (via
    backend/discovery/scanner.py). go2rtc exposes every registered
    stream as a live fragmented MP4 over HTTP via
    `GET /api/stream.mp4?src={name}`, which browsers play natively
    in a <video> element with a few seconds of startup latency and
    built-in reconnect behavior. We proxy that through FastAPI so
    the frontend only needs to know one origin.

The unified ffmpeg pipeline's preview output (the fifo/mjpeg/TCP
branch) is vestigial as of this commit and will be removed once the
frontend has been verified on the new path.
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse

from .. import db, go2rtc_client

logger = logging.getLogger(__name__)

router = APIRouter(tags=["streams"])

BOUNDARY = "frame"

# Heartbeat for the disconnect-detection loop. We do not block forever
# on queue.get() because if no frames arrive (camera offline, ffmpeg
# stalled) we still need to notice when the browser closes the request.
PREVIEW_QUEUE_TIMEOUT_S = 2.0

# Chunk size for proxying the live MP4 stream from go2rtc. 64 KB is a
# standard TCP-friendly chunk that keeps latency low without thrashing
# the event loop on high-bitrate streams.
_LIVE_PROXY_CHUNK_SIZE = 64 * 1024


async def _multipart_stream(
    request: Request, recorder
) -> AsyncIterator[bytes]:
    """Subscribe to the recorder's preview broadcaster and yield
    multipart/x-mixed-replace frame chunks until the client disconnects."""
    queue = recorder.subscribe_preview()
    try:
        while True:
            if await request.is_disconnected():
                return
            try:
                frame = await asyncio.wait_for(
                    queue.get(), timeout=PREVIEW_QUEUE_TIMEOUT_S
                )
            except asyncio.TimeoutError:
                # No frame in the timeout window — loop and re-check
                # the client connection state. Keeps the response alive
                # while the camera is briefly silent.
                continue
            # None is the FrameBroadcaster EOF sentinel, published by
            # close() when the upstream recorder is stopped. Exit the
            # streaming loop cleanly so the browser's <img> sees the
            # HTTP response end and reconnects to whatever recorder
            # is live now. Without this, the old code would keep
            # looping forever on timeouts, silently dropping the tile
            # for ~15 seconds (the browser-side hasFirstFrame timer).
            if frame is None:
                return
            yield (
                f"--{BOUNDARY}\r\n"
                f"Content-Type: image/jpeg\r\n"
                f"Content-Length: {len(frame)}\r\n\r\n"
            ).encode()
            yield frame
            yield b"\r\n"
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(
            "Preview stream error for camera %s: %s",
            getattr(recorder, "camera", None) and recorder.camera.id,
            e,
        )
    finally:
        recorder.unsubscribe_preview(queue)


@router.get("/cameras/{camera_id}/live.m3u8")
async def live_m3u8(camera_id: str, request: Request):
    """
    Live HLS master playlist for a camera, proxied from go2rtc's
    `/api/stream.m3u8?src={name}` endpoint.

    The frontend's hls.js player fetches this, which points at a
    child playlist at the relative URL `hls/playlist.m3u8?id=XXX`.
    That relative URL resolves against THIS endpoint's path, landing
    on `/api/cameras/{id}/hls/playlist.m3u8?id=XXX` — served by
    live_hls_path() below. From there hls.js follows segment URLs
    the same way. The browser only ever talks to the FastAPI origin;
    go2rtc is an internal implementation detail.

    Returns:
      - 404 if the camera doesn't exist in the DB
      - 409 if the camera is not authenticated
      - 503 if go2rtc is not available
      - Otherwise the HLS master playlist body with content-type
        application/vnd.apple.mpegurl
    """
    conn = request.app.state.db
    camera = await db.get_camera(conn, camera_id)

    if not camera:
        return Response(status_code=404, content=b"Camera not found")
    if not camera.rtsp_uri:
        return Response(
            status_code=409, content=b"Camera not authenticated"
        )
    if not go2rtc_client.is_enabled():
        return Response(
            status_code=503,
            content=(
                b"go2rtc is not available - live preview requires it. "
                b"Check the backend startup log for 'dev go2rtc ready' "
                b"or the tether env vars in Tauri mode."
            ),
        )

    base = go2rtc_client.api_base()
    upstream_url = f"{base}/api/stream.m3u8?src={camera_id}"

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(5.0)
        ) as client:
            resp = await client.get(upstream_url)
    except Exception as e:
        logger.warning("HLS master fetch failed for %s: %s", camera_id, e)
        return Response(
            status_code=502, content=b"Upstream HLS fetch failed"
        )

    if resp.status_code >= 400:
        return Response(
            status_code=resp.status_code,
            content=resp.content,
        )

    return Response(
        content=resp.content,
        media_type="application/vnd.apple.mpegurl",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )


async def _hls_binary_proxy(
    request: Request, upstream_url: str
) -> AsyncIterator[bytes]:
    """Stream TS segment bytes from go2rtc to the browser, aborting
    on client disconnect.

    TS segments are small (a few hundred KB at most at 500ms cadence)
    but we stream them rather than buffer to minimize latency between
    go2rtc producing a segment and hls.js appending it to the media
    source buffer. Client-disconnect check on every chunk keeps an
    idle browser tab from pinning a per-camera RTSP session.
    """
    timeout = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("GET", upstream_url) as resp:
                if resp.status_code >= 400:
                    logger.warning(
                        "HLS segment upstream returned %d: %s",
                        resp.status_code, upstream_url,
                    )
                    return
                async for chunk in resp.aiter_bytes(
                    chunk_size=_LIVE_PROXY_CHUNK_SIZE
                ):
                    if await request.is_disconnected():
                        return
                    if chunk:
                        yield chunk
    except (httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError):
        return
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error("HLS segment proxy error for %s: %s", upstream_url, e)


@router.get("/cameras/{camera_id}/hls/{subpath:path}")
async def live_hls_path(camera_id: str, subpath: str, request: Request):
    """
    Proxy HLS child playlists and TS segments through to go2rtc.

    hls.js follows relative URLs from the master playlist: starting
    with `hls/playlist.m3u8?id=XXX`, then each segment reference
    like `segment.ts?id=XXX&n=N`. These all resolve against the
    master's URL path (`/api/cameras/{id}/live.m3u8`), so they land
    here under `/api/cameras/{id}/hls/{subpath}`. We forward the
    exact subpath plus query string to go2rtc's `/api/hls/{subpath}`.

    The camera_id in the URL is not actually used by go2rtc on the
    child endpoints (go2rtc keys child requests by the `id` query
    param that the master playlist embedded), but we keep it in our
    URL shape for clean scoping and for future access-control
    decisions.
    """
    if not go2rtc_client.is_enabled():
        return Response(status_code=503, content=b"go2rtc not available")

    base = go2rtc_client.api_base()
    # Forward subpath + original query string verbatim. go2rtc's
    # child URLs use `?id=` for the session token and `&n=` for the
    # segment number; we pass them through unchanged.
    query = request.url.query
    upstream_url = f"{base}/api/hls/{subpath}"
    if query:
        upstream_url = f"{upstream_url}?{query}"

    # The child playlist (.m3u8) is text and small; the segments
    # (.ts) are binary and also small but streamable for latency.
    # Both are handled by the same streaming generator — content
    # type is inferred from the subpath suffix.
    if subpath.endswith(".m3u8"):
        media_type = "application/vnd.apple.mpegurl"
    elif subpath.endswith(".ts"):
        media_type = "video/mp2t"
    else:
        media_type = "application/octet-stream"

    return StreamingResponse(
        _hls_binary_proxy(request, upstream_url),
        media_type=media_type,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )


@router.get("/cameras/{camera_id}/stream.mjpeg")
async def stream_camera(camera_id: str, request: Request):
    """
    Live MJPEG stream from a camera, served from the recorder's unified
    pipeline. The recorder is the sole owner of the camera's RTSP
    connection — this endpoint is a passive subscriber.

    Returns 503 if the camera is not currently being recorded (the
    unified pipeline only exists while the recorder is running).
    """
    conn = request.app.state.db
    camera = await db.get_camera(conn, camera_id)

    if not camera:
        return Response(status_code=404, content=b"Camera not found")

    if not camera.rtsp_uri:
        return Response(status_code=409, content=b"Camera not authenticated")

    recorder_mgr = request.app.state.recorder
    recorder = recorder_mgr.recorders.get(camera_id)
    if recorder is None or not recorder.is_running:
        return Response(
            status_code=503,
            content=b"Camera pipeline not running (recording disabled?)",
        )

    return StreamingResponse(
        _multipart_stream(request, recorder),
        media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )


@router.get("/cameras/{camera_id}/snapshot.jpg")
async def snapshot_camera(camera_id: str, request: Request):
    """
    Return the most recent preview frame for a camera as a single JPEG.

    Zero-copy: pulls the latest frame already buffered in the
    FrameBroadcaster, no new RTSP connection or decode work. Used by
    the Name Cameras screen so the user can see what each camera
    actually sees while assigning a friendly name, and by the Inbox
    "Connecting…" overlay as a fallback last-known thumbnail.

    Returns 503 if the recorder isn't running or no frame has been
    published yet (brand-new camera, first few seconds of startup).
    """
    conn = request.app.state.db
    camera = await db.get_camera(conn, camera_id)

    if not camera:
        return Response(status_code=404, content=b"Camera not found")

    recorder_mgr = request.app.state.recorder
    recorder = recorder_mgr.recorders.get(camera_id)
    if recorder is None or not recorder.is_running:
        return Response(
            status_code=503,
            content=b"Camera pipeline not running",
        )

    frame = recorder.preview_broadcaster.latest
    if frame is None:
        return Response(
            status_code=503,
            content=b"No frame available yet",
        )

    return Response(
        content=frame,
        media_type="image/jpeg",
        headers={
            # Short cache so the naming screen feels live without
            # hammering the broadcaster. 2 seconds is enough for a
            # scroll on the bulk-naming list without the browser
            # re-fetching every card every frame.
            "Cache-Control": "public, max-age=2",
        },
    )
