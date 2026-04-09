"""
Live preview stream endpoints — now empty.

The frontend reaches go2rtc through a Vite dev proxy at /g2r/* in
dev mode (frontend/vite.config.ts), or — once shipped — an
equivalent server-side reverse proxy in Tauri production mode
(see Task #12 in the project tasks). Either way, the proxy
rewrites the Origin header to bypass go2rtc's strict Cross-Site
WebSocket protection so the browser never has to fight with
go2rtc's origin check directly.

Lineage:
  - v1 (pre-unified): one ffmpeg per browser client. Failed on
    single-RTSP-client cameras.
  - v2 (unified MJPEG): recorder owned the RTSP connection,
    fanned 10fps MJPEG frames out as multipart/x-mixed-replace.
    Fragile: any sync loss in SOI/EOI demuxer or browser parser
    wedged tiles.
  - v3 (HLS via FastAPI proxy): backend proxied go2rtc's HLS
    endpoints. Broken in dev mode because the Vite http-proxy-
    middleware between frontend and backend wrapped transient
    streaming-response failures as 502.
  - v4 (HLS direct): frontend talked to go2rtc directly via an
    absolute URL. Failed because go2rtc's HLS muxer produces TS
    segments without inline SPS/PPS for many camera streams (see
    "decode_slice_header error" findings 2026-04-09).
  - v5 (current): frontend uses go2rtc's WebRTC/MSE web component
    via vendored video-stream.js (see frontend/src/vendor/go2rtc/),
    pointed at /g2r — a Vite dev proxy that forwards to go2rtc
    on 127.0.0.1:58581 with an Origin header rewrite so go2rtc's
    strict Cross-Site WebSocket check accepts the request. Zero
    bytes of live video flow through this Python module.

This file is kept for the empty router so the main.py include
call still has a valid target, and so future camera-stream-
related endpoints (e.g. local thumbnails, debug dumps) have a
home. The Tauri production /g2r equivalent will land here when
implemented (Task #12).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["streams"])
