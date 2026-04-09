"""
FrameBroadcaster — fan-out for JPEG frames produced by a single FFmpeg
output, consumed by zero-or-more async subscribers (browser previews,
motion detector, etc).

Each subscriber gets its own bounded asyncio.Queue. When a subscriber
falls behind, the OLDEST queued frame is dropped to make room for the
newest — a slow client must never back-pressure the producer, because
the producer is the camera's only RTSP connection.

The broadcaster also caches the most recently published frame so that
new subscribers can start with a visible image immediately rather than
waiting for the next keyframe.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class FrameBroadcaster:
    def __init__(self, name: str, max_queue: int = 5):
        self._name = name
        self._max_queue = max_queue
        self._latest: bytes | None = None
        self._subscribers: set[asyncio.Queue] = set()

    @property
    def latest(self) -> bytes | None:
        return self._latest

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, frame: bytes) -> None:
        """Push a new frame to all subscribers. Drops the oldest queued
        frame for any subscriber whose queue is full — never blocks."""
        self._latest = frame
        for q in self._subscribers:
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(frame)
            except asyncio.QueueFull:
                # Race with another publisher (shouldn't happen — single
                # producer per broadcaster) — silently drop.
                pass

    def subscribe(self) -> asyncio.Queue:
        """Return a new bounded queue receiving every published frame.
        If a frame has already been published, the new queue is primed
        with that latest frame so the subscriber sees something at once."""
        q: asyncio.Queue = asyncio.Queue(maxsize=self._max_queue)
        self._subscribers.add(q)
        if self._latest is not None:
            try:
                q.put_nowait(self._latest)
            except asyncio.QueueFull:
                pass
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def reset(self) -> None:
        """Clear the latest-frame cache. Called when the upstream producer
        restarts so subscribers don't see a stale frame from the previous
        ffmpeg generation."""
        self._latest = None

    def close(self) -> None:
        """Signal every current subscriber that the producer is gone,
        then forget them.

        The signal is a `None` sentinel put into each subscriber's
        queue — consumers that read from the queue can treat `None`
        as an EOF marker and exit their loop. Without this call, a
        subscriber on a soon-to-be-abandoned broadcaster will
        `await queue.get()` forever because nobody will ever publish
        another frame to it. The motion detector's consume loop in
        backend/motion/detector.py is the primary consumer and
        explicitly handles the None sentinel.

        We drop oldest-first if a queue is full (same policy as
        publish()) so the sentinel always lands even for slow
        consumers. The subscriber set is then cleared so any
        stragglers that subscribe through a stale handle later
        become effective no-ops.

        Called by CameraRecorder.stop() on recorder teardown so the
        motion detector exits its consume loop cleanly instead of
        sitting forever on an empty queue from a dead producer."""
        for q in list(self._subscribers):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass
        self._subscribers.clear()
        self._latest = None
