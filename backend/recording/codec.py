"""
FFmpeg codec selection and command building.

Picks hardware-accelerated h264_videotoolbox on macOS, falls back to libx264
elsewhere. Builds segment-based recording commands for both `original` (copy)
and downsampled (re-encode) modes.
"""

from __future__ import annotations

import logging
import platform
from pathlib import Path

logger = logging.getLogger(__name__)


def select_encoder() -> tuple[str, list[str]]:
    """
    Return (encoder_name, encoder_flags) suitable for the current platform.

    On macOS we use h264_videotoolbox (hardware accelerated, very low CPU).
    Elsewhere we use libx264 with ultrafast preset (still cheap).
    """
    if platform.system() == "Darwin":
        # Hardware-accelerated H.264 on macOS
        return ("h264_videotoolbox", ["-b:v", "1500k", "-realtime", "1"])
    return ("libx264", ["-preset", "ultrafast", "-crf", "28"])


def build_record_cmd(
    rtsp_uri: str,
    output_pattern: Path,
    segment_secs: int,
    fps_setting: str,
    encoder: str,
    encoder_flags: list[str],
) -> list[str]:
    """
    Build the FFmpeg command for recording a camera.

    fps_setting:
      - "original" → -c copy (no re-encode, zero CPU)
      - "10", "5", "2", "1" → re-encode at that framerate
      - "0.5" → re-encode at 1 frame every 2 seconds (uses fps=1/2 filter)
    """
    base = [
        "ffmpeg",
        "-rtsp_transport", "tcp",
        "-i", rtsp_uri,
    ]

    if fps_setting == "original":
        # Pure stream copy — no re-encoding, no quality loss, ~zero CPU
        codec_args = ["-c", "copy"]
    else:
        # Re-encode with downsampled framerate
        if fps_setting == "0.5":
            fps_filter = "fps=1/2"
        else:
            fps_filter = f"fps={fps_setting}"
        codec_args = [
            "-vf", fps_filter,
            "-c:v", encoder,
            *encoder_flags,
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
