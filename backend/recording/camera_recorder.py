"""
CameraRecorder — owns ONE FFmpeg process per camera that opens a single
RTSP connection (via go2rtc's loopback) and produces two outputs:

  1. Segmented MP4 recording (stream-copy by default) — written to disk
  2. Scene-filtered motion frames — fanned out to MotionDetector

Live browser preview is a third output, but it is NOT produced by
this ffmpeg process. The frontend connects directly to go2rtc (via
the Vite dev proxy or the Tauri equivalent) and consumes WebRTC/MSE
streams from go2rtc's own player pipeline. go2rtc is already in the
stack because we use its loopback RTSP as the sole RTSP client per
camera (Eufy at 10.0.0.9 only allows one concurrent client), and
since it's already decoding the stream for its own consumers, having
it serve browser preview too is free. Historical versions of this
module added a third MJPEG-over-TCP branch to the unified ffmpeg
command for browser preview; that code was removed 2026-04-09 after
live-preview migrated to go2rtc.

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
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from asyncio.subprocess import PIPE
from asyncio import create_subprocess_exec as spawn_proc

from .. import db, go2rtc_client
from ..config import BITRATE_ROLLING_WINDOW, FFMPEG_RESTART_BACKOFF
from ..ffmpeg_path import get_ffprobe
from ..process_cleanup import terminate_process_group
from .codec import build_unified_cmd
from .frame_broadcaster import FrameBroadcaster

# Frame-staleness watchdog: kill ffmpeg if neither stderr nor the on-disk
# segment file have shown progress for this many seconds. The two signals
# together cover both the "alive-but-stalled RTSP socket" case AND the
# "intermittent camera that goes silent on stderr but is otherwise fine"
# case (Eufy at 10.0.0.9 is the canonical example — its RTSP server emits
# packets in bursts with quiet windows >60s, but the recording segment file
# DOES grow whenever bursts arrive, so file-growth is a strictly stronger
# liveness signal than -progress pipe:2 stats).
#
# The clock does NOT start at spawn — it starts on the FIRST stderr line
# OR the first observed segment file growth. This excludes RTSP setup time
# (which can take 10-15s on slow cameras and is silent on stderr because
# libc fully-buffers pipes).
STALE_FRAME_THRESHOLD_S = 120.0
STALE_CHECK_INTERVAL_S = 5.0

# Health state thresholds. Distinct from STALE_FRAME_THRESHOLD_S
# above (which triggers an ffmpeg kill + restart). These are the
# thresholds the watchdog uses to *label* the camera's current state
# so the frontend can render it on the live tile. A camera stays
# "ok" as long as packets are flowing within the last
# HEALTH_STALLED_THRESHOLD_S seconds; slides into "stalled" in the
# 15-60s window where it's probably a brief hiccup; and escalates
# to "offline" at HEALTH_OFFLINE_THRESHOLD_S — still well before
# the 120s kill threshold so the UI shows the outage before the
# restart cycle kicks in.
HEALTH_STALLED_THRESHOLD_S = 15.0
HEALTH_OFFLINE_THRESHOLD_S = 60.0

# Stderr tail ring buffer for the D#1 rc=255 diagnostic. When ffmpeg
# exits unexpectedly we want to see WHAT it was complaining about, not
# just the exit code. The stderr_watcher drains ffmpeg's stderr line by
# line — we keep the most recent N lines in memory so the process
# monitor can dump them when the exit code is non-zero. 30 lines is
# enough to capture ffmpeg's preamble (input probe, codec selection)
# plus any error at the end without bloating RSS on long-running
# cameras.
_STDERR_TAIL_LINES = 30

# Fast-fail threshold for flagging rc=255-class bugs. If a spawned
# ffmpeg lives less than this many seconds before exiting, it almost
# certainly died in RTSP negotiation / codec negotiation / auth —
# not from a mid-stream network hiccup. Label the log line so it's
# obvious this is the same D#1 failure class and not a transient.
_FAST_FAIL_THRESHOLD_S = 5.0

# JPEG SOI/EOI markers — used by the motion stdout reader to demux
# concatenated JPEGs from ffmpeg's scene-filtered MJPEG output.
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

        self._last_progress_ts: float = 0.0
        self._last_segment_size: int = 0
        # Most recent health state we've emitted to the event bus.
        # The watchdog only fires camera_health events on transitions
        # so healthy cameras stay silent. None on spawn — the first
        # time we observe packet flow we emit "ok"; the first time we
        # cross the stalled/offline thresholds we emit those.
        self._last_emitted_health: str | None = None
        # Ring buffer of recent ffmpeg stderr lines for post-mortem
        # diagnosis when the process exits unexpectedly. See the
        # _STDERR_TAIL_LINES constant at the top of this module for
        # the D#1 rationale. Populated by _stderr_watcher, drained by
        # _process_monitor.
        self._stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
        # Monotonic timestamp of the last successful ffmpeg spawn. Used
        # to detect fast-fail exits (process lived < _FAST_FAIL_THRESHOLD_S)
        # which are characteristic of the D#1 rc=255 class — failures in
        # RTSP/codec/auth negotiation rather than mid-stream hiccups.
        self._spawn_monotonic_ts: float = 0.0
        self._current_segment_path: Path | None = None
        self._current_segment_started_at: datetime | None = None
        self._current_segment_id: str | None = None
        self._running = False
        self._backoff_index = 0
        self._bitrate_history: deque[int] = deque(maxlen=BITRATE_ROLLING_WINDOW)

        # The motion broadcaster lives for the entire CameraRecorder
        # lifetime so MotionDetector can subscribe once and survive
        # ffmpeg restarts transparently. No preview broadcaster here
        # anymore — browser live preview comes from go2rtc directly.
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
    # Subscription API — exposed to MotionDetector
    # ------------------------------------------------------------------
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
        try:
            await self._spawn()
        except Exception:
            # Any failure in _spawn() (socket bind errors, subprocess
            # creation failures other than FileNotFoundError, task
            # creation errors) leaves the recorder in a half-started
            # state: _running=True with no subprocess and no monitor.
            # The old code left _running=True forever, which made the
            # `is_running` guard in RecordingManager block recovery
            # attempts permanently — the only way out was a full
            # backend restart. Reset _running here so the manager's
            # stop-before-overwrite path can reach a fresh spawn.
            self._running = False
            raise

    async def _spawn(self) -> None:
        cam_dir = self._recordings_dir / self.camera.id
        today_dir = cam_dir / datetime.now().strftime("%Y-%m-%d")
        today_dir.mkdir(parents=True, exist_ok=True)

        # FFmpeg's strftime expansion produces e.g.
        # data/recordings/{cam_id}/2026-04-05/14-30-00.mp4
        output_pattern = cam_dir / "%Y-%m-%d" / "%H-%M-%S.mp4"

        # ── go2rtc loopback decision ──────────────────────────────────
        # If the Tauri shell handed us a go2rtc URL AND we can register
        # this camera's stream there, point ffmpeg's RTSP input at the
        # loopback instead of opening a fresh socket to the camera.
        # The discovery scanner has usually already registered the
        # stream, but doing it again here is idempotent (PUT) and
        # protects the recorder against the case where go2rtc was
        # restarted while the camera was still in our recorders dict.
        # On any failure, fall back transparently to the direct URL —
        # recording must keep working even if go2rtc is unhealthy.
        # The stored rtsp_uri is credential-free (see backend/rtsp_url.py).
        # Rebuild the authenticated upstream URL here — it's the single
        # string go2rtc or ffmpeg needs to actually open a connection to
        # the camera.
        from ..rtsp_url import authed_uri

        upstream_uri = authed_uri(self.camera)
        input_uri = upstream_uri
        loopback_uri = None
        if go2rtc_client.is_enabled() and upstream_uri:
            ok = await go2rtc_client.add_stream(self.camera.id, upstream_uri)
            if ok:
                loopback_uri = go2rtc_client.loopback_url_for(self.camera.id)
                if loopback_uri:
                    input_uri = loopback_uri
            else:
                logger.warning(
                    "go2rtc registration failed for %s; falling back to "
                    "direct camera URL",
                    self.camera.ip,
                )

        cmd = build_unified_cmd(
            rtsp_uri=input_uri,
            output_pattern=output_pattern,
            segment_secs=self._settings.segment_duration_minutes * 60,
            fps_setting=self._settings.recording_fps,
            encoder=self._encoder,
            encoder_flags=self._encoder_flags,
        )

        # Wrap the ffmpeg invocation in tether (our cross-platform
        # parent-death supervisor) when the Tauri parent provided the
        # binary path via SIMPLENVR_TETHER_BIN. This guarantees every
        # ffmpeg child dies if this Python process dies for any reason
        # — on macOS via stdin-EOF watchdog, on Linux via PR_SET_PDEATHSIG,
        # on Windows via Job Object. Without tether (dev mode, running
        # `python -m backend.main` from a terminal without the env var),
        # we fall back to the old behaviour and rely on the FastAPI
        # lifespan + terminate_process_group to clean up on graceful
        # shutdown only.
        tether_bin = os.environ.get("SIMPLENVR_TETHER_BIN")
        if tether_bin and Path(tether_bin).exists():
            cmd = [tether_bin, *cmd]
            via_tether = True
        else:
            via_tether = False

        logger.info(
            "Starting unified pipeline: %s (%s) via=%s tether=%s",
            self.camera.ip,
            self.camera.id,
            "go2rtc" if loopback_uri else "direct",
            "yes" if via_tether else "no",
        )

        try:
            self._proc = await spawn_proc(
                *cmd,
                stdin=PIPE,  # tether's macOS watchdog reads stdin for EOF
                stdout=PIPE,
                stderr=PIPE,
                limit=1024 * 1024,
                start_new_session=True,
            )
        except FileNotFoundError:
            logger.error("ffmpeg not found in PATH")
            self._running = False
            return

        # Watchdog clock stays at 0.0 (dormant) until first stderr line OR
        # first observed segment file growth.
        self._last_progress_ts = 0.0
        self._last_segment_size = 0
        # Reset health state so a post-restart recorder starts clean
        # — otherwise an "offline" that triggered the restart would
        # linger as the last_emitted_health and suppress the first
        # "ok" emit after ffmpeg comes back up.
        self._last_emitted_health = None
        # Reset the stderr tail so a restart's diagnostic dump only
        # reflects the current ffmpeg generation, not the previous one.
        self._stderr_tail.clear()
        self._spawn_monotonic_ts = time.monotonic()
        # Reset the motion broadcaster so a brief restart doesn't
        # replay an obsolete frame from the previous ffmpeg generation.
        self.motion_broadcaster.reset()

        self._watcher_task = asyncio.create_task(self._stderr_watcher())
        self._monitor_task = asyncio.create_task(self._process_monitor())
        self._watchdog_task = asyncio.create_task(self._staleness_watchdog())
        self._motion_reader_task = asyncio.create_task(self._motion_pipe_reader())

        await self._event_bus.emit(
            "recording_started", {"camera_id": self.camera.id}
        )

    async def stop(self) -> None:
        self._running = False
        if self._proc is None:
            return

        # SIGTERM the whole process group. The FFmpeg process is
        # spawned with start_new_session=True so it leads its own
        # session/group; signaling the group covers any future
        # multi-process output topology (e.g. tee muxer, fifo
        # demuxer) without having to teach this code about each
        # variant. This is the lifecycle contract from 72332c8.
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
        ):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        # Unblock the motion detector if it's currently subscribed
        # to our motion broadcaster. Without this, its consume loop
        # would spin forever on an empty queue (the motion pipe
        # reader has already exited and nothing will ever publish
        # another frame). The None sentinel from close() causes the
        # consume loop to exit cleanly.
        self.motion_broadcaster.close()

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
                # Capture into the rolling tail buffer for post-mortem
                # diagnosis on unexpected exit (D#1). Filtering out
                # empty lines keeps the tail dense.
                if decoded:
                    self._stderr_tail.append(decoded)

                match = SEGMENT_OPEN_RE.search(decoded)
                if match:
                    new_path = Path(match.group(1))
                    prev_path = self._current_segment_path
                    prev_started = self._current_segment_started_at

                    new_path.parent.mkdir(parents=True, exist_ok=True)

                    self._current_segment_path = new_path
                    self._current_segment_started_at = datetime.now(timezone.utc)
                    self._current_segment_id = str(uuid.uuid4())
                    # Reset the per-segment growth baseline so the watchdog
                    # measures the new file from zero.
                    self._last_segment_size = 0

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

                # Secondary liveness signal: segment file size growth.
                # Some cameras (e.g. Eufy) emit RTSP data in bursts and go
                # silent on ffmpeg stderr for >60s windows even though
                # recording is healthy. The on-disk segment file growing
                # is an unambiguous "ffmpeg is processing packets" signal.
                if self._current_segment_path is not None:
                    try:
                        size = self._current_segment_path.stat().st_size
                    except OSError:
                        size = 0
                    if size > self._last_segment_size:
                        self._last_segment_size = size
                        self._last_progress_ts = time.monotonic()

                if self._last_progress_ts == 0.0:
                    continue
                elapsed = time.monotonic() - self._last_progress_ts

                # Derive and emit health state transitions. The three
                # states map to UI treatment: ok = green live tile,
                # stalled = amber corner badge, offline = red corner
                # badge with "last live Nm ago". Only transitions are
                # emitted — a healthy camera generates zero events
                # here, so the event bus stays quiet in the steady
                # state.
                if elapsed < HEALTH_STALLED_THRESHOLD_S:
                    new_health = "ok"
                elif elapsed < HEALTH_OFFLINE_THRESHOLD_S:
                    new_health = "stalled"
                else:
                    new_health = "offline"

                if new_health != self._last_emitted_health:
                    self._last_emitted_health = new_health
                    # last_frame_at is derived from _last_progress_ts
                    # (a monotonic clock) by anchoring to wall-clock
                    # now and subtracting elapsed. Good enough for
                    # UI labels; not intended for forensics.
                    last_frame_at = datetime.now(timezone.utc)
                    if elapsed > 0:
                        from datetime import timedelta

                        last_frame_at = last_frame_at - timedelta(seconds=elapsed)
                    try:
                        await self._event_bus.emit(
                            "camera_health",
                            {
                                "camera_id": self.camera.id,
                                "health": new_health,
                                "last_frame_at": last_frame_at.isoformat(),
                            },
                        )
                    except Exception as e:
                        # Event bus failures must never take down the
                        # watchdog — recording correctness is the
                        # priority. Log and continue.
                        logger.warning(
                            "camera_health emit failed for %s: %s",
                            self.camera.ip,
                            e,
                        )

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

        # Tear down the per-generation reader task so the next
        # _spawn() starts clean.
        if self._motion_reader_task and not self._motion_reader_task.done():
            self._motion_reader_task.cancel()

        if self._running:
            delay = FFMPEG_RESTART_BACKOFF[
                min(self._backoff_index, len(FFMPEG_RESTART_BACKOFF) - 1)
            ]
            self._backoff_index += 1
            rc = self._proc.returncode if self._proc is not None else None

            # D#1 diagnostic: detect fast-fail (process lived less than
            # _FAST_FAIL_THRESHOLD_S) and dump the stderr tail so next
            # session can see WHAT ffmpeg actually complained about
            # instead of just the exit code. Without this, rc=255 loops
            # are completely opaque.
            alive_for = (
                time.monotonic() - self._spawn_monotonic_ts
                if self._spawn_monotonic_ts > 0.0
                else 0.0
            )
            fast_fail = alive_for < _FAST_FAIL_THRESHOLD_S and rc not in (0, None)
            label = "FAST-FAIL" if fast_fail else "exit"
            logger.warning(
                "FFmpeg for %s %s (rc=%s, alive=%.1fs), restarting in %ds",
                self.camera.ip,
                label,
                rc,
                alive_for,
                delay,
            )
            if fast_fail or rc not in (0, None):
                tail = list(self._stderr_tail)
                if tail:
                    logger.warning(
                        "FFmpeg stderr tail for %s (%d lines):\n  %s",
                        self.camera.ip,
                        len(tail),
                        "\n  ".join(tail),
                    )
                else:
                    logger.warning(
                        "FFmpeg stderr tail for %s is EMPTY — process died "
                        "before writing any diagnostic output (likely spawn "
                        "failure or immediate SIGPIPE)",
                        self.camera.ip,
                    )
            await asyncio.sleep(delay)
            if self._running:
                await self._spawn()
