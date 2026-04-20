"""HomeKitBridge — publishes SimpleNVR cameras into Apple Home.

Lifecycle-wise, this runs as an asyncio task inside the Python sidecar,
started from `backend/main.py`'s background-startup block after audio
has been wired up. A crashed / failed-to-start bridge must never change
whether the recorder writes segments — the recording lifecycle is
completely independent of HomeKit publication (plan §Architecture
"integration is additive, never subtractive").

M1 scope:
  - Enumerate DB cameras at start, advertise every online non-hub one.
  - Run HAP-python's AccessoryDriver (mDNS + pair server).
  - Clean shutdown that tells paired iOS clients we're going away.

NOT in M1 (planned for later milestones):
  - Live add/remove/update on camera_found / camera_deleted events (M2).
  - Motion sensor characteristic flip on motion_started / _ended (M3).
  - Paired-state restart verification (M4).
  - Settings UI + API endpoints (M5).
  - Snapshot + audio proxy (M6).
  - HKSV service (env-gated, separate decision).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import TYPE_CHECKING

from pyhap.accessory import Bridge
from pyhap.accessory_driver import AccessoryDriver

from ... import db, go2rtc_client
from .camera_accessory import CameraAccessory
from .state import accessory_state_path, load_or_generate_pincode

if TYPE_CHECKING:
    import aiosqlite

    from ...api.ws import EventBus

logger = logging.getLogger(__name__)

# Fixed HAP port so Windows can pre-register the firewall rule at MSI
# install time instead of popping an approval dialog on first bridge
# advertisement. The HAP spec doesn't mandate a port — mDNS advertises
# whatever we bind. HAP-python's library default is 51234; we pick 51826
# (the Homebridge-community convention) for cross-install consistency.
HAP_PORT = 51826


class HomeKitBridge:
    """Owns the AccessoryDriver + per-camera accessories."""

    def __init__(
        self,
        conn: "aiosqlite.Connection",
        event_bus: "EventBus",
    ):
        self._conn = conn
        self._event_bus = event_bus
        self._driver: AccessoryDriver | None = None
        self._bridge: Bridge | None = None
        self._accessories: dict[str, CameraAccessory] = {}
        self._driver_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Construct the driver, enumerate online cameras, start HAP.

        Callers MUST wrap this in try/except — a failed start should log
        and leave the rest of the sidecar untouched (integration is
        additive). See backend/main.py for the wrapper.
        """
        if not go2rtc_client.is_enabled():
            logger.warning(
                "HomeKit bridge not starting: go2rtc loopback is not "
                "configured (SIMPLENVR_GO2RTC_RTSP_URL unset). Cameras "
                "cannot be published without a single-RTSP-client source."
            )
            return

        persist_file = str(accessory_state_path())
        # HAP-python regenerates the pincode every driver construction
        # unless we pass one in — which would mean the setup code visible
        # in Settings rotates on every sidecar restart. Commercial HomeKit
        # accessories (and Home Assistant) persist the code; we match
        # that. See state.load_or_generate_pincode().
        pincode = load_or_generate_pincode()
        # Without loop=, AccessoryDriver.__init__ calls asyncio.new_event_loop()
        # and binds its HTTP server Protocol callbacks to that new loop.
        # Nobody ever iterates that loop, so the socket accepts connections
        # but connection_made / data_received never fire — the pair-setup
        # handshake silently stalls. Hand pyhap FastAPI's running loop so
        # its callbacks fire on the same loop that's driving our sidecar.
        loop = asyncio.get_running_loop()
        self._driver = AccessoryDriver(
            port=HAP_PORT,
            persist_file=persist_file,
            pincode=pincode,
            loop=loop,
        )
        self._bridge = Bridge(self._driver, "SimpleNVR")
        self._bridge.set_info_service(
            manufacturer="SimpleNVR",
            model="SimpleNVR Bridge",
            serial_number="simplenvr-bridge",
            firmware_revision="1.0.0",
        )

        cameras = await db.get_all_cameras(self._conn)
        advertised = 0
        skipped = 0
        for cam in cameras:
            if cam.status != "online" or cam.device_type == "hub":
                skipped += 1
                continue
            rtsp_url = go2rtc_client.loopback_url_for(cam.id)
            if not rtsp_url:
                skipped += 1
                continue
            accessory = CameraAccessory(
                driver=self._driver,
                camera=cam,
                rtsp_url=rtsp_url,
            )
            self._bridge.add_accessory(accessory)
            self._accessories[cam.id] = accessory
            advertised += 1

        self._driver.add_accessory(self._bridge)

        # async_start() kicks off the HAP TCP server + mDNS advertisement
        # on the current event loop and runs until async_stop(). We hold
        # the task so shutdown can cancel it deterministically.
        self._driver_task = asyncio.create_task(self._driver.async_start())

        # HAP-python prints the QR + setup code to stdout at driver-construct
        # time by default — that's the M1 pair path until M5's Settings UI
        # lands. We additionally log the pincode at INFO so Ben can grep it
        # out of the sidecar logs without waiting for the stdout dump.
        pincode = self._driver.state.pincode
        if isinstance(pincode, (bytes, bytearray)):
            pincode = pincode.decode("ascii")
        logger.info(
            "HomeKit bridge ready on port %d — %d camera(s) advertised, "
            "%d skipped. Setup code: %s",
            HAP_PORT, advertised, skipped, pincode,
        )

    async def shutdown(self) -> None:
        """Clean disconnect — tells paired iOS clients we're going away.

        Best-effort. We never raise from shutdown because the overall
        sidecar lifespan teardown must not hang on HAP-python cleanup.
        """
        # Stop per-session ffmpegs first so they release RTSP slots on
        # go2rtc before the driver closes the HAP sessions under them.
        for accessory in self._accessories.values():
            with suppress(Exception):
                await accessory.stop()

        if self._driver is not None:
            with suppress(Exception):
                await self._driver.async_stop()

        if self._driver_task is not None and not self._driver_task.done():
            self._driver_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._driver_task

        self._accessories.clear()
        self._bridge = None
        self._driver = None
        self._driver_task = None
