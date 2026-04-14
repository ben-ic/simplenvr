"""DetectFfmpegSource — one ffmpeg per camera producing decoded RGB24 frames
for the detect pipeline.

Sibling to `backend.recording.camera_recorder.CameraRecorder`, but
single-purpose: decode a low-fps, 640-wide rawvideo stream from the go2rtc
loopback RTSP and drop each frame into a `NewestFrameSlot` for the
detector task. Audio is explicitly dropped here — audio has its own
pipeline (see recording/audio_broadcaster.py and the YAMNet path).

Output shape is derived at spawn time by ffprobing the source and
computing `height = round(src_h * 640 / src_w / 2) * 2` — ffmpeg's
`scale=640:-2` filter produces exactly that even integer.

Lifecycle mirrors CameraRecorder:
  - `start_new_session=True` so SIGTERM/SIGKILL cleans up the whole group
  - stderr drained in parallel (and a ring of the last ~32 lines kept for
    the restart log)
  - exponential backoff on restart (1s, 2s, 4s, cap 30s; reset on >=60s
    of continuous success)
  - `stop()` is a 2s SIGTERM grace then SIGKILL
  - `set_fps(new_fps)` is implemented as a restart; a brief frame gap is
    acceptable for a 2 fps pipeline
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from collections import deque
from pathlib import Path
from typing import Optional

from asyncio import create_subprocess_exec as spawn_proc
from asyncio.subprocess import PIPE

import numpy as np

from ..ffmpeg_path import get_ffmpeg, get_ffprobe
from ..process_cleanup import terminate_process_group
from .shm_ring import NewestFrameSlot

# Output width is fixed at 640. `scale=640:-2` forces even height.
OUTPUT_WIDTH = 640

# Backoff schedule for ffmpeg restarts — matches the shape used elsewhere
# (recording uses FFMPEG_RESTART_BACKOFF from config). Kept inline here
# because v1 has no detect-specific config knobs.
_BACKOFF_SEQ = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
# If ffmpeg stays up this long, we consider the camera healthy and reset
# the backoff index so the next failure starts fresh at 1s.
_SUCCESS_RESET_S = 60.0

# ffprobe timeout — short because the source is our own go2rtc loopback,
# which is always local and always already publishing.
_FFPROBE_TIMEOUT_S = 3.0

# How many stderr lines to keep for the restart log.
_STDERR_TAIL_LINES = 32

# Clean-shutdown grace before SIGKILL.
_STOP_GRACE_S = 2.0


def _round_even(n: float) -> int:
    """Round to the nearest even integer, matching ffmpeg's scale=W:-2."""
    return int(round(n / 2.0) * 2)


class DetectFfmpegSource:
    """Supervises one ffmpeg that emits rawvideo RGB24 frames at a low fps.

    Construction takes the camera id, the full RTSP URL (almost always the
    go2rtc loopback), the target `NewestFrameSlot`, an initial fps, and
    the ffmpeg/ffprobe binary paths. A logger may be supplied; by default
    it uses `logging.getLogger(__name__)`.
    """

    def __init__(
        self,
        camera_id: str,
        rtsp_url: str,
        slot: NewestFrameSlot,
        *,
        fps: int = 2,
        ffmpeg_path: Optional[str] = None,
        ffprobe_path: Optional[str] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.camera_id = camera_id
        self.rtsp_url = rtsp_url
        self.slot = slot
        self._fps = int(fps)
        self._ffmpeg_path = ffmpeg_path or get_ffmpeg()
        self._ffprobe_path = ffprobe_path or get_ffprobe()
        self._logger = logger or logging.getLogger(__name__)

        self._width: int = 0
        self._height: int = 0

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._supervise_task: Optional[asyncio.Task[None]] = None
        self._stderr_task: Optional[asyncio.Task[None]] = None
        self._stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)

        self._running = False
        self._restarting_for_fps = False
        # Event set when the current ffmpeg generation has exited and been
        # reaped; used by set_fps to sequence restarts cleanly.
        self._gen_done = asyncio.Event()
        self._gen_done.set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    @property
    def width(self) -> int:
        return self._width

    @property
    def height(self) -> int:
        return self._height

    @property
    def is_running(self) -> bool:
        return self._running and self._proc is not None and self._proc.returncode is None

    async def start(self) -> None:
        if self._running:
            return
        # Probe once up-front to learn the output height. If the probe
        # fails here we bail cleanly rather than entering the restart
        # loop — the supervisor will re-probe inside the loop on each
        # restart anyway, so a transient failure here is recoverable by
        # calling start() again later.
        probed = await self._probe_output_dims()
        if probed is None:
            self._logger.error(
                "detect[%s]: initial ffprobe failed for %s; not starting",
                self.camera_id, self.rtsp_url,
            )
            return
        self._width, self._height = probed
        self._running = True
        self._supervise_task = asyncio.create_task(
            self._supervise_loop(), name=f"detect-ffmpeg-{self.camera_id}"
        )

    async def stop(self) -> None:
        self._running = False
        await self._terminate_current_proc()
        task = self._supervise_task
        self._supervise_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def set_fps(self, new_fps: int) -> None:
        """Change the output fps. Implemented as a restart of the ffmpeg
        subprocess; a brief frame gap is acceptable."""
        new_fps = int(new_fps)
        if new_fps <= 0:
            raise ValueError(f"fps must be positive, got {new_fps}")
        if new_fps == self._fps:
            return
        self._fps = new_fps
        if not self._running:
            return
        self._logger.info(
            "detect[%s]: fps change -> %d; restarting ffmpeg",
            self.camera_id, new_fps,
        )
        # Flag so the supervise loop knows this exit was intentional and
        # should not count against backoff.
        self._restarting_for_fps = True
        await self._terminate_current_proc()
        # Wait for the supervisor to reap and respawn. We don't block
        # callers forever — a best-effort short wait is enough.
        try:
            await asyncio.wait_for(self._gen_done.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass

    # ------------------------------------------------------------------
    # Internal — supervise loop
    # ------------------------------------------------------------------
    async def _supervise_loop(self) -> None:
        backoff_idx = 0
        while self._running:
            spawn_ts = time.monotonic()
            self._gen_done.clear()
            try:
                await self._run_one_generation()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._logger.warning(
                    "detect[%s]: generation crashed: %s", self.camera_id, e,
                )
            finally:
                self._gen_done.set()

            if not self._running:
                break

            if self._restarting_for_fps:
                self._restarting_for_fps = False
                # Clean fps-change cycle, no backoff. Reprobe output dims
                # and spin immediately.
                probed = await self._probe_output_dims()
                if probed is not None:
                    self._width, self._height = probed
                continue

            lifetime = time.monotonic() - spawn_ts
            if lifetime >= _SUCCESS_RESET_S:
                backoff_idx = 0

            wait_s = _BACKOFF_SEQ[min(backoff_idx, len(_BACKOFF_SEQ) - 1)]
            backoff_idx = min(backoff_idx + 1, len(_BACKOFF_SEQ) - 1)
            self._logger.warning(
                "detect[%s]: ffmpeg exited after %.1fs; tail=%s; retry in %.0fs",
                self.camera_id, lifetime,
                list(self._stderr_tail)[-6:],
                wait_s,
            )
            try:
                await asyncio.sleep(wait_s)
            except asyncio.CancelledError:
                break

            # Re-probe before respawn — the source may have changed
            # resolution (camera reboot, profile switch). Cheap in the
            # common case.
            probed = await self._probe_output_dims()
            if probed is not None:
                self._width, self._height = probed

    async def _run_one_generation(self) -> None:
        """Spawn ffmpeg, read frames, drain stderr, wait for exit."""
        if self._width <= 0 or self._height <= 0:
            # Can happen after a reprobe failure between restarts.
            self._logger.warning(
                "detect[%s]: no valid dims; skipping spawn this round",
                self.camera_id,
            )
            # Yield a touch so the backoff layer paces the next attempt.
            await asyncio.sleep(0.1)
            return

        cmd = [
            self._ffmpeg_path,
            "-hide_banner", "-loglevel", "warning",
            "-rtsp_transport", "tcp",
            "-i", self.rtsp_url,
            "-vf", f"scale={OUTPUT_WIDTH}:-2,fps={self._fps}",
            "-f", "rawvideo", "-pix_fmt", "rgb24",
            "-an",
            "pipe:1",
        ]
        tether_bin = os.environ.get("SIMPLENVR_TETHER_BIN")
        if tether_bin and Path(tether_bin).exists():
            cmd = [tether_bin, *cmd]

        self._logger.info(
            "detect[%s]: spawn ffmpeg %dx%d @ %d fps",
            self.camera_id, self._width, self._height, self._fps,
        )

        try:
            self._proc = await spawn_proc(
                *cmd,
                stdin=PIPE,  # tether's macOS stdin-EOF watchdog
                stdout=PIPE,
                stderr=PIPE,
                limit=4 * 1024 * 1024,  # room for one frame in stdout buffer
                start_new_session=True,
            )
        except FileNotFoundError:
            self._logger.error(
                "detect[%s]: ffmpeg not found at %s",
                self.camera_id, self._ffmpeg_path,
            )
            return

        self._stderr_tail.clear()
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(),
            name=f"detect-ffmpeg-stderr-{self.camera_id}",
        )

        try:
            await self._read_frames_loop()
        finally:
            # Reap: ensure the process is gone and the stderr drain is
            # finished before we return to the supervise loop.
            await self._terminate_current_proc()
            if self._stderr_task and not self._stderr_task.done():
                try:
                    await asyncio.wait_for(self._stderr_task, timeout=1.0)
                except (asyncio.TimeoutError, Exception):
                    self._stderr_task.cancel()
            self._stderr_task = None

    # ------------------------------------------------------------------
    # Internal — frame reader
    # ------------------------------------------------------------------
    async def _read_frames_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        stdout = self._proc.stdout
        frame_bytes = self._width * self._height * 3
        shape = (self._height, self._width, 3)
        while True:
            try:
                buf = await stdout.readexactly(frame_bytes)
            except asyncio.IncompleteReadError as e:
                if e.partial:
                    self._logger.debug(
                        "detect[%s]: short read (%d/%d); ffmpeg ending",
                        self.camera_id, len(e.partial), frame_bytes,
                    )
                break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._logger.warning(
                    "detect[%s]: stdout read error: %s", self.camera_id, e,
                )
                break
            # `np.frombuffer` on immutable bytes yields a read-only view;
            # reshape without copy. Consumers that mutate should copy.
            try:
                frame = np.frombuffer(buf, dtype=np.uint8).reshape(shape)
            except ValueError:
                # Byte count mismatch vs probed dims — source almost
                # certainly changed resolution. Force a restart.
                self._logger.warning(
                    "detect[%s]: frame reshape failed; source dims drifted?",
                    self.camera_id,
                )
                break
            self.slot.put(frame)

    # ------------------------------------------------------------------
    # Internal — stderr drain
    # ------------------------------------------------------------------
    async def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                try:
                    text = line.decode("utf-8", errors="replace").rstrip()
                except Exception:
                    text = repr(line)
                if text:
                    self._stderr_tail.append(text)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._logger.debug("detect[%s]: stderr drain ended: %s",
                               self.camera_id, e)

    # ------------------------------------------------------------------
    # Internal — termination
    # ------------------------------------------------------------------
    async def _terminate_current_proc(self) -> None:
        proc = self._proc
        if proc is None:
            return
        if proc.returncode is None:
            terminate_process_group(proc, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=_STOP_GRACE_S)
            except asyncio.TimeoutError:
                self._logger.warning(
                    "detect[%s]: SIGTERM grace expired; SIGKILL",
                    self.camera_id,
                )
                terminate_process_group(proc, signal.SIGKILL)
                try:
                    await proc.wait()
                except Exception:
                    pass
            except Exception:
                pass
        self._proc = None

    # ------------------------------------------------------------------
    # Internal — ffprobe
    # ------------------------------------------------------------------
    async def _probe_output_dims(self) -> Optional[tuple[int, int]]:
        """Run ffprobe on the RTSP source and compute the ffmpeg output
        dims. Returns (width, height) or None on failure/timeout."""
        cmd = [
            self._ffprobe_path,
            "-v", "error",
            "-rtsp_transport", "tcp",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "json",
            self.rtsp_url,
        ]
        try:
            proc = await spawn_proc(
                *cmd,
                stdout=PIPE,
                stderr=PIPE,
                start_new_session=(sys.platform != "win32"),
            )
        except FileNotFoundError:
            self._logger.error(
                "detect[%s]: ffprobe not found at %s",
                self.camera_id, self._ffprobe_path,
            )
            return None

        try:
            stdout, _stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_FFPROBE_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            self._logger.warning(
                "detect[%s]: ffprobe timed out after %.1fs",
                self.camera_id, _FFPROBE_TIMEOUT_S,
            )
            try:
                terminate_process_group(proc, signal.SIGKILL)
            except Exception:
                pass
            try:
                await proc.wait()
            except Exception:
                pass
            return None

        if proc.returncode != 0:
            self._logger.warning(
                "detect[%s]: ffprobe exit=%s", self.camera_id, proc.returncode,
            )
            return None

        try:
            data = json.loads(stdout.decode("utf-8", errors="replace") or "{}")
            stream = (data.get("streams") or [{}])[0]
            src_w = int(stream.get("width") or 0)
            src_h = int(stream.get("height") or 0)
        except (ValueError, json.JSONDecodeError, KeyError, IndexError) as e:
            self._logger.warning(
                "detect[%s]: ffprobe parse error: %s", self.camera_id, e,
            )
            return None

        if src_w <= 0 or src_h <= 0:
            self._logger.warning(
                "detect[%s]: ffprobe returned invalid dims %sx%s",
                self.camera_id, src_w, src_h,
            )
            return None

        # scale=640:-2 => height rounded to nearest even integer.
        out_w = OUTPUT_WIDTH
        out_h = max(2, _round_even(src_h * OUTPUT_WIDTH / src_w))
        return out_w, out_h
