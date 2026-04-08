"""
FFmpeg codec selection and command building.

Only uses hardware-accelerated H.264 encoders (h264_videotoolbox on macOS,
h264_mf on Windows, h264_vaapi on Linux) so that re-encoded recordings are
covered by the platform vendor's MPEG-LA patent license. Never uses libx264,
which is GPL-licensed and would taint the app bundle, and would also create
MPEG-LA royalty exposure in a commercial distribution.

Stream-copy is the default (recording_fps == "original") and has zero H.264
liability since no new bitstream is created — we just remux the camera's
already-encoded frames into an MP4 container.

Hardware DECODE is separate from hardware encode and always valuable because
the motion-detection and preview branches of the unified pipeline both apply
CPU-side filters (scene change detection, scale) which require decoded
frames even when the recording branch stream-copies. select_decoder() picks
a per-platform -hwaccel method; the escape hatch SIMPLENVR_NO_HWACCEL=1
disables it for broken drivers.

If no hardware encoder is available (e.g., headless Linux without a GPU),
select_encoder() returns None and build_record_cmd() silently falls back
to stream-copy regardless of the recording_fps setting.
"""

from __future__ import annotations

import logging
import os
import platform
from pathlib import Path

from ..ffmpeg_path import get_ffmpeg

logger = logging.getLogger(__name__)

# Log the decoder choice exactly once per process (the function is called
# on every build_unified_cmd invocation, which is every recorder spawn).
_decoder_logged = False


def select_encoder() -> tuple[str, list[str]] | None:
    """
    Return (encoder_name, encoder_flags) for a hardware-accelerated H.264
    encoder available on the current platform, or None if none is available.

    We only use hardware encoders because:
      1. libx264 is GPL — cannot be shipped in the bundled app
      2. Hardware encoders are covered by the platform vendor's MPEG-LA
         patent license (Apple for videotoolbox, Microsoft for Media
         Foundation, Intel/AMD for VAAPI)

    Returning None is acceptable: build_record_cmd() falls back to stream-copy
    when there is no encoder, which is also the default recording mode.
    """
    system = platform.system()

    if system == "Darwin":
        # Apple's hardware H.264 encoder — available on all Macs since 2011
        return ("h264_videotoolbox", ["-b:v", "1500k", "-realtime", "1"])

    if system == "Windows":
        # Windows Media Foundation H.264 encoder — available on Windows 8+
        return ("h264_mf", ["-b:v", "1500k"])

    if system == "Linux":
        # VAAPI works on Intel integrated GPUs and AMD GPUs with open drivers.
        # On headless Linux with no GPU, FFmpeg will fail at runtime and the
        # CameraRecorder restart loop will surface the error — users in that
        # situation can only use stream-copy mode.
        return ("h264_vaapi", ["-b:v", "1500k"])

    # Unknown platform — no safe encoder, force stream-copy
    return None


def select_decoder() -> list[str]:
    """
    Return FFmpeg input-side flags for hardware-accelerated H.264 decode,
    or an empty list (software decode) if hwaccel is disabled.

    **Hardware decode is OPT-IN via SIMPLENVR_HWACCEL=1**, not default-on.
    This is deliberate:

    - Recording itself is stream-copy by default; the decoder only feeds
      the motion and preview branches. Software decode of one 1080p stream
      is trivially cheap on any modern CPU, so the default cost is low.

    - Surveillance camera streams routinely send SPS/PPS parameter changes
      mid-stream, which trips hardware decoders harder than software ones.
      Verified live: VideoToolbox gets stuck in a reconfig loop on a
      Reolink 2560x1920 H.264 stream and outputs zero decoded frames.
      Software decode handles the same stream cleanly. Enabling hwaccel
      globally would silently break motion detection and preview on these
      cameras.

    - The user's primary recording cost is stream-copy (no decode), not
      real-time transcoding. Hardware decode mainly helps when:
        a) Recording with a non-"original" fps setting (re-encode path)
        b) Running close to the CPU ceiling from the motion/preview branches

    When SIMPLENVR_HWACCEL=1 is set, platform selection is:

    - macOS: videotoolbox. Caveat above — test each camera model.

    - Windows: d3d11va. Works on any D3D11-capable GPU (Intel/AMD/NVIDIA
      + Qualcomm Adreno). On Snapdragon X Elite Copilot+ PCs this routes
      through Qualcomm's Media Foundation component. **UNTESTED on
      Snapdragon hardware as of this commit.**

    - Linux: vaapi. Intel iGPUs and open-driver AMD.
    """
    global _decoder_logged

    if os.environ.get("SIMPLENVR_HWACCEL") != "1":
        if not _decoder_logged:
            logger.info("Hardware decode: disabled (set SIMPLENVR_HWACCEL=1 to enable)")
            _decoder_logged = True
        return []

    system = platform.system()

    if system == "Darwin":
        flags = ["-hwaccel", "videotoolbox"]
        name = "videotoolbox (macOS)"
    elif system == "Windows":
        flags = ["-hwaccel", "d3d11va"]
        if platform.machine().lower() in ("arm64", "aarch64"):
            name = "d3d11va (Windows ARM64 — UNTESTED on Snapdragon)"
        else:
            name = "d3d11va (Windows)"
    elif system == "Linux":
        flags = ["-hwaccel", "vaapi"]
        name = "vaapi (Linux)"
    else:
        flags = []
        name = "unavailable (unknown platform)"

    if not _decoder_logged:
        logger.info("Hardware decode: %s", name)
        _decoder_logged = True
    return flags


from ..config import MOTION_SCENE_THRESHOLD


def build_unified_cmd(
    rtsp_uri: str,
    output_pattern: Path,
    segment_secs: int,
    fps_setting: str,
    encoder: str | None,
    encoder_flags: list[str] | None,
    preview_tcp_url: str,
    preview_width: int = 640,
    preview_fps: int = 10,
    motion_width: int = 320,
) -> list[str]:
    """
    Build the unified FFmpeg command that opens a single RTSP connection
    and produces THREE outputs from it:

      1. Segmented MP4 recording (the original recorder output, stream-copy
         by default) — written to disk via the segment muxer.
      2. Scene-filtered MJPEG motion frames — emitted to stdout (pipe:1)
         only when the inter-frame scene change exceeds MOTION_SCENE_THRESHOLD.
      3. 10fps MJPEG preview stream — connected to a Python-side TCP listener
         on `preview_tcp_url`. Wrapped in the `-f fifo` muxer with
         drop_pkts_on_overflow so a stalled browser cannot back-pressure
         the camera's only RTSP connection.

    fps_setting (recording branch):
      - "original" → -c copy (stream-copy; default; zero CPU, no patent risk)
      - "10", "5", "2", "1" → re-encode at that framerate via hardware encoder
      - "0.5" → re-encode at 1 frame every 2 seconds

    If `encoder` is None (no hardware encoder available on this platform),
    the recording branch silently falls back to stream-copy regardless of
    fps_setting.
    """
    # RTSP-specific input hardening:
    #   -timeout 10000000: 10s socket timeout so dead RTSP connections fail
    #     fast instead of hanging FFmpeg indefinitely (making restart useless)
    #   -use_wallclock_as_timestamps 1: stamp frames with wall-clock time
    #     instead of trusting camera PTS, which on some cameras drifts/rolls
    #     and breaks segment duration calculations
    cmd: list[str] = [get_ffmpeg(), "-rtsp_transport", "tcp"]
    if rtsp_uri.lower().startswith("rtsp://"):
        cmd += [
            "-timeout", "10000000",
            "-use_wallclock_as_timestamps", "1",
        ]
    # Hardware decode flags MUST come before -i or ffmpeg ignores them.
    # Empty list when hwaccel is unavailable / disabled.
    cmd += select_decoder()
    cmd += ["-i", rtsp_uri]

    # ---------- Output 1: segmented recording ----------
    if fps_setting == "original" or encoder is None:
        # Pure stream copy — no re-encode, zero CPU, no H.264 patent liability.
        rec_codec = ["-map", "0:v", "-an", "-c", "copy"]
    else:
        # Re-encode at the chosen framerate via the platform hardware encoder
        if fps_setting == "0.5":
            fps_filter = "fps=1/2"
        else:
            fps_filter = f"fps={fps_setting}"
        rec_codec = [
            "-map", "0:v", "-an",
            "-vf", fps_filter,
            "-c:v", encoder,
            *(encoder_flags or []),
        ]

    rec_args = rec_codec + [
        "-f", "segment",
        "-segment_time", str(segment_secs),
        "-segment_format", "mp4",
        # +faststart relocates the moov atom to the start of the file
        # when the segment finalizes, so HTML5 video can seek to any
        # point in a closed segment instantly. The previous flags
        # (+frag_keyframe+empty_moov+default_base_moof) produced a
        # live-streamable fragmented MP4 whose seek index lived inside
        # the per-fragment moof boxes — browsers had to scan the whole
        # file to seek to a target time, making clip loading slow
        # and unpredictable. Tradeoff: the currently-in-progress
        # segment file is NOT playable from disk until it closes (max
        # ~segment_duration seconds, currently 1 minute). That's
        # acceptable because (a) live preview comes from the
        # FrameBroadcaster MJPEG stream, not the recording file, and
        # (b) the Inbox's gap-fallback already handles "event just
        # happened, no playable segment yet" gracefully.
        "-segment_format_options",
        "movflags=+faststart",
        "-reset_timestamps", "1",
        "-strftime", "1",
        str(output_pattern),
    ]

    # ---------- Output 2: scene-filtered motion frames (stdout pipe) ----------
    # The select filter only emits frames where the inter-frame scene
    # difference exceeds the threshold (matches the legacy MotionDetector
    # ffmpeg pattern). vsync vfr is required so dropped frames are actually
    # dropped from the output rather than duplicated.
    motion_args = [
        "-map", "0:v", "-an",
        "-vf",
        f"select='gt(scene,{MOTION_SCENE_THRESHOLD})',scale={motion_width}:-2",
        "-vsync", "vfr",
        "-f", "image2pipe",
        "-vcodec", "mjpeg",
        "-q:v", "5",
        "pipe:1",
    ]

    # ---------- Output 3: 10fps MJPEG preview (TCP fan-out) ----------
    # Wrapped in the fifo muxer so a stalled browser cannot back-pressure
    # the shared input read loop — drop_pkts_on_overflow=1 silently
    # discards frames the consumer can't keep up with, attempt_recovery=1
    # reconnects after transient errors.
    preview_args = [
        "-map", "0:v", "-an",
        "-vf", f"fps={preview_fps},scale={preview_width}:-2",
        "-c:v", "mjpeg",
        "-q:v", "5",
        "-f", "fifo",
        "-fifo_format", "mjpeg",
        "-drop_pkts_on_overflow", "1",
        "-attempt_recovery", "1",
        "-recovery_wait_time", "1",
        # queue_size in packets — ~5 seconds of frames at 10fps
        "-queue_size", "60",
        preview_tcp_url,
    ]

    # ---------- Global logging / progress ----------
    # verbose level needed to detect "Opening '...' for writing" segment lines
    # -progress pipe:2 keeps the staleness watchdog fed even when the verbose
    # log buffer is fully-buffered by libc.
    log_args = [
        "-loglevel", "verbose",
        "-progress", "pipe:2",
        "-stats_period", "5",
    ]

    return cmd + log_args + rec_args + motion_args + preview_args


# Backward-compat shim — kept only because main.spec / future tests might
# import the old name. The new code path uses build_unified_cmd directly.
def build_record_cmd(*args, **kwargs):  # pragma: no cover
    raise RuntimeError(
        "build_record_cmd is obsolete; use build_unified_cmd which produces "
        "the unified single-RTSP-connection ffmpeg command."
    )
