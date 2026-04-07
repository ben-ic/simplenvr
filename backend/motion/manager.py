"""
MotionManager — orchestrates motion detection across all cameras.
Mirrors RecordingManager structure.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from .. import db
from ..api.streams import get_preview_uri_for_camera
from ..config import MOTION_THUMBNAILS_DIR
from ..models import Camera
from .detector import MotionDetector

if TYPE_CHECKING:
    import aiosqlite

    from ..api.ws import EventBus

logger = logging.getLogger(__name__)


class MotionManager:
    def __init__(self, conn: "aiosqlite.Connection", event_bus: "EventBus"):
        self._conn = conn
        self._event_bus = event_bus
        self.detectors: dict[str, MotionDetector] = {}
        self._queue: asyncio.Queue | None = None

    async def run_forever(self) -> None:
        MOTION_THUMBNAILS_DIR.mkdir(parents=True, exist_ok=True)

        # Bootstrap: start detection for cameras already online
        cameras = await db.get_all_cameras(self._conn)
        for cam in cameras:
            if cam.status == "online" and cam.rtsp_uri:
                await self.start_detection(cam)

        self._queue = self._event_bus.subscribe()
        try:
            while True:
                event = await self._queue.get()
                try:
                    await self._handle_event(event)
                except Exception as e:
                    logger.error("Motion event handler error: %s", e, exc_info=True)
        except asyncio.CancelledError:
            raise
        finally:
            if self._queue:
                self._event_bus.unsubscribe(self._queue)

    async def _handle_event(self, event: dict) -> None:
        event_type = event.get("type")
        data = event.get("data", {})

        if event_type in ("camera_found", "camera_updated"):
            cam_data = data.get("camera")
            if not cam_data:
                return
            cam = Camera(**cam_data)
            if cam.status == "online" and cam.rtsp_uri:
                await self.start_detection(cam)
            else:
                await self.stop_detection(cam.id)

        elif event_type == "camera_lost":
            camera_id = data.get("camera_id")
            if camera_id:
                await self.stop_detection(camera_id)

    async def start_detection(self, camera: Camera) -> None:
        if camera.id in self.detectors and self.detectors[camera.id].is_running:
            return
        if not camera.rtsp_uri:
            return
        # Prefer the ONVIF-discovered substream; fall back to URL guessing.
        substream = await get_preview_uri_for_camera(camera)

        # If we end up using the main URI, run motion anyway. Cameras that
        # genuinely can't handle two simultaneous RTSP clients will visibly
        # flap recording — that's better than silently disabling motion for
        # cameras whose URLs don't match a hardcoded vendor pattern.
        if substream == camera.rtsp_uri:
            logger.info(
                "Motion using main stream for %s (%s); no substream available, "
                "recording may flap on single-client cameras",
                camera.ip,
                camera.id,
            )

        detector = MotionDetector(
            camera=camera,
            substream_uri=substream,
            conn=self._conn,
            event_bus=self._event_bus,
        )
        self.detectors[camera.id] = detector
        await detector.start()

    async def stop_detection(self, camera_id: str) -> None:
        detector = self.detectors.pop(camera_id, None)
        if detector:
            await detector.stop()

    async def shutdown(self) -> None:
        for camera_id in list(self.detectors.keys()):
            await self.stop_detection(camera_id)
