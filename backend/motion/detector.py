"""
MotionDetector — consumes scene-filtered JPEG frames from a camera's
unified recorder pipeline and groups them into motion events.

Previous architecture: each detector spawned its own ffmpeg with the
scene-filter, opening a second RTSP connection per camera. Single-client
cameras (like the Eufy at 10.0.0.9) couldn't tolerate that and the
motion detector starved out the live preview.

New architecture: scene filtering happens inside the recorder's single
ffmpeg process (`select='gt(scene,N)'` is one of three -map outputs).
The detector subscribes to the recorder's motion FrameBroadcaster and
runs only the event-grouping / DB-write logic in Python — no ffmpeg
subprocess, no extra RTSP connection, no concurrency conflict.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .. import db
from ..config import MOTION_DEBOUNCE_SECONDS, MOTION_THUMBNAILS_DIR

if TYPE_CHECKING:
    import aiosqlite

    from ..api.ws import EventBus
    from ..models import Camera
    from ..recording.camera_recorder import CameraRecorder

logger = logging.getLogger(__name__)


class MotionDetector:
    def __init__(
        self,
        camera: "Camera",
        recorder: "CameraRecorder",
        conn: "aiosqlite.Connection",
        event_bus: "EventBus",
    ):
        self.camera = camera
        self._recorder = recorder
        self._conn = conn
        self._event_bus = event_bus

        self._queue: asyncio.Queue | None = None
        self._consumer_task: asyncio.Task | None = None
        self._closer_task: asyncio.Task | None = None
        self._running = False

        self._current_event_id: str | None = None
        self._current_event_started_at: datetime | None = None
        self._last_frame_at: datetime | None = None
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._queue = self._recorder.subscribe_motion()
        self._consumer_task = asyncio.create_task(self._consume_loop())
        self._closer_task = asyncio.create_task(self._idle_closer())
        logger.info(
            "Motion detector subscribed: %s (%s)",
            self.camera.ip, self.camera.id,
        )

    async def stop(self) -> None:
        self._running = False
        if self._queue is not None:
            self._recorder.unsubscribe_motion(self._queue)
            self._queue = None
        for task in (self._consumer_task, self._closer_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        async with self._lock:
            if self._current_event_id is not None:
                await self._close_current_event_locked()

    async def _consume_loop(self) -> None:
        assert self._queue is not None
        try:
            while True:
                frame = await self._queue.get()
                await self._handle_frame(frame)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(
                "Motion consume loop error for %s: %s", self.camera.ip, e
            )

    async def _handle_frame(self, jpeg: bytes) -> None:
        now = datetime.now(timezone.utc)
        async with self._lock:
            self._last_frame_at = now
            if self._current_event_id is None:
                event_id = str(uuid.uuid4())
                cam_dir = MOTION_THUMBNAILS_DIR / self.camera.id
                cam_dir.mkdir(parents=True, exist_ok=True)
                thumb_path = cam_dir / f"{event_id}.jpg"
                try:
                    thumb_path.write_bytes(jpeg)
                except Exception as e:
                    logger.error("Failed to write thumbnail: %s", e)

                self._current_event_id = event_id
                self._current_event_started_at = now

                try:
                    await db.insert_motion_event(
                        self._conn,
                        event_id=event_id,
                        camera_id=self.camera.id,
                        started_at=now.isoformat(),
                        thumbnail_path=str(thumb_path),
                    )
                except Exception as e:
                    logger.error("Failed to insert motion event: %s", e)

                await self._event_bus.emit(
                    "motion_started",
                    {
                        "id": event_id,
                        "camera_id": self.camera.id,
                        "started_at": now.isoformat(),
                        "thumbnail_url": f"/api/motion_events/{event_id}/thumbnail.jpg",
                    },
                )
                logger.info(
                    "Motion started: cam=%s event=%s", self.camera.id, event_id
                )

    async def _close_current_event_locked(self) -> None:
        event_id = self._current_event_id
        ended_at = self._last_frame_at or datetime.now(timezone.utc)
        if event_id is None:
            return
        try:
            await db.complete_motion_event(
                self._conn, event_id=event_id, ended_at=ended_at.isoformat()
            )
        except Exception as e:
            logger.error("Failed to complete motion event: %s", e)
        await self._event_bus.emit(
            "motion_ended",
            {
                "id": event_id,
                "camera_id": self.camera.id,
                "ended_at": ended_at.isoformat(),
            },
        )
        logger.info("Motion ended: cam=%s event=%s", self.camera.id, event_id)
        self._current_event_id = None
        self._current_event_started_at = None

    async def _idle_closer(self) -> None:
        try:
            while True:
                await asyncio.sleep(1.0)
                async with self._lock:
                    if (
                        self._current_event_id is not None
                        and self._last_frame_at is not None
                    ):
                        idle = (
                            datetime.now(timezone.utc) - self._last_frame_at
                        ).total_seconds()
                        if idle >= MOTION_DEBOUNCE_SECONDS:
                            await self._close_current_event_locked()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Motion idle closer error for %s: %s", self.camera.ip, e)
