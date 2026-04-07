"""
Async HTTP client for the go2rtc admin API.

Phase 1 of the go2rtc adoption: SimpleNVR uses go2rtc as the single
RTSP client per camera. The discovery scanner registers each camera's
RTSP URL with go2rtc via this module, and the recording layer connects
to the loopback RTSP server go2rtc exposes for those streams instead
of opening a fresh connection to the camera. This collapses the
per-camera RTSP-client count to exactly one regardless of how many
internal SimpleNVR consumers (recording, motion, preview) attach.

The Tauri Rust shell spawns go2rtc as a sidecar before it spawns the
Python backend, and hands us its admin URL via the SIMPLENVR_GO2RTC_URL
env var (and the loopback RTSP base via SIMPLENVR_GO2RTC_RTSP_URL).
Absence of those vars is the explicit signal to fall back to direct
camera connections — every operation in this module becomes a no-op
that returns False, and the caller knows to skip the loopback path.

API surface (verified live against go2rtc v1.9.14, see openapi.yaml):
  GET    /api/streams                     -> readiness probe + listing
  PUT    /api/streams?src=<url>&name=<id> -> register new stream
  PATCH  /api/streams?src=<url>&name=<id> -> update existing stream source
  DELETE /api/streams?src=<id>            -> remove stream by name
"""

from __future__ import annotations

import asyncio
import logging
import os

import httpx

logger = logging.getLogger(__name__)


def api_base() -> str | None:
    """Return the go2rtc admin URL or None if go2rtc is not configured."""
    url = os.environ.get("SIMPLENVR_GO2RTC_URL")
    return url.rstrip("/") if url else None


def rtsp_base() -> str | None:
    """Return the go2rtc loopback RTSP URL or None if not configured."""
    url = os.environ.get("SIMPLENVR_GO2RTC_RTSP_URL")
    return url.rstrip("/") if url else None


def is_enabled() -> bool:
    """True iff the Tauri shell handed us a go2rtc admin URL."""
    return api_base() is not None


def loopback_url_for(camera_id: str) -> str | None:
    """Build the loopback RTSP URL the recorder should use as ffmpeg's
    input for `camera_id`. Returns None when go2rtc is not configured."""
    base = rtsp_base()
    if not base:
        return None
    return f"{base}/{camera_id}"


async def _client() -> httpx.AsyncClient:
    # Short timeouts: the admin API is on loopback and answers in
    # single-digit milliseconds. A long timeout would just paper over
    # an actually-dead go2rtc and delay the fallback path.
    return httpx.AsyncClient(timeout=httpx.Timeout(2.0, connect=1.0))


async def health() -> bool:
    """Single GET /api/streams probe. True on 2xx."""
    base = api_base()
    if not base:
        return False
    try:
        async with await _client() as c:
            r = await c.get(f"{base}/api/streams")
            return r.status_code < 300
    except Exception:
        return False


async def wait_for_ready(timeout_s: float = 15.0) -> bool:
    """Poll health() every 250ms until success or timeout. Returns
    True if go2rtc answered, False otherwise.

    The Tauri shell already gates Python startup on go2rtc readiness
    in production builds, so this is mostly a no-op there. It earns
    its keep in dev (manual `python -m backend.main` after starting
    go2rtc separately) and as belt-and-suspenders if the Tauri shell's
    own health-check polling has a tighter budget than ours."""
    if not is_enabled():
        return False
    deadline = asyncio.get_event_loop().time() + timeout_s
    attempts = 0
    while asyncio.get_event_loop().time() < deadline:
        attempts += 1
        if await health():
            logger.info("go2rtc admin API ready after %d probe(s)", attempts)
            return True
        await asyncio.sleep(0.25)
    logger.error(
        "go2rtc admin API did not become ready within %.1fs (%d probes)",
        timeout_s,
        attempts,
    )
    return False


async def add_stream(camera_id: str, source_url: str) -> bool:
    """Register a stream with go2rtc using the camera UUID as the name.

    PUT is idempotent for go2rtc: re-PUT-ing with the same name and
    a new source replaces the producer cleanly. We use that property
    so the scanner can call add_stream on every periodic loop without
    worrying about whether the stream already exists.

    Returns True on 2xx, False on any failure (including go2rtc being
    disabled or unreachable). Callers MUST check the return and fall
    back to direct camera connections on False."""
    base = api_base()
    if not base:
        return False
    try:
        async with await _client() as c:
            r = await c.put(
                f"{base}/api/streams",
                params={"src": source_url, "name": camera_id},
            )
            if r.status_code >= 300:
                logger.warning(
                    "go2rtc PUT /api/streams name=%s -> HTTP %d: %s",
                    camera_id,
                    r.status_code,
                    r.text[:200],
                )
                return False
            return True
    except Exception as e:
        logger.warning("go2rtc add_stream(%s) failed: %s", camera_id, e)
        return False


async def remove_stream(camera_id: str) -> bool:
    """Delete a stream from go2rtc. Stream is identified by name (the
    camera UUID), NOT by source URL — the openapi.yaml `src` parameter
    name is misleading but verified live to take the stream name."""
    base = api_base()
    if not base:
        return False
    try:
        async with await _client() as c:
            r = await c.delete(
                f"{base}/api/streams",
                params={"src": camera_id},
            )
            return r.status_code < 300
    except Exception as e:
        logger.warning("go2rtc remove_stream(%s) failed: %s", camera_id, e)
        return False


async def list_streams() -> dict:
    """Return go2rtc's stream registry as a dict, or {} on any failure."""
    base = api_base()
    if not base:
        return {}
    try:
        async with await _client() as c:
            r = await c.get(f"{base}/api/streams")
            if r.status_code >= 300:
                return {}
            return r.json() or {}
    except Exception:
        return {}
