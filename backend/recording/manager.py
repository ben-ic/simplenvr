"""
RecordingManager — orchestrates recording across all cameras.

Subscribes to the EventBus to react to camera state changes:
  - camera_found / camera_updated with status=online → start recording
  - camera_lost / status changed away from online → stop recording

Also runs the periodic storage janitor and emits storage_updated events.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from .. import db
from ..config import JANITOR_INTERVAL, RECORDINGS_DIR
from ..models import Camera, Settings
from .camera_recorder import CameraRecorder
from .codec import select_encoder
from .janitor import cleanup_orphan_files, enforce_storage_limit
from .storage import compute_storage_status

if TYPE_CHECKING:
    import aiosqlite

    from ..api.ws import EventBus

logger = logging.getLogger(__name__)


class RecordingManager:
    def __init__(self, conn: "aiosqlite.Connection", event_bus: "EventBus"):
        self._conn = conn
        self._event_bus = event_bus
        self.recorders: dict[str, CameraRecorder] = {}
        self._settings: Settings = Settings()
        self._queue: asyncio.Queue | None = None
        self._janitor_task: asyncio.Task | None = None

        encoder_result = select_encoder()
        self._encoder: str | None
        self._encoder_flags: list[str] | None
        if encoder_result is None:
            self._encoder = None
            self._encoder_flags = None
            logger.warning(
                "No hardware H.264 encoder available on this platform. "
                "Recording will always use stream-copy regardless of fps setting."
            )
        else:
            self._encoder, self._encoder_flags = encoder_result
            logger.info(
                "RecordingManager init: encoder=%s flags=%s",
                self._encoder,
                self._encoder_flags,
            )

    async def load_settings(self) -> Settings:
        all_settings = await db.get_all_settings(self._conn)
        self._settings = Settings(
            max_storage_gb=float(all_settings.get("max_storage_gb", "10")),
            segment_duration_minutes=int(
                all_settings.get("segment_duration_minutes", "15")
            ),
            recording_enabled=all_settings.get("recording_enabled", "true") == "true",
            recording_fps=all_settings.get("recording_fps", "original"),
        )
        return self._settings

    @property
    def settings(self) -> Settings:
        return self._settings

    async def run_forever(self) -> None:
        # Belt-and-suspenders: kill any ffmpeg processes left over from a
        # previous SimpleNVR session that wasn't shut down cleanly. They
        # hold RTSP slots on cameras with low concurrent-client limits.
        from ..process_cleanup import kill_orphan_ffmpegs, kill_orphan_go2rtc
        kill_orphan_ffmpegs()
        kill_orphan_go2rtc()

        await self.load_settings()

        # Clean up any orphaned in_progress rows from a previous crash
        await db.cleanup_orphan_in_progress(self._conn)
        await cleanup_orphan_files(RECORDINGS_DIR, self._conn)

        # Bootstrap: start recording for cameras already online
        if self._settings.recording_enabled:
            cameras = await db.get_all_cameras(self._conn)
            for cam in cameras:
                if cam.status == "online" and cam.rtsp_uri:
                    await self.start_recording(cam)

        # Subscribe to event bus
        self._queue = self._event_bus.subscribe()

        # Start periodic janitor as a safety net
        self._janitor_task = asyncio.create_task(self._periodic_janitor())

        try:
            while True:
                event = await self._queue.get()
                try:
                    await self._handle_event(event)
                except Exception as e:
                    logger.error("Event handler error: %s", e, exc_info=True)
        except asyncio.CancelledError:
            raise
        finally:
            if self._janitor_task:
                self._janitor_task.cancel()
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
                if self._settings.recording_enabled:
                    await self.start_recording(cam)
            else:
                await self.stop_recording(cam.id)

        elif event_type == "camera_lost":
            camera_id = data.get("camera_id")
            if camera_id:
                await self.stop_recording(camera_id)

    async def start_recording(self, camera: Camera) -> None:
        if camera.id in self.recorders and self.recorders[camera.id].is_running:
            return

        recorder = CameraRecorder(
            camera=camera,
            settings=self._settings,
            encoder=self._encoder,
            encoder_flags=self._encoder_flags,
            recordings_dir=RECORDINGS_DIR,
            conn=self._conn,
            event_bus=self._event_bus,
            on_segment_complete=self._on_segment_complete,
        )
        self.recorders[camera.id] = recorder
        await recorder.start()

    async def stop_recording(self, camera_id: str) -> None:
        recorder = self.recorders.pop(camera_id, None)
        if recorder:
            await recorder.stop()

    async def _on_segment_complete(
        self, camera_id: str, file_bytes: int, bitrate_bps: int
    ) -> None:
        # Enforce storage limit immediately after a new segment
        limit_bytes = int(self._settings.max_storage_gb * 1024 * 1024 * 1024)
        await enforce_storage_limit(self._conn, limit_bytes)

        # Emit storage update
        status = await compute_storage_status(self._conn, self, self._settings)
        await self._event_bus.emit(
            "storage_updated", status.model_dump(mode="json")
        )

    async def _periodic_janitor(self) -> None:
        try:
            while True:
                await asyncio.sleep(JANITOR_INTERVAL)
                limit_bytes = int(
                    self._settings.max_storage_gb * 1024 * 1024 * 1024
                )
                await enforce_storage_limit(self._conn, limit_bytes)
                status = await compute_storage_status(
                    self._conn, self, self._settings
                )
                await self._event_bus.emit(
                    "storage_updated", status.model_dump(mode="json")
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Periodic janitor error: %s", e)

    async def apply_settings_change(self) -> None:
        """Reload settings and restart all recorders if recording params changed."""
        old_fps = self._settings.recording_fps
        old_segment = self._settings.segment_duration_minutes
        old_enabled = self._settings.recording_enabled

        await self.load_settings()

        needs_restart = (
            self._settings.recording_fps != old_fps
            or self._settings.segment_duration_minutes != old_segment
        )

        if not self._settings.recording_enabled:
            # Disabled — stop everything
            for camera_id in list(self.recorders.keys()):
                await self.stop_recording(camera_id)
            return

        if not old_enabled and self._settings.recording_enabled:
            # Re-enabled — start for all online cameras
            cameras = await db.get_all_cameras(self._conn)
            for cam in cameras:
                if cam.status == "online" and cam.rtsp_uri:
                    await self.start_recording(cam)
            return

        if needs_restart:
            # Restart all active recorders with new params
            active_camera_ids = list(self.recorders.keys())
            cameras = await db.get_all_cameras(self._conn)
            cameras_by_id = {c.id: c for c in cameras}

            for camera_id in active_camera_ids:
                await self.stop_recording(camera_id)

            for camera_id in active_camera_ids:
                cam = cameras_by_id.get(camera_id)
                if cam and cam.status == "online" and cam.rtsp_uri:
                    await self.start_recording(cam)

    async def shutdown(self) -> None:
        for camera_id in list(self.recorders.keys()):
            await self.stop_recording(camera_id)
