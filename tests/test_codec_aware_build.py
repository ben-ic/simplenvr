"""Decision-boundary tests for the recorder's codec-aware ffmpeg builder.

`build_unified_cmd(source_codec=...)` branches between stream-copy and
a hardware-encoder transcode path. The load-bearing rule these tests
pin: **every recording we produce must be playable**, so MJPEG sources
must never reach the fragmented-MP4 stream-copy branch (which produces
black files — one fragment per frame). See
plans/substream-codec-aware-plan.md §Architecture.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.recording.codec import (
    UnsupportedSourceCodecError,
    build_unified_cmd,
)


OUTPUT_PATTERN = Path("/tmp/rec/%Y-%m-%d/%H-%M-%S.mp4")


def _build(**overrides):
    """Shorthand that fills in defaults matching a real recorder spawn."""
    kwargs = dict(
        rtsp_uri="rtsp://127.0.0.1:8554/cam",
        output_pattern=OUTPUT_PATTERN,
        segment_secs=60,
        fps_setting="original",
        encoder=None,
        encoder_flags=None,
        source_codec=None,
    )
    kwargs.update(overrides)
    return build_unified_cmd(**kwargs)


# ---------------------------------------------------------------------------
# Stream-copy path — unchanged behaviour for H.264 / H.265 / unknown.
# ---------------------------------------------------------------------------

def test_h264_source_stream_copies():
    """H.264 source stream-copies — zero re-encode CPU, no H.264 patent
    liability (the camera already paid the license)."""
    cmd = _build(source_codec="H264")
    assert "copy" in cmd, "H.264 source must use -c copy"
    assert "h264_videotoolbox" not in cmd
    assert "h264_mf" not in cmd
    assert "h264_vaapi" not in cmd
    # The transcode path's signature flags must not appear.
    assert "-b:v" not in cmd or "2000k" not in cmd


def test_h265_source_stream_copies():
    """H.265 source also stream-copies — we never re-encode HEVC since
    the H.264 encoders wouldn't do it anyway."""
    cmd = _build(source_codec="H265")
    assert "copy" in cmd


def test_unknown_source_codec_stream_copies():
    """source_codec=None is the pre-migration state (row predates the
    codec-awareness migration). Must fall through to stream-copy so
    existing installs keep working until the next re-interrogation
    fills in the codec column."""
    cmd = _build(source_codec=None)
    assert "copy" in cmd


def test_lowercase_codec_tag_is_accepted():
    """The selector normalizes codec tags to upper-case but nothing
    enforces it on the wire — defensive lower-case handling means a
    future caller that forgets to normalize doesn't silently corrupt
    recordings."""
    cmd = _build(source_codec="h264")
    assert "copy" in cmd


# ---------------------------------------------------------------------------
# MJPEG transcode path — the load-bearing correctness fix.
# ---------------------------------------------------------------------------

def test_mjpeg_source_with_encoder_transcodes_to_h264():
    """MJPEG source plus a hardware H.264 encoder transcodes at record
    time. Stream-copying MJPEG into a fragmented MP4 container produces
    one fragment per frame (every MJPEG frame is a keyframe) and every
    playback pipeline renders that as black — hence the transcode
    branch."""
    cmd = _build(
        source_codec="MJPEG",
        encoder="h264_videotoolbox",
        encoder_flags=["-realtime", "1"],
    )
    # The transcode line must reference the platform encoder and the
    # 2 Mbps fallback bitrate.
    assert "h264_videotoolbox" in cmd
    assert "-b:v" in cmd and "2000k" in cmd
    # -profile:v high is load-bearing for compatibility with browser
    # playback pipelines (Safari's native HLS rejects baseline at
    # 720p+ on some codec variants).
    assert "-profile:v" in cmd
    # Stream-copy must NOT appear — that would bypass the transcode.
    assert "-c copy" not in " ".join(cmd)
    assert ["-c:v", "copy"] != cmd[cmd.index("-c:v") : cmd.index("-c:v") + 2]
    # Encoder flags forwarded verbatim so per-platform tuning like
    # `-realtime 1` on videotoolbox still lands.
    assert "-realtime" in cmd and "1" in cmd


def test_mjpeg_source_no_encoder_raises():
    """MJPEG source on a platform without a hardware H.264 encoder —
    the recorder must refuse to spawn rather than silently produce
    broken files. This is the 'every recording must be playable'
    product rule as an enforced precondition."""
    with pytest.raises(UnsupportedSourceCodecError) as excinfo:
        _build(source_codec="MJPEG", encoder=None)
    # The message should mention the codec + the missing encoder so
    # log readers can diagnose without reading the exception class.
    msg = str(excinfo.value).lower()
    assert "mjpeg" in msg
    assert "encoder" in msg


def test_mjpeg_source_transcode_ignores_fps_reencode_setting():
    """A `recording_fps=5` setting normally produces the fps-reencode
    branch. When the source is MJPEG, the MJPEG transcode branch takes
    precedence — we care about codec conversion first, fps second.
    Worst case the user's fps setting is ignored on MJPEG cameras,
    which is strictly better than producing black recordings."""
    cmd = _build(
        source_codec="MJPEG",
        encoder="h264_videotoolbox",
        fps_setting="5",
    )
    # The MJPEG branch emits -b:v 2000k; the fps-reencode branch does
    # not. Checking for 2000k is a structural proof we took the MJPEG
    # path, not the fps path.
    assert "2000k" in cmd
