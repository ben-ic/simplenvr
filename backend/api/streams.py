"""
Live preview stream proxy.

Provides the `/g2r/*` reverse proxy that forwards browser requests
to the go2rtc sidecar on the loopback interface, with the Origin
header rewritten to match go2rtc's own listen address. go2rtc's
Cross-Site WebSocket Hijacking protection would otherwise reject
WebSocket upgrades from any other origin (http://localhost:3000 in
dev, tauri://localhost in bundled), and setting go2rtc's api.origin
to "*" would expose its admin API to any page the user happens to
visit while SimpleNVR is running.

This is the Tauri-production equivalent of the Vite dev proxy in
frontend/vite.config.ts. Both paths MUST agree on:

  1. The same /g2r path prefix
  2. The same Origin rewrite (to go2rtc's api_base())
  3. The same path allowlist (below)

so the single `go2rtc_base_url` the frontend receives from the WS
snapshot behaves identically across dev and bundled modes.

Lineage (how we got here):
  - v1 (pre-unified): one ffmpeg per browser client. Failed on
    single-RTSP-client cameras.
  - v2 (unified MJPEG): recorder owned the RTSP connection and
    fanned 10fps frames out as multipart/x-mixed-replace. Fragile.
  - v3 (HLS via FastAPI proxy): Vite http-proxy-middleware wrapped
    backend streaming failures as 502. Dev-only breakage.
  - v4 (HLS direct to go2rtc): go2rtc's HLS muxer produces TS
    segments without inline SPS/PPS for most cameras.
  - v5 (current): vendored <video-stream> web component via
    video-stream.js, pointed at /g2r — Vite dev proxy in dev,
    THIS MODULE in bundled production.
"""

from __future__ import annotations

import asyncio
import logging
import re

import httpx
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from .. import go2rtc_client

logger = logging.getLogger(__name__)

# The original streams router is kept so main.py's include call has
# a valid target. Reserved for future camera-stream-adjacent REST
# endpoints (local thumbnails, debug dumps, etc.) that live under
# /api/streams/*.
router = APIRouter(tags=["streams"])

# The /g2r proxy intentionally lives OUTSIDE the /api namespace — the
# frontend treats /g2r as a distinct prefix it can rewrite on its own
# (see frontend/vite.config.ts and the go2rtc_base_url field in the WS
# snapshot). main.py includes this router WITHOUT a prefix.
g2r_router = APIRouter(tags=["g2r"])

# Allowlist of go2rtc endpoint paths reachable through the proxy.
#
# Tight by design: only the streaming endpoints the frontend actually
# uses (ws for WebRTC/MSE handshake, stream.* for transcoded playback
# fallbacks, frame.* for snapshots). Every admin endpoint is blocked:
#
#   api/streams  — managed via go2rtc_client.py (server-side only)
#   api/config   — read/write go2rtc config
#   api/restart  — restart the process
#   api/exit     — exit the process
#   api/log      — stream log file
#
# CORS on the main FastAPI app is the primary gate against malicious
# origins reaching this proxy at all (only tauri://localhost and the
# configured dev origins pass the allowlist). The path allowlist is
# defense-in-depth against the case where a legitimate origin gets
# compromised.
_ALLOWED_G2R_PATH_RE = re.compile(
    r"^api/(ws|stream\.[a-z0-9]+|frame\.[a-z0-9]+)$"
)

# HTTP hop-by-hop headers that MUST NOT be forwarded between the two
# legs of the proxy. These describe the specific TCP hop and would
# break chunked encoding or content negotiation if blindly passed
# through. content-length/content-encoding are also stripped because
# we stream raw bytes and httpx recomputes them for the upstream leg.
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "content-encoding",
}


def _is_allowed_g2r_path(path: str) -> bool:
    """Return True iff this go2rtc path is safe to expose through
    the proxy. False triggers an HTTP 403 / WS 1008 close."""
    return bool(_ALLOWED_G2R_PATH_RE.match(path.strip("/")))


def _go2rtc_host(base: str) -> str:
    """Extract the host:port portion of the go2rtc base URL.

    base looks like 'http://127.0.0.1:58581'. We strip the scheme
    to get '127.0.0.1:58581' for use as the Host header on the
    forwarded request. Needed because go2rtc validates Host in
    addition to Origin for its CSRF check.
    """
    return base.split("://", 1)[1]


@g2r_router.api_route(
    "/g2r/{path:path}",
    methods=["GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS"],
)
async def proxy_g2r_http(request: Request, path: str):
    """Proxy an HTTP request to go2rtc with Origin/Host rewritten.

    Mirrors the Vite dev proxy's /g2r rule from vite.config.ts: strip
    the /g2r prefix, rewrite Origin to go2rtc's own listen address
    (so the CSRF check passes), and stream the response body back to
    the caller. Used for HLS playlists, snapshot frames, and any
    future streaming endpoint on go2rtc's side.
    """
    base = go2rtc_client.api_base()
    if not base:
        raise HTTPException(status_code=503, detail="go2rtc not configured")
    if not _is_allowed_g2r_path(path):
        logger.warning("g2r http: denied path %r", path)
        raise HTTPException(status_code=403, detail="path not allowed")

    target_url = f"{base}/{path.lstrip('/')}"
    if request.url.query:
        target_url += f"?{request.url.query}"

    # Rewrite Origin and Host so go2rtc accepts us as same-origin.
    # Every other header is passed through unchanged except the
    # hop-by-hop set — those describe the client-to-backend hop,
    # not the backend-to-go2rtc hop.
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP and k.lower() not in ("host", "origin")
    }
    headers["Origin"] = base
    headers["Host"] = _go2rtc_host(base)

    # Only read the request body when the method allows one. go2rtc's
    # allowlisted endpoints are read-only, but honoring the body for
    # non-idempotent methods keeps the proxy general-purpose if the
    # allowlist ever expands.
    body = None
    if request.method not in ("GET", "HEAD", "DELETE", "OPTIONS"):
        body = await request.body()

    # Short connect timeout (loopback is fast); longer overall timeout
    # so HLS playlist fetches + snapshot captures don't trip it.
    client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0))
    try:
        upstream_req = client.build_request(
            method=request.method,
            url=target_url,
            headers=headers,
            content=body,
        )
        upstream_resp = await client.send(upstream_req, stream=True)
    except httpx.RequestError as e:
        await client.aclose()
        logger.warning("g2r http: upstream error for %s: %s", target_url, e)
        raise HTTPException(status_code=502, detail="upstream unreachable")

    async def forward():
        try:
            async for chunk in upstream_resp.aiter_raw():
                yield chunk
        finally:
            await upstream_resp.aclose()
            await client.aclose()

    # Mirror the upstream response headers back to the browser, minus
    # the hop-by-hop set. StreamingResponse adds its own transfer-
    # encoding + content-length as needed.
    response_headers = {
        k: v for k, v in upstream_resp.headers.items() if k.lower() not in _HOP_BY_HOP
    }

    return StreamingResponse(
        forward(),
        status_code=upstream_resp.status_code,
        headers=response_headers,
        media_type=upstream_resp.headers.get("content-type"),
    )


@g2r_router.websocket("/g2r/{path:path}")
async def proxy_g2r_ws(websocket: WebSocket, path: str):
    """Proxy a WebSocket upgrade to go2rtc with Origin rewritten.

    The vendored <video-stream> custom element opens a WebSocket at
    /g2r/api/ws?src=<camera_id> to run the WebRTC data channel
    handshake with go2rtc. We accept the browser's upgrade, open our
    own WebSocket client to go2rtc with Origin rewritten (so its CSRF
    check passes), and forward text/binary frames bidirectionally
    until either side disconnects.
    """
    base = go2rtc_client.api_base()
    if not base:
        await websocket.close(code=1011, reason="go2rtc not configured")
        return
    if not _is_allowed_g2r_path(path):
        logger.warning("g2r ws: denied path %r", path)
        await websocket.close(code=1008, reason="path not allowed")
        return

    # base is http://127.0.0.1:58581 → ws://127.0.0.1:58581
    ws_base = base.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
    query = websocket.url.query
    target_url = f"{ws_base}/{path.lstrip('/')}"
    if query:
        target_url += f"?{query}"

    # Imported at call time so a dev install without uvicorn[standard]
    # doesn't crash on module import — websockets ships with the
    # standard uvicorn extra we declare in requirements.txt.
    try:
        import websockets  # type: ignore
    except ImportError:
        logger.error("g2r ws: websockets library not available")
        await websocket.close(code=1011, reason="ws client unavailable")
        return

    await websocket.accept()

    try:
        async with websockets.connect(
            target_url,
            origin=base,  # type: ignore[arg-type]
            max_size=None,  # defer frame-size policy to go2rtc
            ping_interval=None,  # go2rtc runs its own keepalive
        ) as upstream:
            # Forward browser→upstream until the browser disconnects
            # or sends something we can't forward. Text frames carry
            # WebRTC signaling JSON (offer/answer/candidate); binary
            # frames carry the MSE fallback payload.
            async def browser_to_upstream() -> None:
                try:
                    while True:
                        msg = await websocket.receive()
                        if msg.get("type") == "websocket.disconnect":
                            return
                        if msg.get("text") is not None:
                            await upstream.send(msg["text"])
                        elif msg.get("bytes") is not None:
                            await upstream.send(msg["bytes"])
                except WebSocketDisconnect:
                    return
                except Exception as e:
                    logger.debug("g2r ws: browser→upstream ended: %s", e)

            # Forward upstream→browser until the upstream closes or
            # the browser socket is no longer writable.
            async def upstream_to_browser() -> None:
                try:
                    async for frame in upstream:
                        if isinstance(frame, str):
                            await websocket.send_text(frame)
                        else:
                            await websocket.send_bytes(frame)
                except Exception as e:
                    logger.debug("g2r ws: upstream→browser ended: %s", e)

            # Run both directions concurrently. When either finishes
            # (disconnect or error), cancel the other so it doesn't
            # block waiting on a closed socket. asyncio.gather WOULD
            # wait for both naturally, but a still-blocked receive on
            # the cancelled side would hang until its socket is
            # garbage-collected — cancelling eagerly is cleaner.
            browser_task = asyncio.create_task(browser_to_upstream())
            upstream_task = asyncio.create_task(upstream_to_browser())
            try:
                done, pending = await asyncio.wait(
                    {browser_task, upstream_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for task in (browser_task, upstream_task):
                    if not task.done():
                        task.cancel()
                for task in (browser_task, upstream_task):
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
    except websockets.exceptions.InvalidHandshake as e:
        logger.warning("g2r ws: upstream handshake failed: %s", e)
        try:
            await websocket.close(code=1011, reason="upstream handshake failed")
        except Exception:
            pass
    except Exception as e:
        logger.warning("g2r ws: proxy error: %s", e)
        try:
            await websocket.close(code=1011, reason="proxy error")
        except Exception:
            pass
