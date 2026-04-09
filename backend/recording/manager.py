"""
RecordingManager — orchestrates recording across all cameras.

Subscribes to the EventBus to react to camera state changes:
  - camera_found / camera_updated with status=online → start recording
  - camera_lost / status changed away from online → stop recording

Also runs the periodic storage janitor and emits storage_updated events.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
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
        # Per-camera-id set of starts currently in progress. Guards
        # the bootstrap-vs-discovery-event race where two coroutines
        # can both pass the existing-recorder check, both spawn fresh
        # recorders, and the second dict insert wins — the first
        # recorder runs orphaned with no reference and holds an RTSP
        # slot indefinitely on single-client cameras (Tapo, Eufy).
        # Membership test + add are synchronous and run between
        # awaits, so the asyncio scheduler can't interleave them.
        self._starting: set[str] = set()
        self._settings: Settings = Settings()
        self._queue: asyncio.Queue | None = None
        self._janitor_task: asyncio.Task | None = None
        # Effective recordings directory — may be a user override. Seeded
        # to the default until load_settings() runs.
        self._recordings_dir: Path = RECORDINGS_DIR

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
        stored_path = all_settings.get("recordings_path") or None
        # Declared brands and onboarding state are stored as JSON and a flag.
        # Both are optional and default to "onboarding not done, no brands
        # declared" if the keys are missing (first-launch case).
        declared_brands_raw = all_settings.get("declared_brands") or "[]"
        try:
            declared_brands = json.loads(declared_brands_raw)
            if not isinstance(declared_brands, list):
                declared_brands = []
        except Exception:
            declared_brands = []
        self._settings = Settings(
            max_storage_gb=float(all_settings.get("max_storage_gb", "10")),
            segment_duration_minutes=int(
                all_settings.get("segment_duration_minutes", "15")
            ),
            recording_enabled=all_settings.get("recording_enabled", "true") == "true",
            recording_fps=all_settings.get("recording_fps", "original"),
            recordings_path=stored_path,
            onboarding_completed=all_settings.get("onboarding_completed", "false") == "true",
            declared_brands=declared_brands,
        )
        self._recordings_dir = self._resolve_recordings_dir(stored_path)
        return self._settings

    def _resolve_recordings_dir(self, stored_path: str | None) -> Path:
        """
        Pick the effective recordings directory: user override if set and
        usable, otherwise the default under DATA_DIR. A configured-but-
        missing path (e.g. external drive unplugged) logs a warning and
        falls back to the default rather than blocking startup.
        """
        if not stored_path:
            return RECORDINGS_DIR
        candidate = Path(stored_path).expanduser()
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            if not candidate.is_dir():
                raise NotADirectoryError(str(candidate))
        except OSError as e:
            logger.warning(
                "Configured recordings_path %r unusable (%s); "
                "falling back to default %s",
                stored_path,
                e,
                RECORDINGS_DIR,
            )
            return RECORDINGS_DIR
        return candidate

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def recordings_dir(self) -> Path:
        return self._recordings_dir

    @property
    def _limit_bytes(self) -> int:
        """Storage cap in bytes, derived from the user's GB setting."""
        return int(self._settings.max_storage_gb * 1024 * 1024 * 1024)

    async def run_forever(self) -> None:
        # Orphan cleanup is only needed in bare-python development mode.
        # In bundled/Tauri mode, every ffmpeg is wrapped in the tether
        # supervisor (src-tauri/tether/) which guarantees the child
        # dies when SimpleNVR dies for any reason including SIGKILL,
        # combined with the single-instance Tauri lock — orphans are
        # structurally impossible there.
        #
        # In bare-python mode (`python -m backend.main` from a shell),
        # there is no tether binary and no single-instance lock. When
        # the developer hits Ctrl-C twice, sends SIGKILL, or the
        # process crashes, the ffmpeg children (which were spawned
        # with start_new_session=True and thus live in their own
        # process groups) survive. On next startup they show up as
        # orphans reparented to PID 1, still holding RTSP sessions on
        # the cameras, which then blocks the legitimate recorder from
        # reconnecting on a "one-client-at-a-time" camera (Tapo,
        # Eufy). Seen in the wild 2026-04-09: three generations of
        # ffmpegs piled up across bare-python restarts, causing
        # Tapo auth rejections and live-view drops. The SIMPLENVR_
        # TETHER_BIN env var is the existing Tauri-vs-bare discriminator
        # (see camera_recorder.py where it's used to decide whether
        # to wrap the spawn command), so we reuse it here.
        if not os.environ.get("SIMPLENVR_TETHER_BIN"):
            from ..process_cleanup import kill_orphan_ffmpegs
            killed = kill_orphan_ffmpegs()
            if killed:
                logger.warning(
                    "Bare-python startup sweep killed %d orphan ffmpeg "
                    "process(es) from previous session(s).",
                    killed,
                )
        await self.load_settings()

        # Clean up any orphaned in_progress rows from a previous crash
        await db.cleanup_orphan_in_progress(self._conn)
        await cleanup_orphan_files(self._recordings_dir, self._conn)

        # Bootstrap: start recording for cameras already online.
        # Cameras that are filtered out here are silent by default,
        # which is invisible for the developer ("scanner says 5
        # cameras but I only see 4 recorders"). Log every skip with
        # the specific reason so the operator immediately knows why
        # a given row isn't recording. Seen in the wild 2026-04-09:
        # Eufy at 10.0.0.9 never entered credentials, sat in DB with
        # status=needs_auth and null rtsp_uri, bootstrap silently
        # filtered it, dev saw only 4 recorders and thought they
        # were missing one.
        if self._settings.recording_enabled:
            cameras = await db.get_all_cameras(self._conn)
            for cam in cameras:
                if cam.status == "online" and cam.rtsp_uri:
                    await self.start_recording(cam)
                else:
                    reason = (
                        "needs auth (no credentials)"
                        if cam.status == "needs_auth"
                        else f"status={cam.status}"
                        if cam.status != "online"
                        else "no rtsp_uri stored"
                    )
                    logger.info(
                        "Bootstrap skipped %s (%s): %s",
                        cam.name or cam.ip,
                        cam.id,
                        reason,
                    )

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
            # Treat the event payload as a notification only — re-fetch
            # from the DB so we get fields that are excluded from the
            # serialized form (notably Camera.password, which is
            # Field(exclude=True) so it never leaks to the WebSocket).
            # Without this, authed_uri() would see password=None and
            # hand FFmpeg a credential-free URL, breaking recording.
            camera_id = cam_data.get("id")
            if not camera_id:
                return
            cam = await db.get_camera(self._conn, camera_id)
            if cam is None:
                return
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
        # Concurrent-call guard: if another coroutine is already in
        # the middle of starting this camera, bail out. The window
        # this closes is bootstrap-vs-discovery-event firing for the
        # same camera_id close to startup — without the guard, both
        # coroutines pass the existing-recorder check, both spawn
        # fresh recorders, and the second dict insert wins, leaving
        # the first recorder running orphaned with no reference and
        # holding an RTSP slot. The membership test + add below run
        # synchronously between awaits so the scheduler can't
        # interleave them.
        if camera.id in self._starting:
            return
        self._starting.add(camera.id)
        try:
            # Idempotent start: if there's already a live recorder for
            # this camera, return without touching anything. If there's
            # a stale entry (e.g. left over from a failed start, a
            # non-running instance that was never cleaned up, or an
            # exception in _spawn() that left _running False), stop it
            # cleanly before creating a new one. This is the fix for the
            # latent bug found 2026-04-09: an exception in _spawn()
            # other than FileNotFoundError would leave _running=True but
            # without a process or monitor, and the guard would forever
            # block recovery attempts — so we can never reach a clean
            # state without restarting the whole backend. By always
            # stopping the old entry, subsequent calls can create a
            # fresh recorder even if the old one was stuck.
            existing = self.recorders.get(camera.id)
            if existing is not None:
                if existing.is_running:
                    return
                try:
                    await existing.stop()
                except Exception as e:
                    logger.warning(
                        "Failed to stop stale recorder for %s: %s",
                        camera.id, e,
                    )
                # Remove the stale entry so subsequent lookups during the
                # new start see a clean slate.
                self.recorders.pop(camera.id, None)

            recorder = CameraRecorder(
                camera=camera,
                settings=self._settings,
                encoder=self._encoder,
                encoder_flags=self._encoder_flags,
                recordings_dir=self._recordings_dir,
                conn=self._conn,
                event_bus=self._event_bus,
                on_segment_complete=self._on_segment_complete,
            )
            # Start FIRST, then insert into the dict on success only.
            # The old code inserted before awaiting start(), so if start()
            # raised, the dict would hold a corrupted recorder. With this
            # order, a failed start() leaves the dict clean and the next
            # event-driven start_recording call can retry from scratch.
            try:
                await recorder.start()
            except Exception as e:
                logger.error(
                    "Failed to start recorder for %s (%s): %s",
                    camera.name or camera.ip, camera.id, e,
                )
                return
            self.recorders[camera.id] = recorder
        finally:
            self._starting.discard(camera.id)

    async def stop_recording(self, camera_id: str) -> None:
        recorder = self.recorders.pop(camera_id, None)
        if recorder:
            await recorder.stop()

    async def _emit_deleted(self, deleted: list[dict]) -> None:
        """Tell the frontend which recordings were just reaped, so any
        cached timelines for the affected cameras can be invalidated.
        Payload contains both the per-row list AND a deduped set of
        camera_ids so the frontend can take either granularity."""
        if not deleted:
            return
        camera_ids = sorted({d["camera_id"] for d in deleted})
        await self._event_bus.emit(
            "recordings_deleted",
            {
                "recordings": deleted,
                "camera_ids": camera_ids,
                "count": len(deleted),
            },
        )

    async def _on_segment_complete(
        self, camera_id: str, file_bytes: int, bitrate_bps: int
    ) -> None:
        # Enforce storage limit immediately after a new segment
        _freed, deleted = await enforce_storage_limit(self._conn, self._limit_bytes)
        await self._emit_deleted(deleted)

        # Emit storage update
        status = await compute_storage_status(self._conn, self, self._settings)
        await self._event_bus.emit(
            "storage_updated", status.model_dump(mode="json")
        )

    async def _periodic_janitor(self) -> None:
        try:
            while True:
                await asyncio.sleep(JANITOR_INTERVAL)
                _freed, deleted = await enforce_storage_limit(
                    self._conn, self._limit_bytes
                )
                await self._emit_deleted(deleted)
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
        old_recordings_dir = self._recordings_dir

        await self.load_settings()

        # Path change triggers a full restart because in-flight ffmpeg
        # children are writing to the old directory.
        path_changed = self._recordings_dir != old_recordings_dir
        needs_restart = (
            self._settings.recording_fps != old_fps
            or self._settings.segment_duration_minutes != old_segment
            or path_changed
        )

        if path_changed:
            logger.info(
                "Recordings path changed: %s -> %s",
                old_recordings_dir,
                self._recordings_dir,
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
