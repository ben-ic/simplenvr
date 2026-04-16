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
import hashlib
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
from .audio_broadcaster import AudioBroadcaster
from .codec import build_unified_cmd
from .frame_broadcaster import FrameBroadcaster

# Frame-staleness watchdog: kill ffmpeg when the on-disk segment file has
# not grown for this many seconds. File-growth is the sole liveness signal.
# stderr is kept only for diagnostics — the rolling tail buffer for
# post-mortem on unexpected exit, and the segment-open regex parsing for
# new-segment dispatch.
#
# This catches the Zone B pathology: ffmpeg keeps running and emitting
# stderr warnings ("Non-monotonic DTS", "RTP: missed N packets") while
# its muxer produces zero bytes. Observed on Eufy cameras with
# fragmented-MP4 muxer state corruption. The previous stderr-OR
# heartbeat kept the watchdog pacified because ffmpeg's stderr chatter
# never stops, even when the output file has stalled.
#
# The clock does NOT start at spawn — it starts on the first observed
# segment file growth. This excludes RTSP setup time (which can take
# 10-15s on slow cameras). A camera that never produces any bytes is
# caught by ffmpeg's own `-timeout` socket-I/O timeout at the demuxer
# layer (see backend/recording/codec.py), not by this watchdog.
STALE_FRAME_THRESHOLD_S = 30.0
STALE_CHECK_INTERVAL_S = 5.0

# Health state thresholds. Used to *label* the camera's current state
# for the live tile UI, distinct from (though the offline threshold
# coincides with) the STALE_FRAME_THRESHOLD_S that triggers a kill +
# restart. A camera stays "ok" while bytes are flowing within the last
# HEALTH_STALLED_THRESHOLD_S seconds, slides into "stalled" for a brief
# hiccup window, and reaches "offline" at HEALTH_OFFLINE_THRESHOLD_S —
# at which point the watchdog also terminates ffmpeg so the supervise
# loop can respawn. The process_monitor's offline-bridge keeps the UI
# pinned to "offline" across the respawn gap.
HEALTH_STALLED_THRESHOLD_S = 10.0
HEALTH_OFFLINE_THRESHOLD_S = 30.0

# Cross-pipeline split-brain watchdog. The file-growth watchdog above
# catches every genuine recorder stall eventually; this gate gets ahead
# of it by 15s when the detect-ffmpeg sibling is still decoding frames
# from the same go2rtc source — a signal that the camera and go2rtc are
# healthy and only the record-ffmpeg muxer is stuck. No surveyed
# open-source NVR does this because no surveyed NVR has our
# go2rtc-loopback architecture where both ffmpegs consume the same
# producer. See recording-reliability-plan.md §Architecture.
#
#   STARTUP_GRACE_S:             skip the check during recorder cold
#     start — detect-ffmpeg needs 2-3s to produce its first frame, and
#     the recorder needs to land the first segment fragment. Firing in
#     the overlap window would false-positive on every spawn.
#   SPLIT_BRAIN_BYTES_THRESHOLD_S: record-ffmpeg bytes-stale threshold.
#     At 15s we're past realistic camera jitter but well inside the 30s
#     kill threshold of the file-growth watchdog.
#   SPLIT_BRAIN_DETECT_FRESH_S:   detect-ffmpeg decode-time freshness
#     gate. 10s = ~20 frames at the 2 fps detect cadence, generous
#     margin for single dropped frames without falsely declaring a
#     split brain.
STARTUP_GRACE_S = 15.0
SPLIT_BRAIN_BYTES_THRESHOLD_S = 15.0
SPLIT_BRAIN_DETECT_FRESH_S = 10.0

# Chronic-failure circuit breaker. When watchdog-initiated restarts pile
# up faster than the camera can recover on its own, the recorder flips to
# the low-bitrate sub-stream so at least SOMETHING is captured while the
# user investigates. Counts BOTH split-brain kills (detect fresh, record
# stalled) and plain-stall kills (both stalled — typical of a flaky
# upstream starving everything downstream of go2rtc) — both point to the
# same remedy. Per-CameraRecorder state (not manager-level) so a removed
# camera frees its deque automatically — no cleanup code in the manager.
# A sliding-window counter is the simplest shape that catches "3 failures
# in 10 minutes" without false-tripping on a camera that fails once a day.
CIRCUIT_BREAKER_WINDOW_S = 600.0  # 10 minutes
CIRCUIT_BREAKER_THRESHOLD = 3     # watchdog-initiated restarts to trip

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

# Safety cap on the MJPEG demux buffer. A malformed frame (SOI without
# a matching EOI) or a corrupted stream preamble would otherwise grow
# `buffer` unbounded as 8KB chunks accumulate forever. 512KB is ~10×
# a typical scene-filtered 320px JPEG, so it never interferes with
# normal operation, but caps the worst case at half a megabyte per
# camera. On overflow we drop the buffer and resync at the next SOI.
_MAX_MJPEG_BUFFER_SIZE = 512 * 1024

# How long we wait after spawning the audio ffmpeg before deciding
# whether the camera actually has an audio track. If the process is
# still alive when this elapses, it's decoding audio; if it has already
# exited, there's no audio track (or the probe failed). 2 seconds is
# enough for ffmpeg's RTSP probe + codec negotiation on slow cameras.
_AUDIO_PROBE_WINDOW_S = 2.0

if TYPE_CHECKING:
    import aiosqlite

    from ..api.ws import EventBus
    from ..models import Camera, Settings
    from ..motion.manager import MotionManager

logger = logging.getLogger(__name__)

# Matches: [segment @ 0x...] Opening '/path/to/file.mp4' for writing
SEGMENT_OPEN_RE = re.compile(r"Opening '([^']+\.mp4)' for writing")


# ---------------------------------------------------------------------------
# Watchdog pure-logic predicates
# ---------------------------------------------------------------------------
# The two functions below encode the decision boundaries of the Zone-B
# split-brain rule and the chronic-failure circuit breaker. They are
# deliberately pure (no I/O, no subprocess, no asyncio) so they can be
# exercised in unit tests without spinning up a real CameraRecorder. The
# watchdog and the _register_recording_failure_restart method call them as
# black-box predicates — the thresholds live here, the side effects
# (logger.error, terminate_process_group, asyncio.create_task) stay at
# the call sites. Changing a threshold is a one-line edit; changing the
# rule shape is one edit here plus test updates.


def should_trip_split_brain(
    *,
    bytes_elapsed_s: float,
    recorder_uptime_s: float,
    detect_elapsed_s: float | None,
) -> bool:
    """Decide whether the cross-pipeline split-brain rule should fire.

    The rule trips when the record-ffmpeg has not grown its segment file
    for longer than SPLIT_BRAIN_BYTES_THRESHOLD_S *and* the detect-ffmpeg
    sibling has decoded a frame within SPLIT_BRAIN_DETECT_FRESH_S. That
    combination is unambiguous Zone-B: go2rtc is still producing packets
    (proven by detect-ffmpeg), only the recorder's muxer is stuck.

    Args:
        bytes_elapsed_s: Seconds since the record-segment file last grew.
            Caller must have already confirmed at least one byte was
            written (the watchdog's ``_last_progress_ts == 0.0`` guard);
            this predicate does not second-guess that.
        recorder_uptime_s: Seconds since the current ffmpeg process was
            spawned. Under STARTUP_GRACE_S the predicate is forced false
            so concurrent recorder+detect spawn doesn't false-positive
            during the ~2-3s detect-ffmpeg cold start.
        detect_elapsed_s: Seconds since detect-ffmpeg last decoded a
            frame for this camera, or ``None`` if detection is
            unavailable (classifier off, no detector attached for this
            camera, or no frames decoded yet). ``None`` degrades
            gracefully: the rule never trips, and the watchdog falls
            through to the file-growth kill threshold at 30s.
    """
    if recorder_uptime_s < STARTUP_GRACE_S:
        return False
    if detect_elapsed_s is None:
        return False
    return (
        bytes_elapsed_s > SPLIT_BRAIN_BYTES_THRESHOLD_S
        and detect_elapsed_s < SPLIT_BRAIN_DETECT_FRESH_S
    )


def prune_and_check_breaker_trip(
    restarts: "deque[float]",
    *,
    now: float,
    window_s: float,
    threshold: int,
) -> bool:
    """Append ``now`` to ``restarts``, prune entries older than
    ``window_s``, and return True iff the deque now holds ``threshold``
    or more entries.

    Mutates ``restarts`` in place. The caller owns the deque and is
    responsible for any post-trip state reset (e.g. ``.clear()`` if the
    breaker should require a full N fresh failures before re-tripping).
    """
    restarts.append(now)
    cutoff = now - window_s
    while restarts and restarts[0] < cutoff:
        restarts.popleft()
    return len(restarts) >= threshold


# Chain-of-custody: SHA-256 streaming chunk size. 64KB is small enough
# that a single chunk read is a comfortably-sized syscall on every
# platform we target, and large enough that the Python/bytes overhead
# per chunk is negligible compared to hashlib's C implementation. At
# this size a 256 MB segment resolves in ~4000 chunk iterations, well
# under a second on any disk that can keep up with our recording.
_SHA256_CHUNK_SIZE = 64 * 1024


def _hash_segment_file_sync(path: Path) -> str:
    """Stream-hash a segment file with SHA-256 and return the hex digest.

    Synchronous — intended to be invoked via ``asyncio.to_thread`` so
    hashing of hundred-MB segment files never stalls the event loop.
    Raises any ``OSError`` from ``open()``/``read()``; the caller is
    expected to trap it and fall back to ``sha256=None`` rather than
    dropping the recording row (the row is more important than the
    hash — a NULL digest is recoverable, a missing row is not).
    """
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_SHA256_CHUNK_SIZE)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


async def _hash_segment_file(path: Path) -> str:
    """Async wrapper around ``_hash_segment_file_sync`` using to_thread."""
    return await asyncio.to_thread(_hash_segment_file_sync, path)


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
        self._audio_proc: asyncio.subprocess.Process | None = None
        self._watcher_task: asyncio.Task | None = None
        self._monitor_task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._motion_reader_task: asyncio.Task | None = None
        self._audio_reader_task: asyncio.Task | None = None

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
        # Audio broadcaster — created when the audio ffmpeg starts
        # producing data. None if the camera has no audio track.
        self.audio_broadcaster: AudioBroadcaster | None = None

        # Cross-pipeline split-brain witness. Wired by
        # RecordingManager.attach_motion_manager after MotionManager is
        # constructed (startup-order gotcha: RecordingManager is built
        # before MotionManager so constructor injection isn't possible).
        # None disables the split-brain check cleanly.
        self._motion_manager: "MotionManager | None" = None
        # Monotonic timestamp of the most recent successful spawn. Gates
        # the startup grace period for the split-brain check — 2-3s of
        # concurrent recorder/detect cold-start should not be flagged.
        self._recorder_started_at: float = 0.0
        # Sliding-window record of watchdog-triggered restarts (both
        # split-brain kills and plain-stall kills). Each entry is a
        # monotonic timestamp; see _register_recording_failure_restart
        # for the pruning + trip logic.
        self._recording_failure_restarts: deque[float] = deque()

    @property
    def is_running(self) -> bool:
        return self._running

    def attach_motion_manager(self, motion_manager: "MotionManager") -> None:
        """Wire the cross-pipeline witness. Called by
        RecordingManager.attach_motion_manager after MotionManager is
        constructed in main.py's background-startup phase. Safe to call
        multiple times; a None motion_manager disables the split-brain
        check in _staleness_watchdog."""
        self._motion_manager = motion_manager

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
        #
        # Dual-stream registration: when the camera exposes a sub-stream
        # we always register BOTH with go2rtc — main as `{camera_id}`
        # and sub as `{camera_id}_sub` — regardless of the user's
        # `record_substream_when_available` setting. Registration itself
        # costs nothing (go2rtc is a lazy producer; the upstream RTSP
        # session only opens when a consumer actually attaches), and
        # always-on registration lets the frontend render both streams
        # side-by-side in the onboarding compare UI without tearing
        # down and rebuilding the go2rtc config mid-flow.
        #
        # The setting only decides which stream the recorder *reads*
        # for writing to disk. Cameras without a sub-stream fall
        # through to main unconditionally — toggling is always safe.
        #
        # The stored *_uri columns are credential-free (see
        # backend/rtsp_url.py). Rebuild the authenticated upstream URL
        # here — it's the single string go2rtc needs to actually open a
        # connection to the camera.
        from ..rtsp_url import authed_substream_uri, authed_uri

        upstream_main = authed_uri(self.camera)
        upstream_sub = authed_substream_uri(self.camera)

        # Default input is the direct main-stream URL. Each successful
        # go2rtc registration may rewrite `input_uri` to its loopback.
        input_uri = upstream_main
        main_loopback = None
        sub_loopback = None

        if go2rtc_client.is_enabled() and upstream_main:
            ok_main = await go2rtc_client.add_stream(
                self.camera.id, upstream_main
            )
            if ok_main:
                main_loopback = go2rtc_client.loopback_url_for(self.camera.id)
            else:
                # Dual-client hazard: recorder now holds a direct RTSP
                # session to the camera while the frontend's WebRTC
                # consumer is still attached to go2rtc's (possibly
                # half-registered) stream. On cameras that allow only one
                # mainstream client (Tapo, some Dahuas) this causes the
                # upstream to EOF one of the two sessions on a loop.
                # Escalated to ERROR with a distinctive marker so tailing
                # logs during a stuck-on-Connecting session can
                # immediately confirm or rule out this path.
                logger.error(
                    "[RECORDER_DIRECT_FALLBACK] go2rtc registration "
                    "failed for camera id=%s ip=%s (main); recorder is "
                    "opening rtsp directly. If a WebRTC consumer is "
                    "also attached, this camera now has two concurrent "
                    "RTSP sessions and single-client devices will loop EOF.",
                    self.camera.id,
                    self.camera.ip,
                )

            if upstream_sub:
                sub_name = f"{self.camera.id}_sub"
                ok_sub = await go2rtc_client.add_stream(sub_name, upstream_sub)
                if ok_sub:
                    sub_loopback = go2rtc_client.loopback_url_for(sub_name)
                else:
                    # Sub-stream registration failure is non-fatal: the
                    # main-stream path still works, the setting silently
                    # falls back to main for this camera, and the
                    # onboarding compare UI will see "no sub available".
                    logger.info(
                        "go2rtc sub-stream registration failed for %s; "
                        "recorder will use main stream for this camera",
                        self.camera.ip,
                    )

        # Pick the input URL for ffmpeg based on the setting AND whether
        # a sub-stream is actually available for this camera. The
        # fallback order is: sub loopback -> main loopback -> direct main.
        #
        # Per-camera override takes precedence over the global setting:
        # the chronic-failure circuit breaker writes
        # recording_stream_override='sub' after repeated split-brain
        # restarts, and a future user-picker may write 'main'. NULL
        # means "no preference — follow the setting."
        override = self.camera.recording_stream_override
        if override == "sub":
            use_sub = upstream_sub is not None
            if not use_sub:
                logger.warning(
                    "%s: recording_stream_override='sub' but no sub-stream "
                    "available; recording from main",
                    self.camera.ip,
                )
        elif override == "main":
            use_sub = False
        else:
            use_sub = (
                self._settings.record_substream_when_available
                and upstream_sub is not None
            )
        if use_sub and sub_loopback:
            input_uri = sub_loopback
        elif use_sub and not sub_loopback:
            # Sub requested but go2rtc registration of the sub failed.
            # Fall through to main (loopback if available, direct otherwise).
            input_uri = main_loopback or upstream_main
            logger.info(
                "Recording %s from main stream (sub requested but "
                "unavailable)",
                self.camera.ip,
            )
        else:
            input_uri = main_loopback or upstream_main

        # Keep the existing `loopback_uri` variable name for any
        # downstream code that reads it from logs.
        loopback_uri = sub_loopback if use_sub else main_loopback

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
            "Starting unified pipeline: %s (%s) via=%s stream=%s tether=%s",
            self.camera.ip,
            self.camera.id,
            "go2rtc" if loopback_uri else "direct",
            "sub" if use_sub else "main",
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

        # Watchdog clock stays at 0.0 (dormant) until the first observed
        # segment file growth. See the module docstring for STALE_FRAME_THRESHOLD_S.
        self._last_progress_ts = 0.0
        self._last_segment_size = 0
        # Start the split-brain startup-grace clock. The grace window
        # prevents false positives during the 2-3s cold start where
        # detect-ffmpeg is already producing frames but the recorder's
        # first segment hasn't landed any bytes yet.
        self._recorder_started_at = time.monotonic()
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

        # Try to spawn a lightweight audio-only ffmpeg for YAMNet.
        # Reads from the same go2rtc loopback (no extra camera connection),
        # extracts only the audio track as PCM s16le 16kHz mono to stdout.
        # If the camera has no audio track, ffmpeg exits immediately and
        # we skip audio classification for this camera.
        asyncio.create_task(self._try_spawn_audio(input_uri, tether_bin))

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

        # Kill audio ffmpeg if running
        if self._audio_proc and self._audio_proc.returncode is None:
            terminate_process_group(self._audio_proc, signal.SIGTERM)
            try:
                await asyncio.wait_for(self._audio_proc.wait(), timeout=5.0)
            except (asyncio.TimeoutError, Exception):
                terminate_process_group(self._audio_proc, signal.SIGKILL)

        for task in (
            self._watcher_task,
            self._monitor_task,
            self._watchdog_task,
            self._motion_reader_task,
            self._audio_reader_task,
        ):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        # Unblock the motion detector if it's currently subscribed
        # to our motion broadcaster.
        self.motion_broadcaster.close()
        if self.audio_broadcaster:
            self.audio_broadcaster.close()
            self.audio_broadcaster = None

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
    # FFmpeg stderr watcher (segment detection + diagnostic tail only)
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

                # Sole liveness signal: segment file size growth. stderr
                # chatter was previously treated as a heartbeat but that
                # allowed the Zone B split-brain pathology — ffmpeg alive
                # and talking but muxer writing zero bytes. The on-disk
                # segment file growing is an unambiguous "ffmpeg is
                # actually muxing packets" signal.
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

                # Cross-pipeline split-brain gate. Catches Zone B 15s
                # earlier than the file-growth kill threshold by using
                # detect-ffmpeg as a witness that go2rtc is still
                # producing packets. The decision is delegated to the
                # pure `should_trip_split_brain` predicate; this block
                # only collects its inputs and owns the side effects.
                detect_dt = (
                    self._motion_manager.get_last_decoded_frame_dt(self.camera.id)
                    if self._motion_manager is not None
                    else None
                )
                detect_elapsed_s: float | None = None
                if detect_dt is not None:
                    detect_elapsed_s = (
                        datetime.now(timezone.utc) - detect_dt
                    ).total_seconds()

                if should_trip_split_brain(
                    bytes_elapsed_s=elapsed,
                    recorder_uptime_s=time.monotonic() - self._recorder_started_at,
                    detect_elapsed_s=detect_elapsed_s,
                ):
                    logger.error(
                        "split-brain on %s: detect fresh (%.1fs) but "
                        "recorder bytes stalled (%.1fs); restarting "
                        "recorder",
                        self.camera.ip,
                        detect_elapsed_s,
                        elapsed,
                    )
                    self._register_recording_failure_restart()
                    terminate_process_group(self._proc, signal.SIGTERM)
                    return

                if elapsed > STALE_FRAME_THRESHOLD_S:
                    logger.warning(
                        "FFmpeg for %s appears stalled (%.1fs no progress), "
                        "terminating to trigger restart",
                        self.camera.ip,
                        elapsed,
                    )
                    # Count this toward the breaker too — a plain stall with
                    # detect-ffmpeg also starved (or absent) is a different
                    # failure shape than Zone-B split-brain, but the user-
                    # facing remedy is the same: after three of these inside
                    # the window, flip to sub-stream and surface chronic
                    # state. Without this call, a camera whose whole go2rtc-
                    # loopback pipeline keeps stalling together (flapping
                    # upstream, not just a stuck recorder muxer) loops
                    # forever between "offline" and fresh-respawn with no UI
                    # escalation. Observed on a Tapo at 10.0.0.63.
                    self._register_recording_failure_restart()
                    terminate_process_group(self._proc, signal.SIGTERM)
                    return
        except asyncio.CancelledError:
            raise

    def _register_recording_failure_restart(self) -> None:
        """Record one watchdog-initiated restart — either a split-brain
        kill (detect fresh, record stalled) or a plain-stall kill (both
        stalled for STALE_FRAME_THRESHOLD_S). On the Nth restart within
        the sliding window, schedule the chronic-failure handler
        (sub-stream fallback) and clear the deque so we don't re-trip on
        every subsequent failure until the next N accumulate.

        Both failure shapes collapse to the same user-facing remedy:
        drop to the lower-bitrate sub-stream. Split-brain proves go2rtc
        is producing packets but only the recorder's muxer is stuck;
        plain-stall means bytes-were-flowing-then-stopped for longer
        than the watchdog's kill threshold, which after repeated
        restarts is just as indicative of a flaky main stream that the
        sub may ride out. Conflating them here keeps the breaker honest
        about "this camera can't stay recorded on main."
        """
        tripped = prune_and_check_breaker_trip(
            self._recording_failure_restarts,
            now=time.monotonic(),
            window_s=CIRCUIT_BREAKER_WINDOW_S,
            threshold=CIRCUIT_BREAKER_THRESHOLD,
        )
        if tripped:
            asyncio.create_task(self._handle_chronic_failure())
            self._recording_failure_restarts.clear()

    async def _handle_chronic_failure(self) -> None:
        """Circuit breaker tripped: CIRCUIT_BREAKER_THRESHOLD
        watchdog-initiated restarts in CIRCUIT_BREAKER_WINDOW_S. Emit a
        chronic-failure health event, flip the camera to its sub-stream
        in the DB (unless the user has explicitly picked main), refresh
        self.camera so the next spawn sees the override, and ask the
        current ffmpeg to exit. The _process_monitor respawn loop then
        brings the sub-stream up."""
        reason = (
            f"{CIRCUIT_BREAKER_THRESHOLD}+ recording restarts in "
            f"{int(CIRCUIT_BREAKER_WINDOW_S / 60)}min"
        )
        try:
            await self._event_bus.emit(
                "camera_health",
                {
                    "camera_id": self.camera.id,
                    "health": "chronic_recording_failure",
                    "reason": reason,
                },
            )
        except Exception as e:
            logger.warning(
                "chronic_recording_failure emit failed for %s: %s",
                self.camera.ip, e,
            )

        # WHERE guard inside set_stream_override_if_not_set prevents
        # clobbering a user-picked 'main'; that's a deliberate product
        # decision — the breaker should not override explicit user
        # intent. If we're blocked by that guard, the user gets repeated
        # chronic_recording_failure emits but no auto-fallback.
        updated = await db.set_stream_override_if_not_set(
            self._conn,
            camera_id=self.camera.id,
            override="sub",
            reason=reason,
        )
        if updated:
            fresh = await db.get_camera(self._conn, self.camera.id)
            if fresh is not None:
                self.camera = fresh
            logger.warning(
                "%s: auto-fallback to sub-stream after %s",
                self.camera.ip, reason,
            )
        else:
            logger.warning(
                "%s: circuit breaker blocked by user 'main' selection; "
                "keeping main stream despite %s",
                self.camera.ip, reason,
            )

        terminate_process_group(self._proc, signal.SIGTERM)

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
                # Safety cap: SOI found but no matching EOI across many
                # chunks means either a corrupted stream or a camera
                # emitting an unexpectedly huge frame. Drop and resync
                # rather than growing the buffer without bound.
                if len(buffer) > _MAX_MJPEG_BUFFER_SIZE:
                    logger.warning(
                        "MJPEG buffer exceeded %d bytes for %s; dropping "
                        "and resyncing at next SOI",
                        _MAX_MJPEG_BUFFER_SIZE,
                        self.camera.ip,
                    )
                    buffer.clear()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Motion pipe reader error for %s: %s", self.camera.ip, e)

    # ------------------------------------------------------------------
    # Audio pipeline (separate lightweight ffmpeg for YAMNet)
    # ------------------------------------------------------------------
    async def _try_spawn_audio(self, input_uri: str, tether_bin: str | None) -> None:
        """Spawn a second ffmpeg that extracts only the audio track as
        PCM s16le 16kHz mono to stdout. If the camera has no audio track,
        ffmpeg exits immediately and we silently skip audio classification."""
        from ..ffmpeg_path import get_ffmpeg

        cmd: list[str] = [
            get_ffmpeg(),
            "-rtsp_transport", "tcp",
            "-i", input_uri,
            "-map", "0:a",
            "-f", "s16le",
            "-acodec", "pcm_s16le",
            "-ar", "16000",
            "-ac", "1",
            "-loglevel", "error",
            "pipe:1",
        ]

        if tether_bin and Path(tether_bin).exists():
            cmd = [tether_bin, *cmd]

        try:
            self._audio_proc = await spawn_proc(
                *cmd,
                stdin=PIPE,
                stdout=PIPE,
                stderr=PIPE,
                start_new_session=True,
            )
        except FileNotFoundError:
            return

        # Race ffmpeg's early exit against the probe window. If the
        # process exits first, there's no audio track (or probe failed);
        # if the timeout wins, the process is actively decoding.
        #
        # Previously this was `await asyncio.sleep(2.0)` followed by a
        # returncode check — brittle on slow RTSP cameras where probe
        # could complete either side of the 2s line and zombie-prone
        # because a process that exited just AFTER the check was never
        # awaited. Using asyncio.wait on an explicit wait_task lets us
        # both (a) return as soon as we know the answer and (b) keep a
        # reference to reap the process on the pipe reader's exit path.
        wait_task = asyncio.create_task(self._audio_proc.wait())
        done, _ = await asyncio.wait(
            {wait_task}, timeout=_AUDIO_PROBE_WINDOW_S
        )
        if wait_task in done:
            rc = self._audio_proc.returncode
            logger.debug(
                "No audio track for %s (ffmpeg exited rc=%s)",
                self.camera.ip, rc,
            )
            self._audio_proc = None
            return

        self.audio_broadcaster = AudioBroadcaster(
            name=f"audio:{self.camera.id}", max_queue=8
        )
        self._audio_reader_task = asyncio.create_task(self._audio_pipe_reader())
        logger.info("Audio pipeline started for %s", self.camera.ip)

        await self._event_bus.emit(
            "audio_available", {"camera_id": self.camera.id}
        )

    async def _audio_pipe_reader(self) -> None:
        """Drain audio ffmpeg stdout (raw PCM) and feed to audio broadcaster."""
        if self._audio_proc is None or self._audio_proc.stdout is None:
            return
        try:
            while True:
                chunk = await self._audio_proc.stdout.read(4096)
                if not chunk:
                    break
                if self.audio_broadcaster:
                    self.audio_broadcaster.feed(chunk)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Audio pipe reader error for %s: %s", self.camera.ip, e)
        finally:
            # Reap the audio ffmpeg so it can't become a zombie. If the
            # process is still running here (reader cancelled from stop(),
            # not EOF), kill it and wait — we hold the only reference to
            # this Process object, so if we don't wait() it, no one will.
            if self._audio_proc is not None:
                if self._audio_proc.returncode is None:
                    try:
                        self._audio_proc.kill()
                    except Exception:
                        pass
                try:
                    await asyncio.wait_for(self._audio_proc.wait(), timeout=2.0)
                except (asyncio.TimeoutError, Exception):
                    pass
            if self.audio_broadcaster:
                self.audio_broadcaster.close()
            try:
                await self._event_bus.emit(
                    "audio_stopped", {"camera_id": self.camera.id}
                )
            except Exception as e:
                # Event-bus failure must never mask an earlier error or
                # prevent the teardown from completing.
                logger.debug("audio_stopped emit failed for %s: %s", self.camera.ip, e)

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

            # Chain-of-custody: hash the closed segment so later reads can
            # detect tampering. Offloaded to a thread so a 256 MB segment
            # hash (~250ms on fast SSDs, longer on spinning disks) never
            # stalls the recorder's event loop — which would otherwise
            # delay the next SEGMENT_OPEN_RE dispatch and, in the worst
            # case, trip the staleness watchdog. On any IO failure we log
            # and fall back to NULL: the row is more important than the
            # hash, and a NULL digest is cleanly handled by the verify
            # endpoint and future trust-strip UI.
            sha256: str | None
            try:
                sha256 = await _hash_segment_file(path)
            except (OSError, ValueError) as e:
                logger.warning(
                    "sha256 failed for %s (%s); storing NULL digest", path, e
                )
                sha256 = None

            await db.complete_recording(
                self._conn,
                file_path=str(path),
                ended_at=ended_at.isoformat(),
                file_bytes=file_bytes,
                duration_s=duration_s,
                bitrate_bps=bitrate_bps,
                sha256=sha256,
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

            # Bridge the watchdog's blind spot: the staleness watchdog only
            # runs while `_proc is not None`, so the gap between an ffmpeg
            # exit and the next successful spawn is silent on the event bus.
            # Emit `offline` here so the UI tile flips its badge to OFFLINE
            # immediately rather than continuing to show stale RECORDING
            # state for the entire backoff. Only on a transition into
            # offline; the next _spawn() resets _last_emitted_health = None
            # (see the spawn block) so the watchdog can emit `ok` again on
            # the first packet after restart.
            if self._last_emitted_health != "offline":
                last_frame_at = datetime.now(timezone.utc)
                if self._last_progress_ts > 0.0:
                    from datetime import timedelta

                    elapsed = time.monotonic() - self._last_progress_ts
                    last_frame_at = last_frame_at - timedelta(seconds=elapsed)
                try:
                    await self._event_bus.emit(
                        "camera_health",
                        {
                            "camera_id": self.camera.id,
                            "health": "offline",
                            "last_frame_at": last_frame_at.isoformat(),
                        },
                    )
                    self._last_emitted_health = "offline"
                except Exception as e:
                    # Event bus failures must never take down the supervise
                    # loop — recording recovery is the priority. Mirror the
                    # watchdog's swallow-and-log pattern.
                    logger.warning(
                        "camera_health offline emit failed for %s: %s",
                        self.camera.ip,
                        e,
                    )

            await asyncio.sleep(delay)
            if self._running:
                await self._spawn()
