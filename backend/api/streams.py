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


async def _live_mp4_proxy(
    request: Request, upstream_url: str
) -> AsyncIterator[bytes]:
    """Stream bytes from go2rtc's live fragmented-MP4 endpoint to the
    browser, aborting on client disconnect.

    go2rtc keeps the HTTP response open indefinitely as long as the
    camera is streaming, so we need an explicit disconnect check on
    every chunk — otherwise an idle browser tab would pin a
    per-camera RTSP session forever.

    httpx's streaming API yields chunks as they arrive on the wire;
    we forward them verbatim with no buffering so the <video> element
    sees the live MP4 fragments at the same cadence go2rtc produces
    them (typically one fragment per GOP).
    """
    # No total timeout — this is a live stream. Connect timeout keeps
    # a dead go2rtc from hanging the response. Read timeout of None
    # means "don't timeout between chunks while the stream is alive".
    timeout = httpx.Timeout(connect=5.0, read=None, write=5.0, pool=5.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("GET", upstream_url) as resp:
                if resp.status_code >= 400:
                    logger.warning(
                        "go2rtc live proxy upstream returned %d: %s",
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
        # go2rtc closed the connection (camera went offline, stream
        # unregistered, etc.). Exit the generator; the browser will
        # reconnect on its own.
        return
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(
            "live proxy error for %s: %s", upstream_url, e
        )


@router.get("/cameras/{camera_id}/live.mp4")
async def live_mp4(camera_id: str, request: Request):
    """
    Live fragmented-MP4 stream of a camera, proxied from go2rtc's
    `/api/stream.mp4?src={name}` endpoint. The browser consumes this
    directly via `<video src="...">` — no hls.js, no custom player,
    just native HTML5 video. Latency is typically 2-4 seconds
    depending on camera GOP size.

    Returns:
      - 404 if the camera doesn't exist in the DB
      - 409 if the camera is not authenticated
      - 503 if go2rtc is not available (bare-python without dev
        spawner, or go2rtc failed to start)
      - Otherwise a live streaming response with media_type=video/mp4
    """
    conn = request.app.state.db
    camera = await db.get_camera(conn, camera_id)

    if not camera:
        return Response(status_code=404, content=b"Camera not found")
    if not camera.rtsp_uri:
        return Response(status_code=409, content=b"Camera not authenticated")

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
    # Stream name in go2rtc is the camera UUID — matches the
    # scanner's add_stream(camera.id, ...) registration.
    upstream_url = f"{base}/api/stream.mp4?src={camera_id}"

    return StreamingResponse(
        _live_mp4_proxy(request, upstream_url),
        media_type="video/mp4",
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
