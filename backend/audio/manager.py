"""AudioManager — orchestrates audio classification across all cameras.

Symmetric with MotionManager: listens for recording lifecycle events,
subscribes to each camera's AudioBroadcaster, feeds windows to the
YAMNet classifier, and either fires independent events (high-priority
sounds) or enriches existing motion events (low-priority sounds).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from .. import db
from .classifier import YamnetClassifier
from .labelmap import is_high_priority

if TYPE_CHECKING:
    import aiosqlite

    from ..api.ws import EventBus
    from ..motion.manager import MotionManager
    from ..recording.manager import RecordingManager

logger = logging.getLogger(__name__)

# Cooldown per camera per label to prevent flood from continuous sounds
# (siren driving past, dog barking repeatedly).
_COOLDOWN_HIGH_S = 10.0
_COOLDOWN_LOW_S = 30.0

# How far back to look for a concurrent motion event to enrich.
_ENRICH_WINDOW_S = 2.0


class AudioManager:
    def __init__(
        self,
        conn: "aiosqlite.Connection",
        event_bus: "EventBus",
        recording_manager: "RecordingManager",
        motion_manager: "MotionManager | None" = None,
    ):
        self._conn = conn
        self._event_bus = event_bus
        self._recording_manager = recording_manager
        # Optional — used to trigger detection-side audio-boost (plan §7).
        # Kept optional so AudioManager stays testable in isolation.
        self._motion_manager = motion_manager
        self._classifier = YamnetClassifier()
        self._consumers: dict[str, asyncio.Task] = {}
        self._queue: asyncio.Queue | None = None
        # Cooldown tracking: (camera_id, label) → monotonic timestamp
        self._last_fired: dict[tuple[str, str], float] = {}

    @property
    def enabled(self) -> bool:
        return self._classifier.enabled

    async def start(self) -> None:
        await self._classifier.start()
        if not self._classifier.enabled:
            logger.info("Audio classification disabled — YAMNet not available")

    async def run_forever(self) -> None:
        if not self._classifier.enabled:
            return

        # Bootstrap: attach to cameras already recording with audio
        for camera_id, recorder in self._recording_manager.recorders.items():
            if recorder.is_running and recorder.audio_broadcaster is not None:
                self._start_consumer(camera_id)

        self._queue = self._event_bus.subscribe()
        try:
            while True:
                event = await self._queue.get()
                try:
                    await self._handle_event(event)
                except Exception as e:
                    logger.error("Audio event handler error: %s", e, exc_info=True)
        except asyncio.CancelledError:
            raise
        finally:
            if self._queue:
                self._event_bus.unsubscribe(self._queue)
            for task in self._consumers.values():
                task.cancel()
            self._consumers.clear()

    async def _handle_event(self, event: dict) -> None:
        event_type = event.get("type")
        data = event.get("data", {})

        if event_type == "audio_available":
            camera_id = data.get("camera_id")
            if camera_id and camera_id not in self._consumers:
                self._start_consumer(camera_id)

        elif event_type in ("recording_stopped", "camera_lost", "audio_stopped"):
            camera_id = data.get("camera_id")
            if camera_id:
                self._stop_consumer(camera_id)

    def _start_consumer(self, camera_id: str) -> None:
        recorder = self._recording_manager.recorders.get(camera_id)
        if not recorder or not recorder.audio_broadcaster:
            return
        if camera_id in self._consumers:
            return
        task = asyncio.create_task(
            self._consume(camera_id, recorder.audio_broadcaster)
        )
        self._consumers[camera_id] = task
        logger.info("Audio consumer started for %s", camera_id)

    def _stop_consumer(self, camera_id: str) -> None:
        task = self._consumers.pop(camera_id, None)
        if task:
            task.cancel()
            logger.info("Audio consumer stopped for %s", camera_id)

    async def _consume(self, camera_id: str, broadcaster) -> None:
        q = broadcaster.subscribe()
        try:
            while True:
                window = await q.get()
                if window is None:
                    break
                await self._process_window(camera_id, window)
        except asyncio.CancelledError:
            pass
        finally:
            broadcaster.unsubscribe(q)

    async def _process_window(self, camera_id: str, window: bytes) -> None:
        label, confidence = self._classifier.classify(window)
        if label is None:
            return

        # Cooldown check
        now = time.monotonic()
        cooldown = _COOLDOWN_HIGH_S if is_high_priority(label) else _COOLDOWN_LOW_S
        key = (camera_id, label)
        last = self._last_fired.get(key, 0.0)
        if now - last < cooldown:
            return
        self._last_fired[key] = now

        if is_high_priority(label):
            # Plan §7: boost the detection pipeline before firing the
            # independent event so D-FINE is already running at 5 fps
            # with a relaxed threshold when the next frame lands.
            # Best-effort — MotionManager may be None (detection
            # disabled) or the camera may have no active detector.
            if self._motion_manager is not None:
                try:
                    self._motion_manager.boost_detection(camera_id)
                except Exception:
                    logger.debug(
                        "audio-boost hand-off failed", exc_info=True,
                    )
            await self._fire_independent_event(camera_id, label, confidence)
        else:
            await self._enrich_recent_event(camera_id, label, confidence)

    async def _fire_independent_event(
        self, camera_id: str, label: str, confidence: float
    ) -> None:
        """Create a new motion_events row with source='audio' for
        high-priority sounds (glass_break, gunshot, scream, siren)."""
        event_id = str(uuid.uuid4())
        now_iso = datetime.now(timezone.utc).isoformat()

        # Grab the latest motion frame as thumbnail (may be dark/empty —
        # that's the point, the thumbnail is the "nothing visible" receipt)
        thumbnail_path = None
        recorder = self._recording_manager.recorders.get(camera_id)
        if recorder and recorder.motion_broadcaster.latest:
            from ..config import MOTION_THUMBNAILS_DIR
            thumb_file = MOTION_THUMBNAILS_DIR / f"{event_id}.jpg"
            try:
                thumb_file.write_bytes(recorder.motion_broadcaster.latest)
                thumbnail_path = str(thumb_file)
            except Exception:
                pass

        await self._conn.execute(
            "INSERT INTO motion_events "
            "(id, camera_id, started_at, ended_at, thumbnail_path, "
            " sound_class, sound_confidence, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'audio')",
            (event_id, camera_id, now_iso, now_iso, thumbnail_path,
             label, confidence),
        )
        await self._conn.commit()

        cam = await db.get_camera(self._conn, camera_id)
        cam_name = cam.name if cam else camera_id[:8]
        summary = f"{label.replace('_', ' ').title()} at {cam_name}"

        await self._conn.execute(
            "UPDATE motion_events SET summary = ? WHERE id = ?",
            (summary, event_id),
        )
        await self._conn.commit()

        await self._event_bus.emit(
            "motion_event_created",
            {
                "id": event_id,
                "camera_id": camera_id,
                "started_at": now_iso,
                "sound_class": label,
                "source": "audio",
                "summary": summary,
                "thumbnail_path": thumbnail_path,
            },
        )
        logger.info(
            "Audio event: %s on %s (%.2f)", label, camera_id[:8], confidence
        )

    async def _enrich_recent_event(
        self, camera_id: str, label: str, confidence: float
    ) -> None:
        """Attach a low-priority sound label to a recent motion event
        on the same camera. If no recent event exists, discard silently.

        The recency cutoff is `_ENRICH_WINDOW_S` seconds — a bark that
        lands minutes after the last motion event must not mutate that
        stale row. `started_at` is stored as an ISO8601 UTC string from
        `_fire_independent_event` and motion's recorder path, so an ISO
        string comparison is lexicographic and produces the right order.
        """
        cutoff_iso = (
            datetime.now(timezone.utc) - timedelta(seconds=_ENRICH_WINDOW_S)
        ).isoformat()
        cursor = await self._conn.execute(
            "SELECT id FROM motion_events "
            "WHERE camera_id = ? AND source = 'vision' "
            "AND sound_class IS NULL "
            "AND started_at > ? "
            "ORDER BY started_at DESC LIMIT 1",
            (camera_id, cutoff_iso),
        )
        row = await cursor.fetchone()
        if row is None:
            return

        event_id = row[0]
        await db.update_motion_event_sound(
            self._conn, event_id, label, confidence
        )
        await self._event_bus.emit(
            "motion_event_updated",
            {"id": event_id, "camera_id": camera_id, "sound_class": label},
        )
        logger.debug("Enriched event %s with sound %s", event_id[:8], label)

    async def shutdown(self) -> None:
        for task in self._consumers.values():
            task.cancel()
        self._consumers.clear()
