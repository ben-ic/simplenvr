"""
CameraRecorder — owns one FFmpeg process per camera.

Reads FFmpeg's stderr to detect segment boundaries (the "Opening '...'
for writing" log lines). When a new segment starts, the previous one is
known to be closed, so we record its metadata to the DB and trigger
storage cleanup.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

# Use the safe Python equivalent of execFile (no shell, args as list)
from asyncio.subprocess import PIPE
from asyncio import create_subprocess_exec as spawn_proc

from .. import db
from ..config import BITRATE_ROLLING_WINDOW, FFMPEG_RESTART_BACKOFF
from .codec import build_record_cmd

if TYPE_CHECKING:
    import aiosqlite

    from ..api.ws import EventBus
    from ..models import Camera, Settings

logger = logging.getLogger(__name__)

# Matches: [segment @ 0x...] Opening '/path/to/file.mp4' for writing
SEGMENT_OPEN_RE = re.compile(r"Opening '([^']+\.mp4)' for writing")


class CameraRecorder:
    def __init__(
        self,
        camera: "Camera",
        settings: "Settings",
        encoder: str,
        encoder_flags: list[str],
        recordings_dir: Path,
        conn: "aiosqlite.Connection",
        event_bus: "EventBus",
        on_segment_complete=None,
    ):
        self.camera = camera
        self._settings = settings
        self._encoder = encoder
        self._encoder_flags = encoder_flags
        self._recordings_dir = recordings_dir
        self._conn = conn
        self._event_bus = event_bus
        self._on_segment_complete = on_segment_complete

        self._proc: asyncio.subprocess.Process | None = None
        self._watcher_task: asyncio.Task | None = None
        self._monitor_task: asyncio.Task | None = None
        self._current_segment_path: Path | None = None
        self._current_segment_started_at: datetime | None = None
        self._current_segment_id: str | None = None
        self._running = False
        self._backoff_index = 0
        self._bitrate_history: deque[int] = deque(maxlen=BITRATE_ROLLING_WINDOW)

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def average_bitrate_bps(self) -> int:
        if not self._bitrate_history:
            return 0
        return sum(self._bitrate_history) // len(self._bitrate_history)

    @property
    def live_bitrate_bps(self) -> int:
        """
        Current bitrate estimate based on the in-progress segment file size.
        Useful before any segment has completed.
        """
        # If we have rolling average, prefer that
        if self._bitrate_history:
            return self.average_bitrate_bps

        # Otherwise estimate from the in-progress file
        if (
            self._current_segment_path is None
            or self._current_segment_started_at is None
        ):
            return 0

        try:
            if not self._current_segment_path.exists():
                return 0
            file_bytes = self._current_segment_path.stat().st_size
            elapsed = (
                datetime.now(timezone.utc) - self._current_segment_started_at
            ).total_seconds()
            if elapsed < 5 or file_bytes == 0:
                return 0  # Need at least 5 seconds for a meaningful estimate
            return int(file_bytes * 8 / elapsed)
        except Exception:
            return 0

    @property
    def in_progress_file_bytes(self) -> int:
        """Current size of the in-progress segment file, if any."""
        if self._current_segment_path is None:
            return 0
        try:
            if self._current_segment_path.exists():
                return self._current_segment_path.stat().st_size
        except Exception:
            pass
        return 0

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        await self._spawn()

    async def _spawn(self) -> None:
        cam_dir = self._recordings_dir / self.camera.id
        today_dir = cam_dir / datetime.now().strftime("%Y-%m-%d")
        today_dir.mkdir(parents=True, exist_ok=True)

        # FFmpeg's strftime expansion produces e.g.
        # data/recordings/{cam_id}/2026-04-05/14-30-00.mp4
        output_pattern = cam_dir / "%Y-%m-%d" / "%H-%M-%S.mp4"

        cmd = build_record_cmd(
            rtsp_uri=self.camera.rtsp_uri,
            output_pattern=output_pattern,
            segment_secs=self._settings.segment_duration_minutes * 60,
            fps_setting=self._settings.recording_fps,
            encoder=self._encoder,
            encoder_flags=self._encoder_flags,
        )

        logger.info("Starting recording: %s (%s)", self.camera.ip, self.camera.id)

        try:
            # Use a large buffer limit (1MB) so long FFmpeg verbose lines
            # don't trigger LimitOverrunError in the stderr reader
            self._proc = await spawn_proc(
                *cmd, stdout=PIPE, stderr=PIPE, limit=1024 * 1024
            )
        except FileNotFoundError:
            logger.error("ffmpeg not found in PATH")
            self._running = False
            return

        self._watcher_task = asyncio.create_task(self._stderr_watcher())
        self._monitor_task = asyncio.create_task(self._process_monitor())

        await self._event_bus.emit(
            "recording_started", {"camera_id": self.camera.id}
        )

    async def stop(self) -> None:
        self._running = False
        if self._proc is None:
            return

        # SIGTERM so FFmpeg can finalize the current segment cleanly
        try:
            self._proc.terminate()
        except ProcessLookupError:
            pass

        try:
            await asyncio.wait_for(self._proc.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning("FFmpeg did not exit cleanly, killing")
            try:
                self._proc.kill()
                await self._proc.wait()
            except Exception:
                pass

        for task in (self._watcher_task, self._monitor_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        # Finalize any in-progress segment
        if self._current_segment_path and self._current_segment_path.exists():
            await self._finalize_segment(
                self._current_segment_path, self._current_segment_started_at
            )

        self._current_segment_path = None
        self._current_segment_started_at = None
        self._current_segment_id = None
        self._proc = None

        await self._event_bus.emit(
            "recording_stopped", {"camera_id": self.camera.id}
        )

    async def _stderr_watcher(self) -> None:
        if self._proc is None or self._proc.stderr is None:
            return

        try:
            while True:
                try:
                    line = await self._proc.stderr.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    # Line too long — drain it and continue
                    try:
                        await self._proc.stderr.read(1024 * 1024)
                    except Exception:
                        pass
                    continue

                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").rstrip()

                match = SEGMENT_OPEN_RE.search(decoded)
                if match:
                    new_path = Path(match.group(1))
                    prev_path = self._current_segment_path
                    prev_started = self._current_segment_started_at

                    # Pre-create the new segment's parent dir
                    new_path.parent.mkdir(parents=True, exist_ok=True)

                    self._current_segment_path = new_path
                    self._current_segment_started_at = datetime.now(timezone.utc)
                    self._current_segment_id = str(uuid.uuid4())

                    await db.insert_recording(
                        self._conn,
                        recording_id=self._current_segment_id,
                        camera_id=self.camera.id,
                        started_at=self._current_segment_started_at.isoformat(),
                        file_path=str(new_path),
                    )

                    if prev_path is not None and prev_started is not None:
                        await self._finalize_segment(prev_path, prev_started)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("stderr watcher error for %s: %s", self.camera.ip, e)

    async def _finalize_segment(
        self, path: Path, started_at: datetime | None = None
    ) -> None:
        try:
            if not path.exists():
                logger.warning("Segment file missing: %s", path)
                return

            stat = path.stat()
            file_bytes = stat.st_size
            ended_at = datetime.now(timezone.utc)

            if started_at is None:
                started_at = ended_at

            duration_s = max((ended_at - started_at).total_seconds(), 1.0)
            bitrate_bps = int(file_bytes * 8 / duration_s) if duration_s > 0 else 0

            await db.complete_recording(
                self._conn,
                file_path=str(path),
                ended_at=ended_at.isoformat(),
                file_bytes=file_bytes,
                duration_s=duration_s,
                bitrate_bps=bitrate_bps,
            )

            self._bitrate_history.append(bitrate_bps)

            # Reset backoff if we got a stable segment
            half_segment = (self._settings.segment_duration_minutes * 60) / 2
            if duration_s >= half_segment:
                self._backoff_index = 0

            if self._on_segment_complete:
                await self._on_segment_complete(self.camera.id, file_bytes, bitrate_bps)

        except Exception as e:
            logger.error("Failed to finalize segment %s: %s", path, e)

    async def _process_monitor(self) -> None:
        if self._proc is None:
            return
        try:
            await self._proc.wait()
        except asyncio.CancelledError:
            raise

        if self._running:
            delay = FFMPEG_RESTART_BACKOFF[
                min(self._backoff_index, len(FFMPEG_RESTART_BACKOFF) - 1)
            ]
            self._backoff_index += 1
            logger.warning(
                "FFmpeg for %s exited unexpectedly (rc=%s), restarting in %ds",
                self.camera.ip,
                self._proc.returncode,
                delay,
            )
            await asyncio.sleep(delay)
            if self._running:
                await self._spawn()
