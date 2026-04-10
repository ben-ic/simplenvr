"""AudioBroadcaster — accumulates PCM s16le 16kHz mono bytes and
publishes fixed-size 0.96s windows with 0.48s hop to async subscribers.

Symmetric with FrameBroadcaster but audio-specific: frames are
fixed-size PCM windows ready for YAMNet inference, not variable-size
JPEGs.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
WINDOW_SAMPLES = 15360  # 0.96s at 16kHz
HOP_SAMPLES = 7680      # 0.48s overlap
BYTES_PER_SAMPLE = 2    # s16le
WINDOW_BYTES = WINDOW_SAMPLES * BYTES_PER_SAMPLE
HOP_BYTES = HOP_SAMPLES * BYTES_PER_SAMPLE


class AudioBroadcaster:
    def __init__(self, name: str, max_queue: int = 8):
        self._name = name
        self._max_queue = max_queue
        self._buffer = bytearray()
        self._subscribers: set[asyncio.Queue] = set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def feed(self, data: bytes) -> None:
        """Feed raw PCM bytes. Publishes 0.96s windows as they accumulate."""
        self._buffer.extend(data)
        while len(self._buffer) >= WINDOW_BYTES:
            window = bytes(self._buffer[:WINDOW_BYTES])
            self._publish(window)
            del self._buffer[:HOP_BYTES]

    def _publish(self, window: bytes) -> None:
        for q in self._subscribers:
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(window)
            except asyncio.QueueFull:
                pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._max_queue)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def close(self) -> None:
        """Signal all subscribers with None sentinel and clear state."""
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
        self._buffer.clear()
