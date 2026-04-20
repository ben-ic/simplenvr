"""Per-session ffmpeg for HomeKit live streaming.

Reads from a go2rtc loopback RTSP URL and emits SRTP-keyed H.264 video
on the iOS client's negotiated UDP port. One ffmpeg child per active
HomeKit viewer; spawned on `start_stream()`, torn down on `stop_stream()`.

This is NOT the recording ffmpeg — it's an on-demand live transcode that
reads the same go2rtc loopback the recorder already pulls from, keeping
the "single RTSP client per camera" invariant intact.

The VIDEO_OUTPUT template is ported verbatim from Home Assistant's
homeassistant/components/homekit/type_cameras.py. HA runs this against
every RTSP-capable camera in the world at millions-of-installs scale;
our job is to copy, not rewrite.

M1 scope: video only. Audio lands in M6 behind the homekit-audio-proxy
port (the `-an` flag in VIDEO_OUTPUT already excludes audio from this
output stream, so adding audio is purely additive).
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from typing import Any

logger = logging.getLogger(__name__)


# Stream the camera's native H.264 into SRTP without re-encoding.
#
# We tried the "transcode through h264_videotoolbox at iOS-negotiated
# bitrate" approach that Home Assistant uses. It technically works but
# the quality is blocky: iOS asks for ~300 kbps on room-view tile
# previews (Apple UX decision, not a bug), videotoolbox can't produce
# clean 720p at that budget, and the result looks worse than the
# source. Re-packetizing the camera's existing H.264 stream bypasses
# the whole budget problem — iOS just plays what the camera already
# produces. CPU per session is also near-zero vs ~5-15% with the
# encode path, which nails the plan's ≤5%/viewer criterion.
#
# Trade-off: we lose the ability to match iOS's negotiated resolution.
# iPhone gets whatever the camera's substream actually encodes. In
# practice HAP clients tolerate this; Apple Home scales to fit the
# tile. The advertised resolution list becomes a hint, not a contract.
VIDEO_OUTPUT = (
    "-map {v_map} -an "
    "-c:v copy "
    "-payload_type 99 "
    "-ssrc {v_ssrc} -f rtp "
    "-srtp_out_suite AES_CM_128_HMAC_SHA1_80 -srtp_out_params {v_srtp_key} "
    "srtp://{address}:{v_port}?rtcpport={v_port}&"
    "localrtpport={v_port}&pkt_size={v_pkt_size}"
)


def build_video_stream_cmd(
    ffmpeg_bin: str,
    rtsp_url: str,
    stream_config: dict[str, Any],
) -> list[str]:
    """Assemble the ffmpeg argv for one HAP SRTP video session.

    `stream_config` is the dict HAP-python hands our CameraAccessory
    inside `start_stream()` — see pyhap/camera.py. Keys used below
    (`v_ssrc`, `v_srtp_key`, `address`, `v_port`, `v_max_bitrate`,
    `v_max_mtu`, `fps`) are required by the HAP spec and always present
    in a valid setup-endpoints negotiation.

    With `-c:v copy` we re-packetize the camera's existing H.264
    NAL units into SRTP. Encoder-specific params (profile, level,
    bitrate, framerate) are all irrelevant here — iPhone decodes
    whatever the camera's substream already produces. The fields we
    keep are purely HAP session plumbing: SSRC, SRTP key, peer
    address/port, MTU.
    """
    # pyhap sets `v_max_mtu` only if iOS's setup-endpoints TLV
    # includes it (pyhap/camera.py:569 is conditional). Default to
    # 1316 when missing. Coerce bytes→int on older pyhap versions
    # that leak raw TLV bytes through the stream_config dict.
    v_max_mtu_raw = stream_config.get("v_max_mtu", 1316)
    if isinstance(v_max_mtu_raw, (bytes, bytearray)):
        v_max_mtu = int.from_bytes(v_max_mtu_raw, "little")
    else:
        v_max_mtu = int(v_max_mtu_raw)
    substitution = {
        "v_map": "0:v:0",
        "v_ssrc": stream_config["v_ssrc"],
        "v_srtp_key": stream_config["v_srtp_key"],
        "address": stream_config["address"],
        "v_port": stream_config["v_port"],
        "v_pkt_size": v_max_mtu,
    }
    output_tail = VIDEO_OUTPUT.format(**substitution)
    return [
        ffmpeg_bin,
        "-hide_banner",
        "-nostats",
        # go2rtc's loopback only advertises RTSP-over-TCP; default
        # ffmpeg behavior tries UDP first which gets back `461
        # Unsupported transport` and wastes ~5s on fallback. Forcing
        # TCP skips the rejection. Matches what the recorder / motion
        # / audio ffmpegs all do in this codebase.
        "-rtsp_transport", "tcp",
        "-i",
        rtsp_url,
        *shlex.split(output_tail),
    ]


async def spawn_stream_proc(argv: list[str]) -> asyncio.subprocess.Process:
    """Spawn the per-session ffmpeg via argv list (no shell).

    Caller owns the returned Process. We start a background drainer on
    the stderr pipe — without it, any warning/error line from ffmpeg
    eventually fills the OS pipe buffer (~512 KB on macOS), ffmpeg
    blocks on `write(2, ...)`, and RTP output stalls. The symptom on
    the iPhone side is "tile spins forever" with no visible error.
    Draining lets ffmpeg run indefinitely and surfaces encoder / RTSP
    / SRTP errors at DEBUG so we can diagnose future failures.
    """
    logger.debug("HAP video ffmpeg: %s", " ".join(argv))
    spawn = asyncio.create_subprocess_exec
    proc = await spawn(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    asyncio.create_task(_drain_stderr(proc))
    return proc


async def _drain_stderr(proc: asyncio.subprocess.Process) -> None:
    """Read ffmpeg stderr line-by-line; log at DEBUG. Stops on EOF."""
    if proc.stderr is None:
        return
    pid = proc.pid
    try:
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                logger.debug("ffmpeg[%d]: %s", pid, text)
    except Exception as exc:
        logger.debug("ffmpeg[%d] stderr drain ended: %s", pid, exc)
