"""
MotionManager — orchestrates motion detection across all cameras.

In the unified-pipeline architecture, motion detection runs as a Python
consumer of the recorder's motion FrameBroadcaster. There is no longer
a per-camera motion ffmpeg, no second RTSP connection. The motion
manager listens for `recording_started` / `recording_stopped` events
and attaches/detaches a MotionDetector to the corresponding recorder.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from .. import db
from ..config import MOTION_THUMBNAILS_DIR
from .detector import MotionDetector

if TYPE_CHECKING:
    import aiosqlite

    from ..api.ws import EventBus
    from ..recording.manager import RecordingManager

logger = logging.getLogger(__name__)


class MotionManager:
    def __init__(
        self,
        conn: "aiosqlite.Connection",
        event_bus: "EventBus",
        recording_manager: "RecordingManager",
    ):
        self._conn = conn
        self._event_bus = event_bus
        self._recording_manager = recording_manager
        self.detectors: dict[str, MotionDetector] = {}
        self._queue: asyncio.Queue | None = None

    async def run_forever(self) -> None:
        MOTION_THUMBNAILS_DIR.mkdir(parents=True, exist_ok=True)

        # Bootstrap: any cameras already recording when we start get a
        # detector attached immediately. (RecordingManager runs first
        # and may already have spawned recorders before we get here.)
        for camera_id, recorder in self._recording_manager.recorders.items():
            if recorder.is_running:
                cam = await db.get_camera(self._conn, camera_id)
                if cam is not None:
                    await self._attach_detector(cam, recorder)

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

        if event_type == "recording_started":
            camera_id = data.get("camera_id")
            if not camera_id:
                return
            recorder = self._recording_manager.recorders.get(camera_id)
            if recorder is None:
                return
            cam = await db.get_camera(self._conn, camera_id)
            if cam is None:
                return
            await self._attach_detector(cam, recorder)

        elif event_type == "recording_stopped":
            camera_id = data.get("camera_id")
            if camera_id:
                await self.stop_detection(camera_id)

        elif event_type == "camera_lost":
            camera_id = data.get("camera_id")
            if camera_id:
                await self.stop_detection(camera_id)

    async def _attach_detector(self, camera, recorder) -> None:
        existing = self.detectors.get(camera.id)
        if existing is not None and existing.is_running:
            return
        detector = MotionDetector(
            camera=camera,
            recorder=recorder,
            conn=self._conn,
            event_bus=self._event_bus,
        )
        self.detectors[camera.id] = detector
        await detector.start()

    # Compatibility with the old API surface (used by tests / future callers)
    async def start_detection(self, camera) -> None:
        recorder = self._recording_manager.recorders.get(camera.id)
        if recorder is None:
            return
        await self._attach_detector(camera, recorder)

    async def stop_detection(self, camera_id: str) -> None:
        detector = self.detectors.pop(camera_id, None)
        if detector:
            await detector.stop()

    async def shutdown(self) -> None:
        for camera_id in list(self.detectors.keys()):
            await self.stop_detection(camera_id)
