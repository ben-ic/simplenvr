"""
MotionDetector — runs one FFmpeg subprocess per camera using a scene-change
filter to emit JPEG frames only when motion exceeds a threshold. Frames within
MOTION_DEBOUNCE_SECONDS are grouped into a single motion event.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from ..ffmpeg_path import get_ffmpeg

from asyncio import create_subprocess_exec as spawn_proc
from asyncio.subprocess import PIPE

from .. import db
from ..process_cleanup import terminate_process_group
from ..config import (
    FFMPEG_RESTART_BACKOFF,
    MOTION_DEBOUNCE_SECONDS,
    MOTION_SCENE_THRESHOLD,
    MOTION_THUMBNAILS_DIR,
)

if TYPE_CHECKING:
    import aiosqlite

    from ..api.ws import EventBus
    from ..models import Camera

logger = logging.getLogger(__name__)

JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"


class MotionDetector:
    def __init__(
        self,
        camera: "Camera",
        substream_uri: str,
        conn: "aiosqlite.Connection",
        event_bus: "EventBus",
    ):
        self.camera = camera
        self._substream_uri = substream_uri
        self._conn = conn
        self._event_bus = event_bus

        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._monitor_task: asyncio.Task | None = None
        self._closer_task: asyncio.Task | None = None
        self._running = False
        self._backoff_index = 0

        # Current event state
        self._current_event_id: str | None = None
        self._current_event_started_at: datetime | None = None
        self._last_frame_at: datetime | None = None
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._running

    def _build_cmd(self) -> list[str]:
        return [
            get_ffmpeg(),
            "-rtsp_transport", "tcp",
            "-i", self._substream_uri,
            "-vf", f"select='gt(scene,{MOTION_SCENE_THRESHOLD})',scale=320:-2",
            "-vsync", "vfr",
            "-f", "image2pipe",
            "-vcodec", "mjpeg",
            "-q:v", "5",
            "-loglevel", "error",
            "pipe:1",
        ]

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        await self._spawn()

    async def _spawn(self) -> None:
        cmd = self._build_cmd()
        logger.info(
            "Starting motion detector: %s (%s)", self.camera.ip, self.camera.id
        )
        try:
            self._proc = await spawn_proc(
                *cmd, stdout=PIPE, stderr=PIPE, limit=1024 * 1024,
                start_new_session=True,
            )
        except FileNotFoundError:
            logger.error("ffmpeg not found in PATH")
            self._running = False
            return

        self._reader_task = asyncio.create_task(self._frame_reader())
        self._monitor_task = asyncio.create_task(self._process_monitor())
        self._closer_task = asyncio.create_task(self._idle_closer())

    async def stop(self) -> None:
        self._running = False
        if self._proc is not None:
            terminate_process_group(self._proc, signal.SIGTERM)
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                terminate_process_group(self._proc, signal.SIGKILL)
                try:
                    await self._proc.wait()
                except Exception:
                    pass

        for task in (self._reader_task, self._monitor_task, self._closer_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        # Close any open event
        async with self._lock:
            if self._current_event_id is not None:
                await self._close_current_event_locked()

        self._proc = None

    async def _frame_reader(self) -> None:
        if self._proc is None or self._proc.stdout is None:
            return

        buffer = bytearray()
        try:
            while True:
                chunk = await self._proc.stdout.read(8192)
                if not chunk:
                    break
                buffer.extend(chunk)

                while True:
                    start = buffer.find(JPEG_SOI)
                    if start < 0:
                        buffer.clear()
                        break
                    end = buffer.find(JPEG_EOI, start + 2)
                    if end < 0:
                        if start > 0:
                            del buffer[:start]
                        break
                    end += 2
                    frame = bytes(buffer[start:end])
                    del buffer[:end]
                    await self._handle_frame(frame)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Motion frame reader error for %s: %s", self.camera.ip, e)

    async def _handle_frame(self, jpeg: bytes) -> None:
        now = datetime.now(timezone.utc)
        async with self._lock:
            self._last_frame_at = now
            if self._current_event_id is None:
                # Start a new event — save first frame as thumbnail
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

    async def _process_monitor(self) -> None:
        if self._proc is None:
            return
        try:
            await self._proc.wait()
        except asyncio.CancelledError:
            raise

        if self._running:
            delay = FFMPEG_RESTART_BACKOFF[
                min(self._backoff_index, len(FFMPEG_RESTART_BACKOFF) - 1)
            ]
            self._backoff_index += 1
            logger.warning(
                "Motion FFmpeg for %s exited (rc=%s), restarting in %ds",
                self.camera.ip,
                self._proc.returncode,
                delay,
            )
            await asyncio.sleep(delay)
            if self._running:
                await self._spawn()
