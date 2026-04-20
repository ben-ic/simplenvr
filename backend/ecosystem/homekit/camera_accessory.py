"""Per-camera HAP accessory — one per online SimpleNVR camera.

Subclasses `pyhap.camera.Camera` and overrides start_stream / stop_stream
to spawn our own ffmpeg (reads go2rtc loopback, emits SRTP to the iOS
device) instead of HAP-python's bundled macOS-webcam demo command.

`get_snapshot` is NOT overridden in M1 — HAP-python's default placeholder
JPEG ships. Real live snapshots land in M6.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, TYPE_CHECKING

from pyhap.camera import (
    Camera as PyhapCamera,
    VIDEO_CODEC_PARAM_LEVEL_TYPES,
    VIDEO_CODEC_PARAM_PROFILE_ID_TYPES,
)
from pyhap.util import get_local_address

from ...ffmpeg_path import get_ffmpeg
from .stream_ffmpeg import build_video_stream_cmd, spawn_stream_proc

if TYPE_CHECKING:
    from pyhap.accessory_driver import AccessoryDriver

    from ...models import Camera

logger = logging.getLogger(__name__)


def _build_options(local_address: str) -> dict[str, Any]:
    """HAP-python `Camera` options dict.

    Profile / level / resolution set are the Apple-Home-wide intersection:
    Main 3.1 is supported by every HAP client from iOS 11 onward, and the
    three resolutions cover Apple TV (1080p), iPhone / iPad (720p tile),
    and Apple Watch (360p glance). iOS negotiates one of these on each
    `setup-endpoints` call.

    `stream_count=2` caps concurrent SRTP sessions per camera at the plan's
    locked value. A 3rd simultaneous viewer will see "No Response" in
    Apple Home while the first two sessions hold their slots.
    """
    return {
        "video": {
            "codec": {
                "profiles": [VIDEO_CODEC_PARAM_PROFILE_ID_TYPES["MAIN"]],
                "levels": [VIDEO_CODEC_PARAM_LEVEL_TYPES["TYPE3_1"]],
            },
            # With `-c:v copy` this list is a hint rather than a
            # contract — iPhone decodes whatever H.264 resolution the
            # camera's substream already produces, and Apple Home
            # scales the tile to fit. We keep a graduated list (ported
            # from HA's defaults) so iOS has sensible options to
            # advertise back via setup-endpoints for the preview /
            # fullscreen / Watch-glance code paths.
            "resolutions": [
                [1920, 1080, 30],
                [1280, 720, 30],
                [1024, 576, 30],
                [640, 360, 30],
                [480, 270, 30],
                [320, 180, 30],
            ],
        },
        "audio": {
            # Placeholder — M1 emits no audio (VIDEO_OUTPUT uses -an).
            # The codec declaration is required by HAP but ignored until
            # M6 wires homekit-audio-proxy.
            "codecs": [
                {"type": "OPUS", "samplerate": 16},
            ],
        },
        "address": local_address,
        "srtp": True,
        # start_stream_cmd is irrelevant because we override start_stream,
        # but set to empty to prevent HAP-python's macOS-avfoundation
        # default from running if anything ever skips the override path.
        "start_stream_cmd": "",
        "stream_count": 2,
    }


class CameraAccessory(PyhapCamera):
    """HAP accessory wired to a go2rtc RTSP loopback.

    `camera` is the SimpleNVR `Camera` model at accessory-construction
    time. We snapshot values we need (id, name, model, firmware) and
    don't re-read from the model object during streaming — the recording
    layer is the source of truth for camera state.
    """

    def __init__(
        self,
        driver: "AccessoryDriver",
        camera: "Camera",
        rtsp_url: str,
    ):
        options = _build_options(get_local_address())
        display_name = camera.name or f"Camera {camera.ip}"
        super().__init__(options, driver, display_name)

        self.set_info_service(
            manufacturer="SimpleNVR",
            model=camera.model or "IP Camera",
            serial_number=camera.id,
            firmware_revision=camera.firmware or "1.0.0",
        )

        self._camera_id = camera.id
        self._rtsp_url = rtsp_url
        self._session_procs: dict[str, asyncio.subprocess.Process] = {}

    async def start_stream(
        self,
        session_info: dict[str, Any],
        stream_config: dict[str, Any],
    ) -> bool:
        session_id = session_info["id"]
        argv = build_video_stream_cmd(get_ffmpeg(), self._rtsp_url, stream_config)
        try:
            proc = await spawn_stream_proc(argv)
        except Exception as e:
            logger.error(
                "HomeKit start_stream failed (camera=%s session=%s): %s",
                self._camera_id[:8], session_id, e, exc_info=True,
            )
            return False
        self._session_procs[session_id] = proc
        logger.info(
            "HomeKit stream started: camera=%s session=%s pid=%d",
            self._camera_id[:8], session_id, proc.pid,
        )
        return True

    async def stop_stream(self, session_info: dict[str, Any]) -> None:
        session_id = session_info["id"]
        proc = self._session_procs.pop(session_id, None)
        if proc is None:
            return
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            logger.warning(
                "HomeKit ffmpeg did not terminate in 2s; SIGKILL "
                "(camera=%s session=%s pid=%d)",
                self._camera_id[:8], session_id, proc.pid,
            )
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        logger.info(
            "HomeKit stream stopped: camera=%s session=%s",
            self._camera_id[:8], session_id,
        )

    async def reconfigure_stream(
        self,
        session_info: dict[str, Any],
        stream_config: dict[str, Any],
    ) -> bool:
        # iOS calls reconfigure_stream on tile→fullscreen transitions
        # and other bitrate renegotiations. Our previous implementation
        # tore down and respawned ffmpeg on every call, which produced
        # a visible video stutter every time the viewer changed scale.
        # HA's reference implementation (type_cameras.py:471-473) just
        # returns True — the already-running stream continues serving
        # the updated session. Since we're doing `-c:v copy`, iOS's
        # renegotiated bitrate doesn't affect our output anyway.
        return True

    async def stop(self) -> None:
        """Terminate every live session — called during bridge shutdown."""
        for session_id in list(self._session_procs.keys()):
            await self.stop_stream({"id": session_id})
