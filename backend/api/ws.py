from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .. import db
from ..models import DiscoveryEvent, utcnow

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
        # Send initial snapshot
        cameras = await db.get_all_cameras(conn)
        scanner = websocket.app.state.scanner
        snapshot = DiscoveryEvent(
            type="snapshot",
            data={
                "cameras": [c.model_dump(mode="json") for c in cameras],
                "scan_status": scanner.get_status().model_dump(mode="json"),
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
