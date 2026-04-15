"""MotionManager — orchestrates Detection Pipeline v2 across all cameras.

Owns one `DFineDetector` (ORT sessions are thread-safe and multi-request
safe per ONNX runtime docs, so a single session serves every camera).
Per-camera `HeatmapLayer` state is persisted through the main aiosqlite
connection — single writer per DB file, matching SQLite's concurrency
model. Attaches a `MotionDetector` to each camera when its recorder
starts; detaches when the recorder stops or the camera is lost.

`SIMPLENVR_CLASSIFIER=off` is honored as a full kill switch: the D-FINE
session is never loaded, no detectors are attached, and the event loop
still runs so `recording_started` / `recording_stopped` events are drained
(silent no-op). This matches how capability_probe records the same env
into the settings row — both subsystems agree on a single knob.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

from .. import db, go2rtc_client
from ..classification.dfine import DFineDetector
from ..config import MOTION_THUMBNAILS_DIR
from .detector import MotionDetector
from .heatmap import HeatmapLayer

if TYPE_CHECKING:
    import aiosqlite

    from ..api.ws import EventBus
    from ..models import Camera
    from ..recording.manager import RecordingManager

logger = logging.getLogger(__name__)

# Path to the bundled D-FINE weights. Same resolution logic as the
# capability probe, but without the PyInstaller extraction branch — the
# models folder is next to this package tree in both dev and bundle.
_DFINE_MODEL_REL = ("..", "classification", "models", "dfine_n.onnx")


def _dfine_model_path() -> str:
    from pathlib import Path
    import sys
    # Frozen (PyInstaller) bundle: same directory layout under _MEIPASS.
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return str(
            Path(sys._MEIPASS)  # type: ignore[attr-defined]
            / "backend" / "classification" / "models" / "dfine_n.onnx"
        )
    return str(Path(__file__).resolve().parent.parent / "classification" / "models" / "dfine_n.onnx")


class MotionManager:
    def __init__(
        self,
        conn: "aiosqlite.Connection",
        event_bus: "EventBus",
        recording_manager: "RecordingManager",
    ) -> None:
        self._conn = conn
        self._event_bus = event_bus
        self._recording_manager = recording_manager

        self.detectors: dict[str, MotionDetector] = {}
        self._queue: asyncio.Queue | None = None

        # Lazy-initialized on first attach attempt.
        self._dfine: DFineDetector | None = None
        self._dfine_failed: bool = False

        self._disabled: bool = (
            (os.environ.get("SIMPLENVR_CLASSIFIER") or "").lower() == "off"
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        MOTION_THUMBNAILS_DIR.mkdir(parents=True, exist_ok=True)

        if self._disabled:
            logger.info(
                "MotionManager: SIMPLENVR_CLASSIFIER=off — detection disabled",
            )
        else:
            # Pre-load D-FINE so the first camera doesn't pay the ~100 ms
            # ORT session + warmup at frame-handling time.
            await self._ensure_dfine()

        # Bootstrap: any camera already recording when we start gets a
        # detector now. RecordingManager runs first in main.py's lifespan.
        if not self._disabled and self._dfine is not None:
            for camera_id, recorder in self._recording_manager.recorders.items():
                if recorder.is_running:
                    cam = await db.get_camera(self._conn, camera_id)
                    if cam is not None:
                        await self._attach_detector(cam)

        self._queue = self._event_bus.subscribe()
        try:
            while True:
                event = await self._queue.get()
                try:
                    await self._handle_event(event)
                except Exception as e:
                    logger.error(
                        "MotionManager handler error: %s", e, exc_info=True,
                    )
        except asyncio.CancelledError:
            raise
        finally:
            if self._queue:
                self._event_bus.unsubscribe(self._queue)

    async def shutdown(self) -> None:
        for camera_id in list(self.detectors.keys()):
            await self.stop_detection(camera_id)
        # Close the shared D-FINE dispatch executor so the inference
        # thread doesn't outlive the manager. ORT session itself is GC'd.
        if self._dfine is not None:
            try:
                self._dfine.close()
            except Exception:
                logger.debug("DFineDetector close failed", exc_info=True)
        self._dfine = None

    # ------------------------------------------------------------------
    # Shared-resource init (D-FINE)
    # ------------------------------------------------------------------

    async def _ensure_dfine(self) -> None:
        if self._dfine is not None or self._dfine_failed:
            return
        model_path = _dfine_model_path()
        try:
            # Off-loop: model load + warmup can take 100 ms+.
            self._dfine = await asyncio.to_thread(DFineDetector, model_path)
        except Exception as e:
            logger.error(
                "DFineDetector load failed (%s) — detection disabled: %s",
                model_path, e, exc_info=True,
            )
            self._dfine_failed = True
            return
        logger.info("DFineDetector loaded: %s", model_path)

    # ------------------------------------------------------------------
    # Event-bus dispatch
    # ------------------------------------------------------------------

    async def _handle_event(self, event: dict) -> None:
        if self._disabled or self._dfine is None:
            return
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
            if cam is not None:
                await self._attach_detector(cam)

        elif event_type in ("recording_stopped", "camera_lost"):
            camera_id = data.get("camera_id")
            if camera_id:
                await self.stop_detection(camera_id)

    # ------------------------------------------------------------------
    # Attach / detach
    # ------------------------------------------------------------------

    async def _attach_detector(self, camera: "Camera") -> None:
        if self._dfine is None:
            return
        existing = self.detectors.get(camera.id)
        if existing is not None and existing.is_running:
            return

        rtsp_url = go2rtc_client.loopback_url_for(camera.id)
        if rtsp_url is None:
            # Detection pipeline v2 is go2rtc-only — no direct-camera
            # fallback. Log once and skip this camera rather than silently
            # succeeding against a dead URL.
            logger.warning(
                "MotionDetector skipped for %s: go2rtc loopback URL unavailable",
                camera.id,
            )
            return

        # Build the heatmap now (one round-trip to load existing cells)
        # so the detector's start() path has no awaits beyond the ones
        # it already owns. Shares the main aiosqlite connection — single
        # writer per DB file, which is the point of this whole shape.
        heatmap = await HeatmapLayer.create(self._conn, camera.id)

        detector = MotionDetector(
            camera=camera,
            conn=self._conn,
            event_bus=self._event_bus,
            dfine=self._dfine,
            heatmap=heatmap,
            rtsp_url=rtsp_url,
        )
        self.detectors[camera.id] = detector
        await detector.start()

    async def start_detection(self, camera: "Camera") -> None:
        """Imperative entry point used by tests / future API callers."""
        await self._attach_detector(camera)

    async def stop_detection(self, camera_id: str) -> None:
        detector = self.detectors.pop(camera_id, None)
        if detector:
            await detector.stop()

    # ------------------------------------------------------------------
    # Audio-vision fusion entry point (called from AudioManager)
    # ------------------------------------------------------------------

    def boost_detection(self, camera_id: str) -> None:
        """Trigger the 5 s audio-boost window on the named camera's
        detector. No-op if the camera has no active detector (detection
        disabled, recorder not running, or D-FINE failed to load).
        """
        detector = self.detectors.get(camera_id)
        if detector is None:
            return
        detector.audio_boost()
