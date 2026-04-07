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

If no hardware encoder is available (e.g., headless Linux without a GPU),
select_encoder() returns None and build_record_cmd() silently falls back
to stream-copy regardless of the recording_fps setting.
"""

from __future__ import annotations

import logging
import platform
from pathlib import Path

from ..ffmpeg_path import get_ffmpeg

logger = logging.getLogger(__name__)


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


def build_record_cmd(
    rtsp_uri: str,
    output_pattern: Path,
    segment_secs: int,
    fps_setting: str,
    encoder: str | None,
    encoder_flags: list[str] | None,
) -> list[str]:
    """
    Build the FFmpeg command for recording a camera.

    fps_setting:
      - "original" → -c copy (stream-copy; default; zero CPU, no patent risk)
      - "10", "5", "2", "1" → re-encode at that framerate via hardware encoder
      - "0.5" → re-encode at 1 frame every 2 seconds

    If `encoder` is None (no hardware encoder available on this platform),
    we silently fall back to stream-copy regardless of fps_setting.
    """
    # RTSP-specific input hardening:
    #   -timeout 10000000: 10s socket timeout so dead RTSP connections fail
    #     fast instead of hanging FFmpeg indefinitely (making restart useless)
    #   -use_wallclock_as_timestamps 1: stamp frames with wall-clock time
    #     instead of trusting camera PTS, which on some cameras drifts/rolls
    #     and breaks segment duration calculations
    base: list[str] = [get_ffmpeg(), "-rtsp_transport", "tcp"]
    if rtsp_uri.lower().startswith("rtsp://"):
        base += [
            "-timeout", "10000000",
            "-use_wallclock_as_timestamps", "1",
        ]
    base += ["-i", rtsp_uri]

    if fps_setting == "original" or encoder is None:
        # Pure stream copy — no re-encode, no quality loss, zero CPU, no
        # H.264 patent liability. Also the fallback when no hardware
        # encoder is available on this platform.
        codec_args = ["-c", "copy"]
    else:
        # Re-encode with downsampled framerate via hardware encoder
        if fps_setting == "0.5":
            fps_filter = "fps=1/2"
        else:
            fps_filter = f"fps={fps_setting}"
        codec_args = [
            "-vf", fps_filter,
            "-c:v", encoder,
            *(encoder_flags or []),
        ]

    segment_args = [
        "-an",  # No audio — security footage rarely needs it
        "-f", "segment",
        "-segment_time", str(segment_secs),
        "-segment_format", "mp4",
        # Fragmented MP4 so files are playable while being written
        # (otherwise the moov atom is only written when the segment closes)
        "-segment_format_options",
        "movflags=+frag_keyframe+empty_moov+default_base_moof",
        "-reset_timestamps", "1",
        "-strftime", "1",
        # verbose level needed to detect "Opening '...' for writing" lines
        "-loglevel", "verbose",
        str(output_pattern),
    ]

    return base + codec_args + segment_args
