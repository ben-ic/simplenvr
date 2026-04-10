"""SummarizerManager — async worker that describes classified episodes.

Mirrors the ClassificationManager pattern: bounded queue, drop-oldest,
silent fallback, async worker dispatching to a sync inference executor.

The summarizer only processes events that already have a YOLOX label
(object_class IS NOT NULL). Unlabeled events were never interesting
enough to classify and certainly aren't worth a VLM forward pass.

Descriptions are written to motion_events.description (new column)
and emitted via the event bus for real-time frontend updates.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

from .. import db
from .summarizer import MoondreamSummarizer, SummaryResult

if TYPE_CHECKING:
    import aiosqlite

    from ..api.ws import EventBus

logger = logging.getLogger(__name__)

# Smaller queue than the classifier — Moondream is 100× slower per
# item (~2s vs ~30ms), so we don't want to queue more than we can
# realistically process. 16 episodes at ~2s each = ~32s backlog max.
_QUEUE_MAXSIZE = 16


class SummarizerManager:
    """Owns the MoondreamSummarizer and the inference worker loop.

    One instance per backend process, constructed in the lifespan
    after the capability probe has written its verdict.
    """

    def __init__(
        self,
        conn: "aiosqlite.Connection",
        event_bus: "EventBus",
        eligible: bool,
    ):
        self._conn = conn
        self._event_bus = event_bus
        self._eligible = eligible
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._worker_task: asyncio.Task | None = None
        self._summarizer: MoondreamSummarizer | None = None
        self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def start(self) -> None:
        """Kick off background model loading. Returns immediately so the
        backend can start listening before Moondream finishes downloading.

        Refuses to start when not eligible or explicitly disabled.
        """
        if (os.environ.get("SIMPLENVR_SUMMARIZER") or "").lower() == "off":
            logger.info("summarizer disabled via SIMPLENVR_SUMMARIZER=off")
            return
        if not self._eligible:
            logger.info("summarizer disabled: not eligible (tier/disk)")
            return

        self._summarizer = MoondreamSummarizer()
        # Fire-and-forget: load the model in the background so we don't
        # block the backend startup (Tauri has a 15-second timeout).
        self._worker_task = asyncio.create_task(self._load_then_run())
        logger.info("summarizer manager: background load started")

    async def _load_then_run(self) -> None:
        """Load the model in an executor, then start the worker loop."""
        assert self._summarizer is not None
        try:
            loop = asyncio.get_running_loop()
            loaded = await loop.run_in_executor(None, self._summarizer.load)
            if not loaded:
                logger.warning("summarizer model failed to load, staying disabled")
                self._summarizer = None
                return

            self._enabled = True
            logger.info("summarizer manager ready: queue=%d", _QUEUE_MAXSIZE)
            await self._run_worker()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("summarizer background load failed: %s", e, exc_info=True)

    async def shutdown(self) -> None:
        self._enabled = False
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except (asyncio.CancelledError, Exception):
                pass
        self._worker_task = None
        self._summarizer = None

    def submit(self, event: dict) -> None:
        """Enqueue a labeled motion event for summarization.

        Synchronous (no await). Silently no-ops when disabled.
        Only accepts events with object_class set.
        """
        if not self._enabled:
            return
        if not event.get("object_class"):
            return

        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                dropped = self._queue.get_nowait()
                self._queue.task_done()
                logger.warning(
                    "summarizer queue full, dropped event=%s",
                    dropped.get("id"),
                )
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.error("summarizer queue wedged, losing event=%s", event.get("id"))

    async def _run_worker(self) -> None:
        assert self._summarizer is not None
        try:
            while True:
                event = await self._queue.get()
                try:
                    await self._process_event(event)
                except Exception as e:
                    logger.error(
                        "summarize event=%s failed: %s",
                        event.get("id"), e, exc_info=True,
                    )
                finally:
                    self._queue.task_done()
        except asyncio.CancelledError:
            raise

    async def _process_event(self, event: dict) -> None:
        assert self._summarizer is not None

        # Get the thumbnail JPEG for this event.
        thumbnail_path = event.get("thumbnail_path")
        if not thumbnail_path:
            return

        try:
            with open(thumbnail_path, "rb") as f:
                jpeg = f.read()
        except (FileNotFoundError, OSError) as e:
            logger.debug("thumbnail read failed for event=%s: %s", event["id"], e)
            return

        result: SummaryResult = await self._summarizer.describe_frame(jpeg)

        if result.description is None:
            return

        # Write description to DB.
        try:
            await self._conn.execute(
                "UPDATE motion_events SET description = ? WHERE id = ?",
                (result.description, event["id"]),
            )
            await self._conn.commit()
        except Exception as e:
            logger.error("update description failed: %s", e)
            return

        logger.info(
            "summarized event=%s desc=%r",
            event["id"], result.description[:80],
        )

        # Emit for real-time frontend update.
        await self._event_bus.emit(
            "motion_event_updated",
            {
                "id": event["id"],
                "camera_id": event.get("camera_id"),
                "description": result.description,
            },
        )
