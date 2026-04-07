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

from ..ffmpeg_path import get_ffmpeg, get_ffprobe

from .. import db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["streams"])

# JPEG markers for parsing
JPEG_SOI = b"\xff\xd8"  # Start of Image
JPEG_EOI = b"\xff\xd9"  # End of Image

BOUNDARY = "frame"


def _build_ffmpeg_cmd(rtsp_uri: str, width: int = 640, fps: int = 10) -> list[str]:
    """Build FFmpeg command to convert RTSP to MJPEG stream.

    Uses low-latency decoder hints and a bounded probesize so first frame
    arrives faster than FFmpeg's default ~5 second probe window. We do NOT
    set -analyzeduration 0 or -fflags nobuffer here — those caused some
    cameras to fail with "Output file does not contain any stream" because
    they need at least some analyze time to detect H.264 SPS/PPS.
    """
    cmd = [
        get_ffmpeg(),
        "-flags", "low_delay",
        "-probesize", "32k",
        "-rtsp_transport", "tcp",
    ]
    if rtsp_uri.lower().startswith("rtsp://"):
        # RTSP socket I/O timeout in microseconds. -rw_timeout is rejected by
        # newer FFmpeg builds for RTSP inputs; -timeout is the supported name.
        cmd += ["-timeout", "10000000"]   # 10s socket timeout
    cmd += [
        "-i", rtsp_uri,
        "-vf", f"scale={width}:-2,fps={fps}",
        "-q:v", "5",
        "-f", "mjpeg",
        "-loglevel", "error",
        "pipe:1",
    ]
    return cmd


async def _spawn_ffmpeg(cmd: list[str]):
    spawn = getattr(asyncio, "create_subprocess_exec")
    return await spawn(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def _mjpeg_frames(
    request: Request,
    rtsp_uri: str,
    width: int = 640,
    fps: int = 10,
) -> AsyncIterator[bytes]:
    """Spawn FFmpeg with auto-restart on death until the client disconnects.

    A single FFmpeg death (transient RTSP error, packet loss spike, dropped
    camera connection) used to leave the browser staring at a permanently
    frozen frame. This restart loop respawns with exponential backoff
    (250ms->4s) so the stream self-heals.
    """
    backoff_s = 0.25
    max_backoff_s = 4.0
    while not await request.is_disconnected():
        cmd = _build_ffmpeg_cmd(rtsp_uri, width, fps)
        proc = await _spawn_ffmpeg(cmd)
        if proc.stdout is None:
            return

        produced_a_frame = False
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
                        if start > 0:
                            del buffer[:start]
                        break
                    end += 2  # Include the EOI marker
                    frame = bytes(buffer[start:end])
                    del buffer[:end]
                    produced_a_frame = True
                    yield frame
        finally:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass

        # Reset backoff on a successful run; otherwise escalate.
        if produced_a_frame:
            backoff_s = 0.25
        if await request.is_disconnected():
            return
        await asyncio.sleep(backoff_s)
        backoff_s = min(backoff_s * 2, max_backoff_s)


async def _multipart_stream(
    request: Request, rtsp_uri: str, width: int = 640, fps: int = 10
) -> AsyncIterator[bytes]:
    """Wrap JPEG frames in multipart/x-mixed-replace format for browsers."""
    try:
        async for frame in _mjpeg_frames(request, rtsp_uri, width, fps):
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


def _substream_uri_guess(rtsp_uri: str) -> str | None:
    """
    Guess the substream URI by manufacturer pattern.
    Returns None if no known pattern matches.
    """
    if rtsp_uri.endswith("/stream1"):
        return rtsp_uri[:-1] + "2"  # Tapo
    if "_main" in rtsp_uri:
        return rtsp_uri.replace("_main", "_sub")  # Reolink
    if "/Channels/101" in rtsp_uri:
        return rtsp_uri.replace("/Channels/101", "/Channels/102")  # Hikvision
    if "subtype=0" in rtsp_uri:
        return rtsp_uri.replace("subtype=0", "subtype=1")  # Dahua
    if rtsp_uri.endswith("/live0"):
        return rtsp_uri[:-1] + "1"  # Eufy / Generic
    return None


# Cache: rtsp_uri (main) → verified working URI for previews/motion
# Either the substream (if it works) or the main URI (if substream doesn't exist)
_verified_substream_cache: dict[str, str] = {}


async def _probe_uri(uri: str, timeout: float = 3.0) -> bool:
    """Quick check if an RTSP URI is reachable and serves a stream."""
    cmd = [
        get_ffprobe(),
        "-rtsp_transport", "tcp",
        "-rw_timeout", "3000000",
        "-loglevel", "error",
        "-show_streams",
        "-of", "default=noprint_wrappers=1:nokey=1",
        uri,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=timeout + 1.0
            )
            return proc.returncode == 0 and b"video" in stdout
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return False
    except Exception:
        return False


async def get_preview_uri_for_camera(camera) -> str:
    """
    Return the preview/motion URI for a camera. If ONVIF told us about an
    explicit substream during discovery, use it directly (no probing). Otherwise
    fall back to URL-pattern guessing on the main RTSP URI.
    """
    if getattr(camera, "substream_uri", None):
        return camera.substream_uri
    return await get_preview_uri(camera.rtsp_uri)


async def get_preview_uri(rtsp_uri: str) -> str:
    """
    Return the URI to use for live preview / motion detection.
    Probes the substream once and caches the result. Falls back to
    the main URI if the substream doesn't exist (e.g. Eufy /live1 → 404).
    """
    if rtsp_uri in _verified_substream_cache:
        return _verified_substream_cache[rtsp_uri]

    candidate = _substream_uri_guess(rtsp_uri)
    if candidate and candidate != rtsp_uri:
        if await _probe_uri(candidate):
            _verified_substream_cache[rtsp_uri] = candidate
            logger.info("Substream verified: %s", candidate)
            return candidate
        else:
            logger.info(
                "Substream not available, falling back to main: %s", rtsp_uri
            )

    _verified_substream_cache[rtsp_uri] = rtsp_uri
    return rtsp_uri


def _substream_uri(rtsp_uri: str) -> str:
    """
    Synchronous convenience wrapper that returns the cached preview URI.
    If not yet probed, returns the main URI as a safe default.
    """
    return _verified_substream_cache.get(rtsp_uri, rtsp_uri)


@router.get("/cameras/{camera_id}/stream.mjpeg")
async def stream_camera(camera_id: str, request: Request):
    """
    Live MJPEG stream from a camera's RTSP feed.

    Uses the camera's substream when available so it doesn't conflict
    with the recording process (most cameras only allow 1-2 concurrent
    RTSP connections).

    Browser usage: <img src="/api/cameras/{id}/stream.mjpeg" />
    """
    conn = request.app.state.db
    camera = await db.get_camera(conn, camera_id)

    if not camera:
        return Response(status_code=404, content=b"Camera not found")

    if not camera.rtsp_uri:
        return Response(status_code=409, content=b"Camera not authenticated")

    # Prefer the explicit ONVIF substream; fall back to URL-pattern guessing.
    preview_uri = await get_preview_uri_for_camera(camera)

    return StreamingResponse(
        _multipart_stream(request, preview_uri),
        media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )
