from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .. import db
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
        scanner = websocket.app.state.scanner
        recent_events_rows = await db.get_recent_motion_events(conn, 50)
        snapshot = DiscoveryEvent(
            type="snapshot",
            data={
                "cameras": [c.model_dump(mode="json") for c in cameras],
                "scan_status": scanner.get_status().model_dump(mode="json"),
                "recent_motion_events": [
                    _motion_row_to_event(r) for r in recent_events_rows
                ],
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
