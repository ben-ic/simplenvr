"""
Live preview stream endpoints — now empty.

The frontend talks to go2rtc directly for live preview (HLS) and
snapshots (frame.jpeg). go2rtc serves `Access-Control-Allow-Origin: *`
on its admin API, so cross-origin fetches from the frontend (Vite dev
on localhost:3000, Tauri WebView on tauri://localhost) work without a
backend proxy. The frontend learns go2rtc's base URL from the WS
snapshot (see backend/api/ws.py) and uses it directly.

Lineage:
  - v1 (pre-unified): one ffmpeg per browser client. Failed on
    single-RTSP-client cameras.
  - v2 (unified MJPEG): recorder owned the RTSP connection, fanned
    10fps MJPEG frames out as multipart/x-mixed-replace. Fragile:
    any sync loss in SOI/EOI demuxer or browser parser wedged tiles.
  - v3 (HLS via FastAPI proxy): recorder registered with go2rtc,
    backend proxied go2rtc's HLS endpoints. Broken in dev mode
    because the Vite http-proxy-middleware between frontend and
    backend wrapped transient streaming-response failures as 502.
  - v4 (this module, current): frontend talks to go2rtc directly
    via the base URL provided in the WS snapshot. Backend has no
    role in live video delivery — it just tells the frontend where
    to look. Zero proxy code, zero Vite-dev interaction, zero
    httpx-timeout-tuning gymnastics.

This file is kept for the empty router so the main.py include_router
call still has a valid target and so future camera-stream-related
endpoints (e.g. local thumbnails, debug dumps) have a home.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["streams"])
