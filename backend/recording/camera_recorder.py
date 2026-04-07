"""
CameraRecorder — owns ONE FFmpeg process per camera that opens a single
RTSP connection and produces three outputs:

  1. Segmented MP4 recording (stream-copy by default) — written to disk
  2. Scene-filtered motion frames — fanned out to MotionDetector
  3. 10fps MJPEG preview — fanned out to browser clients

The previous architecture spawned a separate ffmpeg per role, which
saturated the small concurrent-RTSP-client limits on consumer cameras
(the Eufy at 10.0.0.9 only allows one client). The unified pipeline
collapses everything into one process so a camera with a 1-client limit
still gets recording, motion detection, and live preview.

The lifecycle contract from commit 72332c8 is preserved:
  - start_new_session=True on spawn (process group kill semantics)
  - terminate_process_group() on stop (kills the whole tree)
  - Orphan ffmpegs from a previous SimpleNVR session are killed at
    startup by RecordingManager via kill_orphan_ffmpegs()
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import socket
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from asyncio.subprocess import PIPE
from asyncio import create_subprocess_exec as spawn_proc

from .. import db
from ..config import BITRATE_ROLLING_WINDOW, FFMPEG_RESTART_BACKOFF
from ..ffmpeg_path import get_ffprobe
from ..process_cleanup import terminate_process_group
from .codec import build_unified_cmd
from .frame_broadcaster import FrameBroadcaster

# Frame-staleness watchdog: if FFmpeg emits no stderr progress for this
# many seconds, kill it and let the restart loop take over. Catches the
# "alive-but-stalled" case where the RTSP socket is held open but no
# frames are flowing.
#
# The clock does NOT start at spawn — it starts on the FIRST stderr line.
# This excludes RTSP setup time (which can take 10-15s on slow cameras and
# is silent on stderr because libc fully-buffers pipes). 60s threshold
# gives generous headroom even if -progress pipe:2 hiccups.
STALE_FRAME_THRESHOLD_S = 60.0
STALE_CHECK_INTERVAL_S = 5.0

# JPEG SOI/EOI markers — used by both the motion stdout reader and the
# preview TCP reader to demux concatenated JPEGs from the ffmpeg pipe.
JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"

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
        encoder: str | None,
        encoder_flags: list[str] | None,
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
        self._watchdog_task: asyncio.Task | None = None
        self._motion_reader_task: asyncio.Task | None = None
        self._preview_acceptor_task: asyncio.Task | None = None
        self._preview_reader_task: asyncio.Task | None = None

        self._listen_sock: socket.socket | None = None
        self._preview_conn: socket.socket | None = None

        self._last_progress_ts: float = 0.0
        self._current_segment_path: Path | None = None
        self._current_segment_started_at: datetime | None = None
        self._current_segment_id: str | None = None
        self._running = False
        self._backoff_index = 0
        self._bitrate_history: deque[int] = deque(maxlen=BITRATE_ROLLING_WINDOW)

        # Broadcasters live for the entire CameraRecorder lifetime so that
        # subscribers (motion detector, browser preview) can attach once
        # and survive ffmpeg restarts transparently.
        self.preview_broadcaster = FrameBroadcaster(
            name=f"preview:{camera.id}", max_queue=5
        )
        self.motion_broadcaster = FrameBroadcaster(
            name=f"motion:{camera.id}", max_queue=10
        )

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
        if self._bitrate_history:
            return self.average_bitrate_bps

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
                return 0
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

    # ------------------------------------------------------------------
    # Subscription API — exposed to MotionDetector and the streams router
    # ------------------------------------------------------------------
    def subscribe_preview(self) -> asyncio.Queue:
        return self.preview_broadcaster.subscribe()

    def unsubscribe_preview(self, q: asyncio.Queue) -> None:
        self.preview_broadcaster.unsubscribe(q)

    def subscribe_motion(self) -> asyncio.Queue:
        return self.motion_broadcaster.subscribe()

    def unsubscribe_motion(self, q: asyncio.Queue) -> None:
        self.motion_broadcaster.unsubscribe(q)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
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

        # Open a TCP listen socket on a kernel-assigned loopback port for
        # the preview MJPEG output. ffmpeg connects to this port; Python
        # accepts and drains. We listen BEFORE spawning so the OS queues
        # the incoming connection — accept() can come a moment later.
        listen_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listen_sock.setblocking(False)
        listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listen_sock.bind(("127.0.0.1", 0))
        listen_sock.listen(1)
        preview_port = listen_sock.getsockname()[1]
        # listen=0 forces ffmpeg into client (connect) mode — explicit
        # so we don't accidentally rely on a future ffmpeg default change.
        preview_url = f"tcp://127.0.0.1:{preview_port}?listen=0"
        self._listen_sock = listen_sock

        cmd = build_unified_cmd(
            rtsp_uri=self.camera.rtsp_uri,
            output_pattern=output_pattern,
            segment_secs=self._settings.segment_duration_minutes * 60,
            fps_setting=self._settings.recording_fps,
            encoder=self._encoder,
            encoder_flags=self._encoder_flags,
            preview_tcp_url=preview_url,
        )

        logger.info(
            "Starting unified pipeline: %s (%s) preview_port=%d",
            self.camera.ip, self.camera.id, preview_port,
        )

        try:
            self._proc = await spawn_proc(
                *cmd,
                stdout=PIPE,
                stderr=PIPE,
                limit=1024 * 1024,
                start_new_session=True,
            )
        except FileNotFoundError:
            logger.error("ffmpeg not found in PATH")
            self._running = False
            self._close_listen_sock()
            return

        # Watchdog clock stays at 0.0 (dormant) until first stderr line.
        self._last_progress_ts = 0.0
        # Reset broadcasters so a brief restart doesn't replay an obsolete
        # frame from the previous ffmpeg generation.
        self.preview_broadcaster.reset()
        self.motion_broadcaster.reset()

        self._watcher_task = asyncio.create_task(self._stderr_watcher())
        self._monitor_task = asyncio.create_task(self._process_monitor())
        self._watchdog_task = asyncio.create_task(self._staleness_watchdog())
        self._motion_reader_task = asyncio.create_task(self._motion_pipe_reader())
        self._preview_acceptor_task = asyncio.create_task(self._preview_acceptor())

        await self._event_bus.emit(
            "recording_started", {"camera_id": self.camera.id}
        )

    def _close_listen_sock(self) -> None:
        if self._listen_sock is not None:
            try:
                self._listen_sock.close()
            except Exception:
                pass
            self._listen_sock = None

    def _close_preview_conn(self) -> None:
        if self._preview_conn is not None:
            try:
                self._preview_conn.close()
            except Exception:
                pass
            self._preview_conn = None

    async def stop(self) -> None:
        self._running = False
        if self._proc is None:
            self._close_listen_sock()
            return

        # SIGTERM the whole process group so FFmpeg (and any helper
        # children spawned by the fifo muxer) all get the signal and
        # can finalize cleanly. This is the lifecycle contract from 72332c8.
        terminate_process_group(self._proc, signal.SIGTERM)

        try:
            # 30s grace allows long-keyframe-interval cameras to finalize
            # segments cleanly
            await asyncio.wait_for(self._proc.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            logger.warning("FFmpeg did not exit cleanly, killing")
            terminate_process_group(self._proc, signal.SIGKILL)
            try:
                await self._proc.wait()
            except Exception:
                pass

        for task in (
            self._watcher_task,
            self._monitor_task,
            self._watchdog_task,
            self._motion_reader_task,
            self._preview_acceptor_task,
            self._preview_reader_task,
        ):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        self._close_preview_conn()
        self._close_listen_sock()

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

    # ------------------------------------------------------------------
    # FFmpeg stderr watcher (segment detection + watchdog heartbeat)
    # ------------------------------------------------------------------
    async def _stderr_watcher(self) -> None:
        if self._proc is None or self._proc.stderr is None:
            return

        try:
            while True:
                try:
                    line = await self._proc.stderr.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    try:
                        await self._proc.stderr.read(1024 * 1024)
                    except Exception:
                        pass
                    continue

                if not line:
                    break
                # Any stderr line means FFmpeg is alive and talking —
                # the watchdog treats this as a progress heartbeat.
                self._last_progress_ts = time.monotonic()
                decoded = line.decode("utf-8", errors="replace").rstrip()

                match = SEGMENT_OPEN_RE.search(decoded)
                if match:
                    new_path = Path(match.group(1))
                    prev_path = self._current_segment_path
                    prev_started = self._current_segment_started_at

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

    async def _staleness_watchdog(self) -> None:
        try:
            while self._running and self._proc is not None:
                await asyncio.sleep(STALE_CHECK_INTERVAL_S)
                if self._proc is None or self._proc.returncode is not None:
                    return
                if self._last_progress_ts == 0.0:
                    continue
                elapsed = time.monotonic() - self._last_progress_ts
                if elapsed > STALE_FRAME_THRESHOLD_S:
                    logger.warning(
                        "FFmpeg for %s appears stalled (%.1fs no progress), "
                        "terminating to trigger restart",
                        self.camera.ip,
                        elapsed,
                    )
                    terminate_process_group(self._proc, signal.SIGTERM)
                    return
        except asyncio.CancelledError:
            raise

    # ------------------------------------------------------------------
    # Motion pipe reader (stdout — scene-filtered MJPEG frames)
    # ------------------------------------------------------------------
    async def _motion_pipe_reader(self) -> None:
        """Drain ffmpeg stdout, demux JPEG frames, publish to motion broadcaster."""
        if self._proc is None or self._proc.stdout is None:
            return
        buffer = bytearray()
        try:
            while True:
                chunk = await self._proc.stdout.read(8192)
                if not chunk:
                    break
                buffer.extend(chunk)
                # Demux concatenated JPEGs by SOI/EOI markers.
                while True:
                    start = buffer.find(JPEG_SOI)
                    if start < 0:
                        buffer.clear()
                        break
                    end = buffer.find(JPEG_EOI, start + 2)
                    if end < 0:
                        if start > 0:
                            del buffer[:start]
                        break
                    end += 2
                    frame = bytes(buffer[start:end])
                    del buffer[:end]
                    self.motion_broadcaster.publish(frame)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Motion pipe reader error for %s: %s", self.camera.ip, e)

    # ------------------------------------------------------------------
    # Preview TCP acceptor + reader (10fps MJPEG over loopback)
    # ------------------------------------------------------------------
    async def _preview_acceptor(self) -> None:
        """Wait for ffmpeg to connect to our preview listen socket, then
        hand off to the reader task."""
        if self._listen_sock is None:
            return
        loop = asyncio.get_running_loop()
        try:
            conn, _addr = await loop.sock_accept(self._listen_sock)
            conn.setblocking(False)
            self._preview_conn = conn
            self._preview_reader_task = asyncio.create_task(
                self._preview_tcp_reader(conn)
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(
                "Preview acceptor error for %s: %s", self.camera.ip, e
            )
        finally:
            # We only ever accept one connection per ffmpeg generation.
            # Close the listening socket so the port is freed.
            self._close_listen_sock()

    async def _preview_tcp_reader(self, conn: socket.socket) -> None:
        """Drain the preview MJPEG TCP stream and publish JPEG frames."""
        loop = asyncio.get_running_loop()
        buffer = bytearray()
        try:
            while True:
                try:
                    chunk = await loop.sock_recv(conn, 16384)
                except (ConnectionResetError, OSError):
                    break
                if not chunk:
                    break
                buffer.extend(chunk)
                while True:
                    start = buffer.find(JPEG_SOI)
                    if start < 0:
                        buffer.clear()
                        break
                    end = buffer.find(JPEG_EOI, start + 2)
                    if end < 0:
                        if start > 0:
                            del buffer[:start]
                        break
                    end += 2
                    frame = bytes(buffer[start:end])
                    del buffer[:end]
                    self.preview_broadcaster.publish(frame)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(
                "Preview TCP reader error for %s: %s", self.camera.ip, e
            )

    # ------------------------------------------------------------------
    # Segment finalize (unchanged from pre-unified architecture)
    # ------------------------------------------------------------------
    async def _ffprobe_duration(self, path: Path) -> float | None:
        try:
            proc = await spawn_proc(
                get_ffprobe(),
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "json",
                str(path),
                stdout=PIPE,
                stderr=PIPE,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("ffprobe hung on %s, giving up", path)
                try:
                    proc.kill()
                except Exception:
                    pass
                return None
            if proc.returncode != 0:
                return None
            data = json.loads(stdout.decode("utf-8", errors="replace") or "{}")
            dur = data.get("format", {}).get("duration")
            return float(dur) if dur is not None else None
        except Exception as e:
            logger.warning("ffprobe error on %s: %s", path, e)
            return None

    async def _finalize_segment(
        self, path: Path, started_at: datetime | None = None
    ) -> None:
        try:
            if not path.exists():
                logger.warning("Segment file missing: %s", path)
                return

            if getattr(self._settings, "validate_segments", False):
                expected = self._settings.segment_duration_minutes * 60
                tolerance = max(10.0, expected * 0.2)
                probed = await self._ffprobe_duration(path)
                if probed is None or abs(probed - expected) > tolerance:
                    logger.warning(
                        "Deleting corrupt segment %s (probed=%s expected=%s)",
                        path, probed, expected,
                    )
                    try:
                        os.unlink(path)
                    except Exception:
                        pass
                    try:
                        await self._event_bus.emit(
                            "recording_stopped",
                            {"camera_id": self.camera.id, "corrupt_segment": str(path)},
                        )
                    except Exception:
                        pass
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

        # Tear down the per-generation reader tasks and sockets so the
        # next _spawn() starts clean.
        for task in (
            self._motion_reader_task,
            self._preview_acceptor_task,
            self._preview_reader_task,
        ):
            if task and not task.done():
                task.cancel()
        self._close_preview_conn()
        self._close_listen_sock()

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
