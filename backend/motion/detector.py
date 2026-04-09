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
    from ..classification.manager import ClassificationManager
    from ..models import Camera
    from ..recording.camera_recorder import CameraRecorder

# Maximum JPEGs we attach to one Track for the classifier. First, last,
# and up to 3 intermediates — enough for median-of-frames scoring to be
# meaningful without pinning tens of MB on a long-running track. When
# the buffer is full, we evict the item near the middle so the first
# and latest frames always survive.
_MAX_FRAMES_PER_TRACK = 5

logger = logging.getLogger(__name__)

# MOG2 parameters tuned for indoor/outdoor surveillance at ~1-2 fps
# (scene-filter output cadence, not full video rate). These are zero-
# config — never exposed to users — and were chosen for:
#   history=120      — ~1-2 minutes of learning at scene-filter cadence.
#                      The original value was 500 (~4-8 min warmup),
#                      which was empirically catastrophic for quiet-
#                      scene cameras on 2026-04-09: Tapos at 10.0.0.46
#                      and 10.0.0.63 plus Reolink 1c3afcfa consistently
#                      produced only 1 bbox per burst because MOG2 was
#                      still in warmup when real motion appeared. 120
#                      stabilizes fast enough for a fresh backend start
#                      to classify your first walk-in-front within ~90
#                      seconds, at the cost of stationary objects
#                      becoming background a bit faster (fine for an
#                      NVR — we care about *moving* things, and the
#                      recorder still holds the raw video).
#   varThreshold=25  — default. Mahalanobis-squared threshold above
#                      which a pixel is flagged as foreground.
#   detectShadows=False — we don't need OpenCV's shadow-classification
#                      pass; it costs CPU and produces gray-value pixels
#                      we'd just threshold back to binary anyway.
_MOG2_HISTORY = 120
_MOG2_VAR_THRESHOLD = 25
_MOG2_DETECT_SHADOWS = False

# Minimum contour area (in foreground-mask pixels, NOT original frame
# pixels) before a blob is treated as a real candidate motion region.
# Below this, it's almost certainly compression noise or a 1-pixel
# flicker and we drop it before it reaches the tracker. 200 is tuned
# for ~320-wide preview JPEGs; larger inputs would want more.
_MIN_CONTOUR_AREA_PX = 200

# Morphological CLOSE kernel size (in preview-JPEG pixels). Applied
# AFTER the 3x3 OPEN despeckling pass to bridge gaps between adjacent
# foreground regions that really belong to the same physical object.
# Without this, MOG2 fragments a walking person into head+torso+legs
# (three contours), the tracker creates three candidate tracks, and
# the Inbox row count balloons. Empirically verified on 0c1ab3e9 on
# 2026-04-09 where one person walking past produced 10+ concurrent
# tracked_events. An 11x11 ellipse on a 320-wide frame bridges gaps
# up to ~11 px — enough to merge body parts of a single person or
# car fragments separated by a narrow low-contrast band — without
# merging two people walking side by side.
_MORPH_CLOSE_KERNEL_PX = 11


class MotionDetector:
    def __init__(
        self,
        camera: "Camera",
        recorder: "CameraRecorder",
        conn: "aiosqlite.Connection",
        event_bus: "EventBus",
        classifier: "ClassificationManager | None" = None,
    ):
        self.camera = camera
        self._recorder = recorder
        self._conn = conn
        self._event_bus = event_bus
        # Optional classifier manager. When None (or when the manager
        # is disabled internally), the detector still persists tracked
        # events but no labels are ever written. This is the
        # tier=disabled safe-failure path.
        self._classifier = classifier

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
                # None is the FrameBroadcaster EOF sentinel, published
                # by close() when the upstream recorder is stopped.
                # Exit the consume loop cleanly so the detector's
                # shutdown path runs without trying to decode a null
                # frame.
                if frame is None:
                    return
                await self._handle_frame(frame)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(
                "Motion consume loop error for %s: %s", self.camera.ip, e
            )

    async def _handle_frame(self, jpeg: bytes) -> None:
        now = datetime.now(timezone.utc)

        # --- Spatial tracking layer (runs FIRST now) ------------------
        # Architectural inversion: the legacy time-grouped motion_events
        # row is now GATED on MOG2 finding at least one foreground bbox,
        # not on "a frame arrived from the scene filter." The old design
        # was safe only because the scene filter itself discriminated
        # quiet from active frames — but that filter is whole-frame
        # histogram based and completely silent on indoor scenes (see
        # codec.py motion_args comment for the full story). Now that
        # the scene filter has a 1 fps floor, quiet frames DO reach the
        # detector constantly, and opening a motion_events row on every
        # arrival would flood the Inbox with dead-scene rows. MOG2 is
        # the real discriminator: if there are no contours above the
        # area floor, there's no motion, and motion_events stays quiet.
        bboxes: list[tuple[int, int, int, int]] = []
        if self._tracker is not None and _CV2_AVAILABLE:
            try:
                bboxes = self._extract_motion_bboxes(jpeg)
            except Exception as e:
                logger.warning(
                    "spatial tracking frame failed cam=%s: %s",
                    self.camera.id, e,
                )
                bboxes = []

        # Always sweep idle tracks, even on bbox-free frames, so promoted
        # tracks close on the expected timeline even when a camera's
        # foreground goes fully quiet mid-track. Without this, a person
        # who walks in and then stops would leave their track hanging
        # until the next foreground-bearing frame.
        closed_tracks: list[Track] = []
        if self._tracker is not None:
            try:
                closed_tracks = self._tracker.sweep_idle(now)
            except Exception as e:
                logger.warning(
                    "tracker sweep failed cam=%s: %s", self.camera.id, e,
                )

        # If there's nothing to report AND no tracks closing, we're
        # done — this is the quiet-frame fast path. The _last_frame_at
        # stamp is NOT updated here because the idle closer should not
        # see spurious liveness from dead-scene frames; only real
        # foreground activity keeps a motion event alive.
        if not bboxes and not closed_tracks:
            return

        # --- Legacy layer: time-grouped motion event ------------------
        # Only open/extend a motion event when MOG2 actually found
        # foreground. This is what makes the fps-floor safe.
        async with self._lock:
            if bboxes:
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

        # --- Tracker observe (only when bboxes present) --------------
        # sweep_idle already ran at the top of this function so closed
        # tracks are in `closed_tracks` from the earlier call. Here we
        # only feed NEW bboxes into the tracker and attach frame refs
        # for the classifier.
        if bboxes and self._tracker is not None and _CV2_AVAILABLE:
            try:
                logger.info(
                    "mog2 cam=%s bboxes=%d active_tracks=%d",
                    self.camera.id, len(bboxes),
                    self._tracker.active_count,
                )
                for bbox in bboxes:
                    track = self._tracker.observe(bbox, now)
                    # Attach up to _MAX_FRAMES_PER_TRACK JPEGs to every
                    # promoted-or-candidate track so the classifier has
                    # representative samples at close time. Candidates
                    # that never promote just get their frame buffer
                    # garbage-collected with the track dataclass; the
                    # memory cost is bounded to:
                    #   active tracks × 5 frames × ~15 KB preview JPEG
                    # which peaks around 1-2 MB per camera worst case.
                    if track is not None:
                        self._append_frame(track, jpeg)
            except Exception as e:
                logger.warning(
                    "spatial tracking observe failed cam=%s: %s",
                    self.camera.id, e,
                )

        # --- Persist closed tracks (always, if any) ------------------
        # Closed tracks may appear on both bbox-bearing and bbox-free
        # frames — e.g. a person leaves the frame, subsequent frames
        # have no foreground, and IDLE_TIMEOUT_SECONDS later their
        # track sweeps on the next frame regardless of bbox content.
        for track in closed_tracks:
            try:
                await self._persist_tracked_event(track, jpeg)
            except Exception as e:
                logger.warning(
                    "track persist failed cam=%s track=%s: %s",
                    self.camera.id, track.id, e,
                )

    def _append_frame(self, track: "Track", jpeg: bytes) -> None:
        """Append a JPEG to the track's classifier-sample buffer.

        Invariant: after this call, `track.frame_refs` has at most
        _MAX_FRAMES_PER_TRACK entries, the first entry is always the
        first observed frame, and the last entry is always the most
        recent frame. When the buffer is full we evict near the middle
        so we keep temporal spread without letting the list grow.
        """
        refs = track.frame_refs
        if len(refs) < _MAX_FRAMES_PER_TRACK:
            refs.append(jpeg)
            return
        # Full: keep first + newest, evict an interior slot. Middle
        # index gets overwritten so the remaining interior slots still
        # span the track's lifetime roughly uniformly.
        mid = len(refs) // 2
        refs.pop(mid)
        refs.append(jpeg)

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
        open_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        fg_mask = cv2.morphologyEx(fg_mask, cv2.MORPH_OPEN, open_kernel)

        # Morphological closing bridges small gaps between adjacent
        # foreground regions so a fragmented body (head/torso/legs) or
        # car (roof/hood/wheels) merges into a single contour before
        # findContours runs. This is what stops oversegmentation from
        # spawning 10 Inbox rows for one real object. The kernel size
        # is intentionally larger than the OPEN kernel — OPEN removes
        # noise at pixel scale, CLOSE bridges gaps at object-part
        # scale. Order matters: OPEN first so noise doesn't get
        # bridged into larger noise.
        close_kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE,
            (_MORPH_CLOSE_KERNEL_PX, _MORPH_CLOSE_KERNEL_PX),
        )
        fg_mask = cv2.morphologyEx(fg_mask, cv2.MORPH_CLOSE, close_kernel)

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

        # Hand the track off to the classifier manager. Sync call, no
        # await — the manager's submit() owns its own bounded queue and
        # drop-oldest overflow policy, so the detector stays on the
        # critical-path I/O loop. Silently no-ops when the manager is
        # disabled (tier=disabled or SIMPLENVR_CLASSIFIER=off).
        if self._classifier is not None:
            try:
                self._classifier.submit(track)
            except Exception as e:
                logger.warning("classifier submit failed: %s", e)

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
