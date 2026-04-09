"""
MotionDetector — consumes scene-filtered JPEG frames from a camera's
unified recorder pipeline and groups them into motion events.

Architecture: scene filtering happens inside the recorder's single
ffmpeg process (`select='gt(scene,N)'` is one of three -map outputs).
The detector subscribes to the recorder's motion FrameBroadcaster and
runs only the event-grouping + classifier-feeding logic in Python — no
ffmpeg subprocess, no extra RTSP connection, no concurrency conflict.

Two layers of output, both fed from the same JPEG stream:

  1. **Time-grouped motion events** (existing, unchanged). First frame
     of a burst opens a motion_events row; 2 seconds of idle closes it.
     This is the always-on "something happened at this time" record
     the Inbox has always shown. It never depends on the classifier
     being healthy.

  2. **Spatially-tracked per-object events** (new in Phase 1). Each
     incoming frame is decoded and passed through OpenCV MOG2
     background subtraction to get a foreground mask; the contours
     become bboxes; the bboxes feed a per-camera CameraTracker (the
     IOU tracker). Closed tracks become tracked_events rows for the
     classifier to label later. If OpenCV is missing, this whole
     layer silently disables itself — the legacy motion_events path
     keeps working and the Inbox falls back to "Motion at X" exactly
     as it did before the classifier subsystem existed.

The two layers share a frame but not a lifecycle. A single motion
event can produce zero, one, or many tracked events depending on what
the tracker sees (leaf jiggle → 0 tracks, lone person → 1 track,
person walking past a car → 2 tracks). The classifier consumes the
tracked events; the motion event is purely a time-bucket for Inbox
rendering.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .. import db
from ..config import MOTION_DEBOUNCE_SECONDS, MOTION_THUMBNAILS_DIR
from .tracker import CameraTracker, Track

# OpenCV + numpy are soft dependencies — if they aren't importable,
# the spatial-tracking layer disables itself and the detector falls
# back to its legacy time-only behavior. This matters for the dev
# workflow where someone might have only done `pip install fastapi`
# without the full requirements, and for the "probe says classifier
# disabled" tier path where we don't want to load 50 MB of OpenCV
# into RSS for no reason.
try:
    import cv2  # type: ignore
    import numpy as np  # type: ignore
    _CV2_AVAILABLE = True
except ImportError:
    cv2 = None  # type: ignore
    np = None  # type: ignore
    _CV2_AVAILABLE = False

if TYPE_CHECKING:
    import aiosqlite

    from ..api.ws import EventBus
    from ..models import Camera
    from ..recording.camera_recorder import CameraRecorder

logger = logging.getLogger(__name__)

# MOG2 parameters tuned for indoor/outdoor surveillance at ~1-2 fps
# (scene-filter output cadence, not full video rate). These are zero-
# config — never exposed to users — and were chosen for:
#   history=500      — ~4 minutes of learning at 2 fps. Long enough to
#                      stabilize on outdoor light changes, short enough
#                      that a new permanent change (e.g. someone parks
#                      a car in the driveway) becomes background within
#                      a few minutes instead of haunting detection for
#                      hours.
#   varThreshold=25  — default. Mahalanobis-squared threshold above
#                      which a pixel is flagged as foreground.
#   detectShadows=False — we don't need OpenCV's shadow-classification
#                      pass; it costs CPU and produces gray-value pixels
#                      we'd just threshold back to binary anyway.
_MOG2_HISTORY = 500
_MOG2_VAR_THRESHOLD = 25
_MOG2_DETECT_SHADOWS = False

# Minimum contour area (in foreground-mask pixels, NOT original frame
# pixels) before a blob is treated as a real candidate motion region.
# Below this, it's almost certainly compression noise or a 1-pixel
# flicker and we drop it before it reaches the tracker. 200 is tuned
# for ~320-wide preview JPEGs; larger inputs would want more.
_MIN_CONTOUR_AREA_PX = 200


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

        # --- Spatial tracking layer (new in Phase 1) ---------------------
        # One MOG2 background subtractor and one IOU tracker per camera.
        # Both are None if OpenCV isn't importable or if the capability
        # probe said tier=disabled — in that case we keep the legacy
        # time-only motion event path and don't feed the classifier.
        # Lazy-initialized on first frame so the memory cost (MOG2
        # allocates state roughly proportional to frame size × history)
        # is only paid when the camera actually starts producing frames.
        self._mog2: "cv2.BackgroundSubtractorMOG2 | None" = None  # type: ignore[name-defined]
        self._tracker: CameraTracker | None = (
            CameraTracker(camera.id) if _CV2_AVAILABLE else None
        )
        # Bbox-to-original-frame-pixels scale factor. MOG2 runs on the
        # decoded preview JPEG (~320 wide); the classifier will run on
        # the same JPEG for now, so no rescale is needed in Phase 1.
        # This field exists so Phase 2 can swap in the full-resolution
        # recording frame without re-threading the tracker output.
        self._frame_scale: tuple[float, float] = (1.0, 1.0)

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

        # Flush any promoted tracks that are still in-flight so they
        # don't get stranded with no terminal classifier pass. Tracks
        # that haven't hit the promotion gate are discarded silently —
        # we don't emit half-observed blobs as tracked_events.
        if self._tracker is not None:
            try:
                stranded = self._tracker.flush()
                if stranded:
                    logger.info(
                        "Flushing %d stranded tracks on shutdown cam=%s",
                        len(stranded), self.camera.id,
                    )
                    # On stop we don't have a current frame to stamp as
                    # thumbnail — pass empty bytes which the persist
                    # helper will tolerate (it silently fails the thumb
                    # write but still inserts the row).
                    for track in stranded:
                        await self._persist_tracked_event(track, b"")
            except Exception as e:
                logger.warning("tracker flush on stop failed: %s", e)

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
            # --- Legacy layer: time-grouped motion event --------------
            # This path has existed since before the classifier and is
            # the authoritative "something happened at this camera now"
            # signal for the Inbox. It runs first and is independent of
            # the spatial-tracking layer below — if OpenCV is missing,
            # the tracker stays None and this path still works.
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

        # --- Spatial tracking layer (new) -----------------------------
        # Runs OUTSIDE the lock so MOG2's ~2-5 ms of OpenCV work doesn't
        # block other frames being enqueued. The tracker itself is not
        # thread-safe, but we're the only caller from a single consumer
        # task per camera so there's no concurrency on the tracker.
        # Any exception in this layer is swallowed and logged — it must
        # never take down the legacy motion path.
        if self._tracker is not None and _CV2_AVAILABLE:
            try:
                bboxes = self._extract_motion_bboxes(jpeg)
                for bbox in bboxes:
                    self._tracker.observe(bbox, now)
                closed_tracks = self._tracker.sweep_idle(now)
                for track in closed_tracks:
                    await self._persist_tracked_event(track, jpeg)
            except Exception as e:
                logger.warning(
                    "spatial tracking frame failed cam=%s: %s",
                    self.camera.id, e,
                )

    def _extract_motion_bboxes(self, jpeg: bytes) -> list[tuple[int, int, int, int]]:
        """Decode a JPEG and run MOG2 + contours → bboxes.

        Returns (x, y, w, h) tuples in pixel space of the decoded
        preview JPEG. Phase 2 may rescale these to full-resolution
        recording frame coordinates for the classifier crop; Phase 1
        keeps everything in preview-JPEG space because the classifier
        also runs on the same preview frames.

        Empty list is a first-class result: MOG2's warmup period, a
        completely static scene, or a scene where all contours are
        below the noise floor all return []. The caller should not
        treat empty as an error.
        """
        assert cv2 is not None and np is not None  # soft-dep guard above
        # Decode the JPEG. cv2.imdecode returns a BGR uint8 ndarray; if
        # the bytes are corrupt it returns None and we bail cleanly.
        buf = np.frombuffer(jpeg, dtype=np.uint8)
        frame = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        if frame is None:
            return []

        # Lazy-init MOG2 on first valid frame. Creating it eagerly in
        # __init__ would allocate state for cameras that never end up
        # sending frames (e.g. the recorder never starts).
        if self._mog2 is None:
            self._mog2 = cv2.createBackgroundSubtractorMOG2(
                history=_MOG2_HISTORY,
                varThreshold=_MOG2_VAR_THRESHOLD,
                detectShadows=_MOG2_DETECT_SHADOWS,
            )

        # Foreground mask. MOG2 returns uint8 with 0=background,
        # 255=foreground, (127=shadow if detectShadows were True).
        fg_mask = self._mog2.apply(frame)

        # Morphological opening removes salt-and-pepper foreground
        # noise (MOG2 sometimes flags isolated pixels on highly
        # compressed JPEGs). Kernel size 3 is small enough to preserve
        # real objects while eliminating 1-2 px flickers.
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        fg_mask = cv2.morphologyEx(fg_mask, cv2.MORPH_OPEN, kernel)

        # Contour extraction. RETR_EXTERNAL means we only get top-level
        # contours (no nested holes), CHAIN_APPROX_SIMPLE compresses
        # horizontal/vertical/diagonal runs to endpoints.
        contours, _ = cv2.findContours(
            fg_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )

        bboxes: list[tuple[int, int, int, int]] = []
        for c in contours:
            if cv2.contourArea(c) < _MIN_CONTOUR_AREA_PX:
                continue
            x, y, w, h = cv2.boundingRect(c)
            bboxes.append((int(x), int(y), int(w), int(h)))
        return bboxes

    async def _persist_tracked_event(self, track: "Track", jpeg: bytes) -> None:
        """Write a closed tracked_events row for a promoted track and
        emit a WS event the classifier manager will consume.

        The thumbnail here is the *current* frame at track-close time,
        not the mid-point of the track — the latter would require
        storing per-frame JPEGs which we avoid for memory reasons.
        Phase 2 may revisit this by letting the classifier manager
        re-pull frames from the recorder's scene JPEGs on demand.
        """
        cam_dir = MOTION_THUMBNAILS_DIR / self.camera.id
        cam_dir.mkdir(parents=True, exist_ok=True)
        thumb_path = cam_dir / f"track_{track.id}.jpg"
        try:
            thumb_path.write_bytes(jpeg)
        except Exception as e:
            logger.warning("Failed to write track thumbnail: %s", e)
            thumb_path = None  # type: ignore[assignment]

        try:
            await db.insert_tracked_event(
                self._conn,
                tracked_id=track.id,
                camera_id=self.camera.id,
                motion_event_id=self._current_event_id,  # may be None if grouping closed first
                started_at=track.first_seen.isoformat(),
                ended_at=track.last_seen.isoformat(),
                frame_count=track.frame_count,
                bbox_json=json.dumps(list(track.bbox)),
                bbox_history_json=json.dumps(
                    [list(b) for b in track.bbox_history]
                ),
                thumbnail_path=str(thumb_path) if thumb_path else None,
            )
        except Exception as e:
            logger.error("Failed to insert tracked_event: %s", e)
            return

        await self._event_bus.emit(
            "tracked_event_closed",
            {
                "id": track.id,
                "camera_id": self.camera.id,
                "motion_event_id": self._current_event_id,
                "started_at": track.first_seen.isoformat(),
                "ended_at": track.last_seen.isoformat(),
                "frame_count": track.frame_count,
                "bbox": list(track.bbox),
            },
        )
        logger.info(
            "Track closed: cam=%s track=%s frames=%d duration=%.1fs",
            self.camera.id, track.id, track.frame_count,
            (track.last_seen - track.first_seen).total_seconds(),
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
