"""
Live MJPEG stream endpoints — converts RTSP to MJPEG via FFmpeg.

Uses asyncio.create_subprocess_exec (the safe equivalent of execFile) to spawn
FFmpeg with arguments as a list — no shell interpretation, no injection risk.
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

# JPEG markers for parsing
JPEG_SOI = b"\xff\xd8"  # Start of Image
JPEG_EOI = b"\xff\xd9"  # End of Image

BOUNDARY = "frame"


def _build_ffmpeg_cmd(rtsp_uri: str, width: int = 640, fps: int = 10) -> list[str]:
    """Build FFmpeg command to convert RTSP to MJPEG stream."""
    return [
        "ffmpeg",
        "-rtsp_transport", "tcp",
        "-i", rtsp_uri,
        "-vf", f"scale={width}:-2,fps={fps}",
        "-q:v", "5",
        "-f", "mjpeg",
        "-loglevel", "error",
        "pipe:1",
    ]


async def _mjpeg_frames(
    rtsp_uri: str, width: int = 640, fps: int = 10
) -> AsyncIterator[bytes]:
    """Spawn FFmpeg and yield individual JPEG frames from its stdout."""
    cmd = _build_ffmpeg_cmd(rtsp_uri, width, fps)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    if proc.stdout is None:
        return

    buffer = bytearray()
    try:
        while True:
            chunk = await proc.stdout.read(8192)
            if not chunk:
                break
            buffer.extend(chunk)

            # Extract complete JPEG frames from buffer
            while True:
                start = buffer.find(JPEG_SOI)
                if start < 0:
                    buffer.clear()
                    break
                end = buffer.find(JPEG_EOI, start + 2)
                if end < 0:
                    # Drop bytes before SOI
                    if start > 0:
                        del buffer[:start]
                    break

                end += 2  # Include the EOI marker
                frame = bytes(buffer[start:end])
                del buffer[:end]
                yield frame
    finally:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass


async def _multipart_stream(
    rtsp_uri: str, width: int = 640, fps: int = 10
) -> AsyncIterator[bytes]:
    """Wrap JPEG frames in multipart/x-mixed-replace format for browsers."""
    try:
        async for frame in _mjpeg_frames(rtsp_uri, width, fps):
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
        logger.error("Stream error for %s: %s", rtsp_uri, e)


@router.get("/cameras/{camera_id}/stream.mjpeg")
async def stream_camera(camera_id: str, request: Request):
    """
    Live MJPEG stream from a camera's RTSP feed.

    Browser usage: <img src="/api/cameras/{id}/stream.mjpeg" />
    """
    conn = request.app.state.db
    camera = await db.get_camera(conn, camera_id)

    if not camera:
        return Response(status_code=404, content=b"Camera not found")

    if not camera.rtsp_uri:
        return Response(status_code=409, content=b"Camera not authenticated")

    return StreamingResponse(
        _multipart_stream(camera.rtsp_uri),
        media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )
