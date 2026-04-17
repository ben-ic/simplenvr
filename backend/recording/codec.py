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

Hardware DECODE is separate from hardware encode. When recording_fps !=
"original" the decoder feeds the encoder; when stream-copy is in effect
no decode happens at all. select_decoder() picks a per-platform -hwaccel
method; SIMPLENVR_HWACCEL=1 enables it (it is opt-in because surveillance
streams trip hardware decoders harder than software).

If no hardware encoder is available (e.g., headless Linux without a GPU),
select_encoder() returns None and build_record_cmd() silently falls back
to stream-copy regardless of the recording_fps setting.

The recorder ffmpeg now produces ONLY segments. The MJPEG fan-out that
fed snapshot consumers has been removed (R3 in the stability plan): a
slow consumer of stdout used to back-pressure the shared encoder and
stall segment writes. Snapshots are now encoded on demand from the
detect pipeline's already-decoded RGB frame via
`MotionManager.encode_latest_snapshot` — no extra ffmpeg, no extra
RTSP client, and bounded staleness at the 2 fps detect cadence.
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


def build_unified_cmd(
    rtsp_uri: str,
    output_pattern: Path,
    segment_secs: int,
    fps_setting: str,
    encoder: str | None,
    encoder_flags: list[str] | None,
) -> list[str]:
    """
    Build the recorder FFmpeg command that opens a single RTSP connection
    (via go2rtc's loopback) and writes segmented MP4 files to disk.

    Single output: segmented MP4 recording (stream-copy by default) via
    the segment muxer. Earlier revisions also emitted a scene-filtered
    MJPEG to stdout for the snapshot cache and motion preview, but a
    slow Python consumer of that pipe would back-pressure the shared
    encoder and stall segment writes. Detection v2 now reads frames from
    `DetectFfmpegSource` directly (its own low-res decode against the
    same go2rtc loopback), and snapshots are encoded on demand from that
    decoded frame via `MotionManager.encode_latest_snapshot`. No second
    ffmpeg is spawned.

    Browser live preview is a separate concern entirely: the frontend
    connects to go2rtc's WebRTC/MSE pipeline (or libmpv via the native
    plugin), not to anything emitted here.

    fps_setting:
      - "original" → -c copy (stream-copy; default; zero CPU, no patent risk)
      - "10", "5", "2", "1" → re-encode at that framerate via hardware encoder
      - "0.5" → re-encode at 1 frame every 2 seconds

    If `encoder` is None (no hardware encoder available on this platform),
    recording silently falls back to stream-copy regardless of fps_setting.
    """
    # RTSP-specific input hardening:
    #   -timeout 30000000: 30s socket I/O timeout on the RTSP demuxer,
    #     covering both the OPTIONS/DESCRIBE handshake and mid-stream RTP
    #     reads. Positioned before `-i` so ffmpeg binds it to the rtsp
    #     demuxer (demuxer-option context). Was `-stimeout` in ffmpeg <5;
    #     renamed to `-timeout` for the RTSP demuxer and the old alias
    #     was removed in 7.x. `-rw_timeout` is intentionally NOT set — it
    #     lives on a different AVClass and ffmpeg 8.1 rejects it when the
    #     server (e.g. go2rtc) answers DESCRIBE with SDP that triggers a
    #     child-demuxer reopen. The 30s figure (was 10s) accommodates
    #     Tapo/Reolink "spiky" firmware mode where the camera pushes brief
    #     packet bursts then EOFs every 500ms-2s — chained flaps under
    #     load can blow through a 10s budget. The supervise loop still
    #     declares failure within 30s on a truly-dead camera.
    #   -analyzeduration 10000000: 10s codec-discovery window (microsecs).
    #     ffmpeg's default analyzeduration finishes fast but gets
    #     truncated by the socket timeout when SPS/PPS arrive late,
    #     producing the "Could not find codec parameters for stream 0"
    #     error followed by [segment] dimensions not set / rc=234. mpv
    #     effectively probes indefinitely; 10s for ffmpeg matches what
    #     it does for spiky cameras without being unbounded.
    #   -probesize 10000000: 10 MB probe window. Pairs with
    #     -analyzeduration — the codec probe terminates when EITHER limit
    #     trips, so both must be raised together or the smaller one
    #     remains the effective ceiling.
    #   -rtbufsize 128M: 128 MB realtime input buffer. ffmpeg's default
    #     is ~3 MB, which is the smallest pair of pants in town for
    #     camera RTSP. mpv's analog (--demuxer-max-bytes) defaults to
    #     150 MB; 128 MB sits in the same league. At a typical 6 Mbps
    #     this absorbs ~170s of stream, easily riding through any
    #     realistic upstream wobble. Worst case 128 MB × 2 ffmpeg per
    #     camera × 32 cameras = 8 GB on a maxed-out install — fine on
    #     the 16+ GB Snapdragon X Elite Copilot+ minimum spec.
    #   -use_wallclock_as_timestamps 1: stamp frames with wall-clock time
    #     instead of trusting camera PTS, which on some cameras drifts/rolls
    #     and breaks segment duration calculations
    cmd: list[str] = [get_ffmpeg(), "-rtsp_transport", "tcp"]
    if rtsp_uri.lower().startswith("rtsp://"):
        cmd += [
            "-timeout", "30000000",
            "-analyzeduration", "10000000",
            "-probesize", "10000000",
            "-rtbufsize", "128M",
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
        # Allow the segmenter to cut up to 1s off the nominal boundary
        # when seeking the next keyframe. Cameras with long GOPs (8-15s,
        # common on cheap Tapo/Reolink) otherwise force the segmenter to
        # either skip an IDR (producing unplayable segments) or wait a
        # whole GOP past the target (producing irregular durations that
        # confuse the Browse-footage HLS stitcher). 1s tolerance is the
        # smallest value that handles realistic GOP jitter.
        "-segment_time_delta", "1.0",
        "-segment_format", "mp4",
        # Fragmented MP4 output. Each segment file is
        # `ftyp + moov + (moof+mdat)+`, self-contained and
        # playable in any modern browser via direct <video src>,
        # hls.js, VLC, QuickTime, etc. The Browse-footage day
        # view stitches segments into a continuous timeline with
        # a tiny HLS playlist (backend/api/recordings.py
        # /recordings/hls/index.m3u8) that lists each segment as
        # an independent HLS fragment with #EXT-X-DISCONTINUITY
        # between them — hls.js handles cross-segment PTS
        # normalization natively. No repackaging, no subprocess
        # overhead, no custom MP4 parsing.
        #
        # History: an earlier iteration of this comment claimed
        # browsers had to "scan the whole file" to seek inside a
        # fragmented segment, and the recorder was switched to
        # +faststart as a result. That concern was verified false
        # on 2026-04-09 against real 60-second camera files in
        # Chrome — seek to any scrubber position is instant. The
        # earlier measurement must have been taken on a different
        # workload (much larger files? older browsers?). Keeping
        # the fragmented path.
        #
        # Flags:
        # +frag_keyframe ........ one fragment per keyframe (GOP)
        # +empty_moov ........... moov has empty sample tables +
        #                         mvex declaring fragments follow
        # +default_base_moof .... every fragment is self-describing
        #                         (default-base-is-moof flag set)
        #
        # Tradeoff: the currently-in-progress segment file is not
        # playable from disk until the segmenter closes it (up to
        # ~segment_duration seconds, currently 1 minute). This is
        # acceptable because (a) live preview comes from go2rtc
        # directly via the frontend's <video-stream> custom element
        # — see backend/recording/camera_recorder.py docstring and
        # backend/api/streams.py for the lineage — and (b) the
        # Inbox's gap-fallback already handles "event just happened,
        # no playable segment yet" gracefully.
        "-segment_format_options",
        "movflags=+frag_keyframe+empty_moov+default_base_moof",
        "-reset_timestamps", "1",
        "-strftime", "1",
        str(output_pattern),
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

    return cmd + log_args + rec_args


# Backward-compat shim — kept only because main.spec / future tests might
# import the old name. The new code path uses build_unified_cmd directly.
def build_record_cmd(*args, **kwargs):  # pragma: no cover
    raise RuntimeError(
        "build_record_cmd is obsolete; use build_unified_cmd which produces "
        "the unified single-RTSP-connection ffmpeg command."
    )
