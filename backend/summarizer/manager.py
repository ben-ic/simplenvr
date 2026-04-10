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


# How long (seconds) before the same camera+class combo gets a fresh
# VLM description. Within this window, repeat events are counted but
# not described — the Story compiler collapses them ("23 vehicles passed").
_DEDUP_WINDOW_S = 300  # 5 minutes


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
        # Dedup: track last VLM-described time per (camera_id, object_class).
        # Events with the same key within _DEDUP_WINDOW_S are skipped —
        # they just increment the count in the Story compiler.
        self._last_described: dict[tuple[str, str], float] = {}

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
        """Wait for live view to stabilize, then load the model."""
        assert self._summarizer is not None
        try:
            # Let cameras connect and live preview stabilize before
            # burning CPU/bandwidth on the Moondream download.
            await asyncio.sleep(30)
            logger.info("summarizer: starting model load after 30s warmup")

            # Tell the frontend a download may be starting.
            needs_download = not self._summarizer.is_cached()
            if needs_download:
                await self._event_bus.emit("model_download", {
                    "model": "moondream2",
                    "status": "downloading",
                    "message": "Downloading AI model (first time only)...",
                })

            loop = asyncio.get_running_loop()
            loaded = await loop.run_in_executor(None, self._summarizer.load)

            if needs_download:
                await self._event_bus.emit("model_download", {
                    "model": "moondream2",
                    "status": "done" if loaded else "error",
                    "message": "AI model ready" if loaded else "AI model failed to load",
                })

            if not loaded:
                logger.warning("summarizer model failed to load, staying disabled")
                self._summarizer = None
                return

            self._enabled = True
            logger.info("summarizer manager ready: queue=%d", _QUEUE_MAXSIZE)

            # Run worker and backfill concurrently. The worker drains
            # the queue; the backfill feeds it. Both run until done or
            # cancelled. The worker is infinite; the backfill finishes
            # when all events are processed, then the worker continues
            # handling new real-time events.
            await asyncio.gather(
                self._run_worker(),
                self._backfill(),
            )
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

        Dedup: if the same camera+class was described within the last
        5 minutes, skip the VLM. The Story compiler collapses repeats
        into counts ("23 vehicles passed") — no need to describe each.
        Person events always get described (higher value).
        """
        if not self._enabled:
            return
        obj_class = event.get("object_class")
        if not obj_class:
            return

        # Dedup: skip repeat vehicle/animal events on the same camera.
        # Person events always get described — they're higher value.
        if obj_class != "person":
            import time
            key = (event.get("camera_id", ""), obj_class)
            now = time.monotonic()
            last = self._last_described.get(key, 0)
            if now - last < _DEDUP_WINDOW_S:
                logger.debug(
                    "dedup skip: camera=%s class=%s (%.0fs ago)",
                    key[0][:8], obj_class, now - last,
                )
                return
            self._last_described[key] = now

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

    async def _extract_recording_frames(
        self, event: dict, duration_s: float
    ) -> list[bytes]:
        """Extract 1-2 JPEG frames from the recording at 50% and 90%."""
        try:
            from datetime import datetime, timezone
            started = datetime.fromisoformat(event["started_at"])
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)

            row = await db.get_recording_for_track(
                self._conn,
                camera_id=event["camera_id"],
                track_timestamp=event["started_at"],
            )
            if not row:
                return []

            import os
            if not os.path.exists(row["file_path"]):
                return []

            rec_started = datetime.fromisoformat(row["started_at"])
            if rec_started.tzinfo is None:
                rec_started = rec_started.replace(tzinfo=timezone.utc)

            seg_offset = (started - rec_started).total_seconds()

            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None, self._read_frames_sync,
                row["file_path"], seg_offset, duration_s,
            )
        except Exception as e:
            logger.debug("recording frame extraction failed: %s", e)
            return []

    @staticmethod
    def _read_frames_sync(
        file_path: str, seg_offset: float, duration_s: float
    ) -> list[bytes]:
        """Read 2 JPEG frames from the recording at 50% and 90%."""
        try:
            import cv2
            cap = cv2.VideoCapture(file_path)
            if not cap.isOpened():
                return []
            try:
                frames: list[bytes] = []
                for frac in (0.5, 0.9):
                    ms = (seg_offset + duration_s * frac) * 1000
                    cap.set(cv2.CAP_PROP_POS_MSEC, ms)
                    ret, frame = cap.read()
                    if ret and frame is not None:
                        _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                        frames.append(buf.tobytes())
                return frames
            finally:
                cap.release()
        except Exception:
            return []

    async def _backfill(self) -> None:
        """Process ALL labeled events that are missing summary/description.

        Feeds events to the queue in small batches so we don't overflow
        the bounded queue. Newest events first so the Inbox gets useful
        descriptions quickly while older events backfill in the background.
        """
        _BATCH = 20
        offset = 0
        total = 0
        try:
            # Count total work.
            cursor = await self._conn.execute(
                "SELECT COUNT(*) FROM motion_events "
                "WHERE object_class IS NOT NULL AND (summary IS NULL OR description IS NULL)"
            )
            remaining = (await cursor.fetchone())[0]
            if remaining == 0:
                logger.info("summarizer backfill: nothing to do")
                return
            logger.info("summarizer backfill: %d events to process", remaining)

            while True:
                cursor = await self._conn.execute(
                    "SELECT * FROM motion_events "
                    "WHERE object_class IS NOT NULL AND (summary IS NULL OR description IS NULL) "
                    "ORDER BY started_at DESC LIMIT ?",
                    (_BATCH,),
                )
                rows = await cursor.fetchall()
                if not rows:
                    break
                for row in rows:
                    # Wait for queue space instead of dropping.
                    await self._queue.put(dict(row))
                    total += 1
                # Log progress every batch.
                logger.info(
                    "summarizer backfill: %d/%d processed",
                    total, remaining,
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("summarizer backfill failed at %d: %s", total, e)
        else:
            if total:
                logger.info("summarizer backfill complete: %d events", total)

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

        # Collect up to 3 frames: thumbnail + recording frames at 25%
        # and 75% of the event duration. Multiple frames let Moondream
        # understand action (driving vs parked, arriving vs leaving).
        jpegs: list[bytes] = []
        duration_s = 0.0

        # Frame 1: the thumbnail (always available, captured at event start).
        thumbnail_path = event.get("thumbnail_path")
        if thumbnail_path:
            try:
                with open(thumbnail_path, "rb") as f:
                    jpegs.append(f.read())
            except (FileNotFoundError, OSError):
                pass

        # Frames 2-3: extract from the recording if available.
        if event.get("started_at") and event.get("ended_at"):
            try:
                from datetime import datetime
                s = datetime.fromisoformat(event["started_at"])
                e = datetime.fromisoformat(event["ended_at"])
                duration_s = (e - s).total_seconds()
            except Exception:
                pass

        if duration_s > 1 and len(jpegs) > 0:
            # Try to extract frames from the recording at 50% and 90%
            # of the event to show progression.
            mid_end_frames = await self._extract_recording_frames(
                event, duration_s
            )
            jpegs.extend(mid_end_frames)

        if not jpegs:
            return

        result: SummaryResult = await self._summarizer.describe_event(
            jpegs, duration_s
        )

        if result.summary is None and result.description is None:
            return

        # Write both fields to DB.
        try:
            await self._conn.execute(
                "UPDATE motion_events SET summary = ?, description = ? WHERE id = ?",
                (result.summary, result.description, event["id"]),
            )
            await self._conn.commit()
        except Exception as e:
            logger.error("update summary/description failed: %s", e)
            return

        logger.info(
            "summarized event=%s summary=%r csv=%r",
            event["id"],
            (result.summary or "")[:60],
            (result.description or "")[:60],
        )

        # Emit for real-time frontend update.
        await self._event_bus.emit(
            "motion_event_updated",
            {
                "id": event["id"],
                "camera_id": event.get("camera_id"),
                "summary": result.summary,
                "description": result.description,
            },
        )
