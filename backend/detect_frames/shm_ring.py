"""Single-slot newest-frame queue for the detect pipeline.

Despite the filename (kept to match plan §9), this does NOT use
`multiprocessing.shared_memory` — the detect task runs in the same process
as the ffmpeg supervisor, so an `asyncio.Queue(maxsize=1)` with drop-oldest
semantics is both simpler and sufficient.

Semantics:
  - `put(frame)` never blocks. If a frame is already waiting, it is
    evicted and replaced — the detector always gets the newest frame,
    never a stale backlog.
  - `get()` awaits the next frame and consumes it.
  - `latest()` peeks at the current slot without consuming. Returns
    None if empty.
"""

from __future__ import annotations

import asyncio
from typing import Optional

import numpy as np


class NewestFrameSlot:
    def __init__(self) -> None:
        self._q: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=1)
        self._latest: Optional[np.ndarray] = None

    def put(self, frame: np.ndarray) -> None:
        """Drop-oldest put. Never blocks; never raises on full."""
        self._latest = frame
        try:
            self._q.put_nowait(frame)
        except asyncio.QueueFull:
            try:
                self._q.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._q.put_nowait(frame)
            except asyncio.QueueFull:
                # Lost a race with another producer; the newer frame is
                # already queued, so dropping this one is correct.
                pass

    async def get(self) -> np.ndarray:
        """Await the next frame and consume it."""
        return await self._q.get()

    def latest(self) -> Optional[np.ndarray]:
        """Peek at the current slot without consuming. May be None."""
        return self._latest
