"""ClassificationManager — async worker that labels closed tracks.

Wires the motion pipeline to the YoloxClassifier. Responsibilities:

  * **Refuse to start on disabled tiers.** If the capability probe said
    `classification_tier=disabled` or the user set SIMPLENVR_CLASSIFIER=off,
    the manager constructs an inert stub that silently drops every
    submitted track. The Inbox stays at "Motion at X" forever on that
    install, which is the documented safe-failure mode.

  * **Bounded in-process queue with drop-oldest.** Motion bursts on
    busy scenes can produce tracks faster than the classifier runs.
    A bounded `asyncio.Queue(maxsize=64)` with drop-oldest semantics
    prevents a runaway queue from eating RAM during a multi-camera
    event. Losing the oldest track on overflow is the right tradeoff:
    the newest track is the freshest observation and most likely to
    still be in-frame for the user.

  * **Multi-track collapse at the motion-event layer.** A single
    motion_event can have 0, 1, or many tracked_events rows. For the
    Inbox-facing row, we write the *highest-confidence labeled track*
    to `motion_events.object_class`. Per-track labels live in
    `tracked_events` as the detail layer. This is the Phase 2 design
    decision that keeps oversegmented MOG2 scenes from exploding the
    Inbox into ten rows for one real event.

  * **Emits `motion_event_updated` on the event bus.** The frontend
    subscribes and patches the affected motion event in place so the
    Inbox row re-renders with the new label without a full refetch.

The manager does not own the classifier's lifetime past its own: on
shutdown, the ORT session is dropped with the manager. The capability
probe's cached verdict survives across restarts so re-probing is cheap.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

from .. import db
from ..motion.tracker import Track
from .classifier import YoloxClassifier, ClassificationResult

if TYPE_CHECKING:
    import aiosqlite

    from ..api.ws import EventBus

logger = logging.getLogger(__name__)

# Max in-flight tracks. Sized for 32 cameras × typical track rate so
# a single busy camera can't starve the others, but small enough that
# a runaway producer can't pin ~100 MB of JPEG bytes in RAM (each
# track carries up to ~5 small JPEGs).
_QUEUE_MAXSIZE = 64


class ClassificationManager:
    """Owns the YoloxClassifier and the inference worker loop.

    Not re-entrant. One instance per backend process, constructed in
    the FastAPI lifespan after the capability probe has written its
    verdict to settings.
    """

    def __init__(
        self,
        conn: "aiosqlite.Connection",
        event_bus: "EventBus",
        tier: str,
        ep: str,
    ):
        self._conn = conn
        self._event_bus = event_bus
        self._tier = tier
        self._ep = ep
        self._queue: asyncio.Queue[Track] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._worker_task: asyncio.Task | None = None
        self._classifier: YoloxClassifier | None = None
        self._enabled = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def start(self) -> None:
        """Load the ORT session and spawn the worker task.

        Refuses to start on tier=disabled or SIMPLENVR_CLASSIFIER=off —
        this is the path that honors the capability probe's veto.
        """
        if (os.environ.get("SIMPLENVR_CLASSIFIER") or "").lower() == "off":
            logger.info("classifier disabled via SIMPLENVR_CLASSIFIER=off")
            return
        if self._tier == "disabled":
            logger.info("classifier disabled: tier=disabled from capability probe")
            return

        try:
            self._classifier = YoloxClassifier(tier=self._tier, ep=self._ep)
        except Exception as e:
            logger.error(
                "classifier failed to load, staying disabled: %s", e, exc_info=True
            )
            return

        self._enabled = True
        self._worker_task = asyncio.create_task(self._run_worker())
        logger.info(
            "classifier manager started: tier=%s ep=%s queue=%d",
            self._tier, self._ep, _QUEUE_MAXSIZE,
        )

    async def shutdown(self) -> None:
        self._enabled = False
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except (asyncio.CancelledError, Exception):
                pass
        self._worker_task = None
        self._classifier = None

    # ------------------------------------------------------------------
    # Submit path — called synchronously from MotionDetector
    # ------------------------------------------------------------------

    def submit(self, track: Track) -> None:
        """Enqueue a closed, promoted track for classification.

        Synchronous (no await) so the motion detector doesn't have to
        schedule a task just to hand off a track. If the queue is full,
        drop the oldest entry and enqueue this one — newest is freshest.

        Silently no-ops when the manager is disabled so the motion
        detector can unconditionally call submit() without checking
        a flag first.
        """
        if not self._enabled:
            return
        try:
            self._queue.put_nowait(track)
        except asyncio.QueueFull:
            # Drop-oldest: make room for the newer observation. On a
            # busy scene the newest track is more likely to still be
            # visible to the user when the label lands.
            try:
                dropped = self._queue.get_nowait()
                self._queue.task_done()
                logger.warning(
                    "classifier queue full, dropped oldest track=%s cam=%s",
                    dropped.id, dropped.camera_id,
                )
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait(track)
            except asyncio.QueueFull:
                logger.error("classifier queue wedged, losing track=%s", track.id)

    # ------------------------------------------------------------------
    # Worker loop
    # ------------------------------------------------------------------

    async def _run_worker(self) -> None:
        assert self._classifier is not None
        try:
            while True:
                track = await self._queue.get()
                try:
                    await self._process_track(track)
                except Exception as e:
                    logger.error(
                        "classify track=%s failed: %s", track.id, e, exc_info=True
                    )
                finally:
                    self._queue.task_done()
        except asyncio.CancelledError:
            raise

    async def _process_track(self, track: Track) -> None:
        assert self._classifier is not None

        result: ClassificationResult = await self._classifier.classify_track(track)

        # Always write the per-track verdict, even when it's (None, 0).
        # The NULL row is how the detail layer records "we looked and
        # found nothing" vs "we never ran the classifier" — the
        # latter is distinguishable because the row would be untouched
        # (object_class stays NULL because insert_tracked_event left
        # it that way) but object_confidence would also stay NULL.
        # Here we at least stamp the confidence column.
        try:
            await db.update_tracked_event_classification(
                self._conn,
                tracked_id=track.id,
                object_class=result.label,
                object_confidence=result.confidence if result.label else None,
            )
        except Exception as e:
            logger.error("update_tracked_event_classification failed: %s", e)
            return

        logger.info(
            "classified track=%s cam=%s label=%s conf=%.3f frames=%d",
            track.id, track.camera_id,
            result.label or "none", result.confidence, len(track.frame_refs),
        )

        # Multi-track collapse at the motion_event layer. Look up every
        # sibling track under this motion_event_id and pick the single
        # highest-confidence labeled one; that's what the Inbox row
        # shows. If nothing is labeled (all None), the motion_event
        # row keeps its NULL object_class and the Inbox renders the
        # silent-fallback "Motion at X" sentence.
        motion_event_id = await self._lookup_motion_event_id(track.id)
        if motion_event_id is None:
            # Detector persisted the track before the motion event
            # existed, or the window already closed with no parent.
            # Nothing to collapse — per-track row still has the label.
            return

        siblings = await db.get_tracked_events_for_motion_event(
            self._conn, motion_event_id
        )
        winner_label: str | None = None
        winner_conf: float | None = None
        for row in siblings:
            label = row.get("object_class")
            conf = row.get("object_confidence")
            if label and conf is not None and (winner_conf is None or conf > winner_conf):
                winner_label = label
                winner_conf = conf

        if winner_label is None:
            # No labeled sibling yet. Leave motion_events.object_class
            # as NULL so the Inbox keeps the "Motion at X" row — we'll
            # re-evaluate the next time a sibling finishes classifying.
            return

        try:
            await db.update_motion_event_classification(
                self._conn,
                event_id=motion_event_id,
                object_class=winner_label,
                object_confidence=winner_conf,
            )
        except Exception as e:
            logger.error("update_motion_event_classification failed: %s", e)
            return

        await self._event_bus.emit(
            "motion_event_updated",
            {
                "id": motion_event_id,
                "camera_id": track.camera_id,
                "object_class": winner_label,
                "object_confidence": winner_conf,
            },
        )

    async def _lookup_motion_event_id(self, tracked_id: str) -> str | None:
        """Fetch the motion_event_id for a tracked_events row. Kept
        inline rather than as a dedicated db helper because it's only
        needed here."""
        try:
            cursor = await self._conn.execute(
                "SELECT motion_event_id FROM tracked_events WHERE id = ?",
                (tracked_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return row[0] if not hasattr(row, "keys") else row["motion_event_id"]
        except Exception as e:
            logger.warning("lookup motion_event_id failed: %s", e)
            return None
