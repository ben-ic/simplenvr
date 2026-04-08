"""
Live MJPEG stream endpoints — subscribe to the camera's unified recorder
pipeline and fan out the 10fps preview frames to the browser.

Previous architecture: each browser request spawned its own ffmpeg
process that opened a fresh RTSP connection to the camera. On
single-client cameras (Eufy at 10.0.0.9), this conflicted with the
recorder + motion detector and the preview either failed to load or
kicked one of the other ffmpegs off the camera.

New architecture: the recorder owns the only RTSP connection and
publishes 10fps MJPEG frames to a FrameBroadcaster. Each preview
request subscribes, receives frames as they arrive, and unsubscribes
on disconnect. Multiple simultaneous browser clients are supported
because the broadcaster fans out to each subscriber's own queue.
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse

from .. import db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["streams"])

BOUNDARY = "frame"

# Heartbeat for the disconnect-detection loop. We do not block forever
# on queue.get() because if no frames arrive (camera offline, ffmpeg
# stalled) we still need to notice when the browser closes the request.
PREVIEW_QUEUE_TIMEOUT_S = 2.0


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
