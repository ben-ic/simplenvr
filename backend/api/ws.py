from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .. import db, go2rtc_client
from ..models import DiscoveryEvent, utcnow
from .motion import _row_to_event as _motion_row_to_event

router = APIRouter()


class EventBus:
    def __init__(self):
        self._subscribers: list[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    async def emit(self, event_type: str, data: Any):
        event = DiscoveryEvent(type=event_type, data=data, timestamp=utcnow())
        payload = event.model_dump(mode="json")
        for q in self._subscribers:
            await q.put(payload)


@router.websocket("/ws/discovery")
async def discovery_ws(websocket: WebSocket):
    await websocket.accept()

    event_bus: EventBus = websocket.app.state.event_bus
    conn = websocket.app.state.db
    queue = event_bus.subscribe()

    try:
        # Send initial snapshot. The snapshot is atomic — everything
        # the UI needs to render its first frame of every screen
        # without a follow-up REST poll must be in here, because
        # otherwise there's an inherent race between the initial
        # REST fetches firing and the backend being warmed up enough
        # to return non-empty results.
        #
        # Motion events are included because the Inbox's cold-start
        # REST poll was hitting an empty motion_events table (fresh
        # session, not enough time yet for a real event to land) and
        # rendering "Nothing new." until the user manually refreshed.
        # Seen live 2026-04-09: "I load the UI and can't see any
        # motion events, refresh fixes it." Serving recent events
        # directly from the snapshot closes that race at the
        # hydration layer instead of papering over it client-side.
        cameras = await db.get_all_cameras(conn)
        scanner = getattr(websocket.app.state, "scanner", None)
        recent_events_rows = await db.get_recent_motion_events(conn, 50, labeled_only=True)
        # Tell the frontend where to reach go2rtc for live preview
        # (WebRTC WebSocket, HLS, snapshots). We hand back a PROXY
        # PATH (never the direct go2rtc URL) because:
        #
        # 1. go2rtc rejects cross-origin WebSocket upgrades with
        #    HTTP 403 (Cross-Site WebSocket Hijacking protection).
        #    Our frontend origin is http://localhost:3000 in dev
        #    and tauri://localhost in bundled mode — neither
        #    matches go2rtc's listen address. A same-origin proxy
        #    sidesteps the origin check entirely.
        #
        # 2. Setting go2rtc's api.origin to "*" would work but
        #    expose its admin API to any malicious web page the
        #    user visits while SimpleNVR is running.
        #
        # 3. Keeping go2rtc's port as a backend implementation
        #    detail means the frontend never hardcodes a loopback
        #    port, which helps cross-platform portability.
        #
        # Two proxy implementations exist, picked by environment:
        #
        # - Dev mode (python -m backend.main + vite dev server):
        #   the Vite dev proxy at frontend/vite.config.ts catches
        #   /g2r/* on the frontend's port 3000 and forwards to
        #   go2rtc with Origin rewritten. We emit the RELATIVE
        #   path '/g2r' so the browser resolves it against the
        #   frontend origin (http://localhost:3000) and Vite's
        #   proxy matches.
        #
        # - Tauri bundled mode: there's no Vite dev server, and
        #   the WebView page origin (tauri://localhost) has no
        #   handler for /g2r/*. We emit an ABSOLUTE URL pointing
        #   at the backend's own /g2r proxy (implemented in
        #   backend/api/streams.py) so the browser connects
        #   directly to the FastAPI server on loopback. The
        #   backend port is captured into SIMPLENVR_BACKEND_PORT
        #   by main.py __main__ before uvicorn.run(); the mode
        #   discriminator is SIMPLENVR_STDIN_WATCHDOG, set by
        #   the Tauri shell when it spawns the sidecar (see
        #   src-tauri/src/lib.rs spawn_sidecar).
        #
        # Null when go2rtc is not running (production misconfig or
        # dev_go2rtc spawn failure); the frontend renders an error
        # state for live preview in that case instead of spinning.
        if not go2rtc_client.is_enabled():
            go2rtc_base_url = None
        elif os.environ.get("SIMPLENVR_STDIN_WATCHDOG") == "1":
            backend_port = os.environ.get("SIMPLENVR_BACKEND_PORT")
            if backend_port:
                go2rtc_base_url = f"http://127.0.0.1:{backend_port}/g2r"
            else:
                # Should never happen — main.py __main__ always sets
                # SIMPLENVR_BACKEND_PORT before uvicorn.run(). If we
                # somehow get here, fall back to the relative path
                # which at least won't hand the frontend a lie.
                go2rtc_base_url = "/g2r"
        else:
            go2rtc_base_url = "/g2r"
        snapshot = DiscoveryEvent(
            type="snapshot",
            data={
                "cameras": [c.model_dump(mode="json") for c in cameras],
                "scan_status": scanner.get_status().model_dump(mode="json") if scanner else {"status": "starting"},
                "recent_motion_events": [
                    _motion_row_to_event(r) for r in recent_events_rows
                ],
                "go2rtc_base_url": go2rtc_base_url,
            },
        )
        await websocket.send_json(snapshot.model_dump(mode="json"))

        # Forward events
        while True:
            event = await queue.get()
            await websocket.send_json(event)
    except WebSocketDisconnect:
        pass
    finally:
        event_bus.unsubscribe(queue)
