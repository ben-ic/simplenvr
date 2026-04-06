"""
Discovery scanner orchestrator.

Runs WS-Discovery probes on a loop, interrogates new cameras via ONVIF,
persists results to the database, and emits real-time events.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING

from .. import db
from ..config import PROBE_TIMEOUT, SCAN_INTERVAL
from ..models import Camera, ScanStatus, utcnow
from .mac_lookup import lookup_manufacturer_by_ip, lookup_manufacturer_by_model
from .onvif_client import interrogate_camera
from .rtsp_probe import is_port_alive, scan_rtsp_devices
from .ws_discovery import parse_scopes, probe_onvif_devices

if TYPE_CHECKING:
    import aiosqlite

    from ..api.ws import EventBus

logger = logging.getLogger(__name__)


class DiscoveryScanner:
    def __init__(self, conn: aiosqlite.Connection, event_bus: EventBus):
        self._conn = conn
        self._event_bus = event_bus
        self._known_cameras: dict[str, Camera] = {}  # keyed by IP
        self._scanning = False
        self._last_scan: utcnow | None = None

    def get_status(self) -> ScanStatus:
        online = sum(1 for c in self._known_cameras.values() if c.status == "online")
        needs_auth = sum(
            1 for c in self._known_cameras.values() if c.status == "needs_auth"
        )
        return ScanStatus(
            scanning=self._scanning,
            last_scan=self._last_scan,
            cameras_found=len(self._known_cameras),
            cameras_online=online,
            cameras_needs_auth=needs_auth,
        )

    async def run_scan(self) -> None:
        """Execute a single discovery scan cycle."""
        self._scanning = True
        logger.info("Starting discovery scan...")

        try:
            endpoints = await probe_onvif_devices(timeout=PROBE_TIMEOUT)
            current_ips: set[str] = set()
            new_count = 0

            for ep in endpoints:
                current_ips.add(ep.ip)
                existing = self._known_cameras.get(ep.ip)

                if existing is None:
                    # New camera — interrogate it
                    scope_meta = parse_scopes(ep.scopes)
                    # Check if we have stored credentials for this IP
                    stored = await db.get_camera_by_ip(self._conn, ep.ip)
                    username = stored.username if stored else None
                    password = stored.password if stored else None

                    info = await interrogate_camera(
                        ep.xaddrs, ep.ip, scope_meta, username, password
                    )

                    # Manufacturer detection: ONVIF → MAC OUI → model name
                    manufacturer = info.manufacturer
                    if not manufacturer:
                        manufacturer = lookup_manufacturer_by_ip(ep.ip)
                    if not manufacturer:
                        manufacturer = lookup_manufacturer_by_model(info.model)

                    # Camera is only "online" if we have a working stream URI
                    # (which requires successful authentication)
                    is_authenticated = info.rtsp_uri is not None
                    status = "online" if is_authenticated else "needs_auth"

                    camera = Camera(
                        id=stored.id if stored else str(uuid.uuid4()),
                        ip=ep.ip,
                        xaddr=ep.xaddrs,
                        manufacturer=manufacturer,
                        model=info.model,
                        firmware=info.firmware,
                        serial_number=info.serial_number,
                        hardware_id=info.hardware_id,
                        resolutions=info.resolutions,
                        rtsp_uri=info.rtsp_uri,
                        status=status,
                        username=username,
                        password=password,
                        name=stored.name if stored else None,
                        first_seen=stored.first_seen if stored else utcnow(),
                        last_seen=utcnow(),
                    )

                    camera = await db.upsert_camera(self._conn, camera)
                    self._known_cameras[ep.ip] = camera
                    new_count += 1

                    await self._event_bus.emit(
                        "camera_found", {"camera": camera.model_dump(mode="json")}
                    )
                    logger.info(
                        "New camera: %s %s at %s",
                        camera.manufacturer or "Unknown",
                        camera.model or "Unknown",
                        camera.ip,
                    )

                else:
                    # Existing camera — update last_seen and re-check auth
                    existing.last_seen = utcnow()
                    if existing.status == "offline":
                        # Coming back online — restore proper auth state
                        existing.status = (
                            "online" if existing.rtsp_uri else "needs_auth"
                        )
                        await self._event_bus.emit(
                            "camera_updated",
                            {"camera": existing.model_dump(mode="json")},
                        )
                    await db.upsert_camera(self._conn, existing)
                    self._known_cameras[ep.ip] = existing

            # RTSP port scan for cameras without ONVIF (e.g., Reolink with ONVIF off)
            try:
                rtsp_endpoints = await scan_rtsp_devices(known_ips=current_ips)
                for rep in rtsp_endpoints:
                    current_ips.add(rep.ip)
                    existing = self._known_cameras.get(rep.ip)
                    if existing is not None:
                        # Existing camera reappeared via RTSP scan — restore status
                        if existing.status == "offline":
                            existing.status = (
                                "online" if existing.rtsp_uri else "needs_auth"
                            )
                            existing.last_seen = utcnow()
                            await db.upsert_camera(self._conn, existing)
                            self._known_cameras[rep.ip] = existing
                            await self._event_bus.emit(
                                "camera_updated",
                                {"camera": existing.model_dump(mode="json")},
                            )
                            logger.info("Camera back online (RTSP): %s", rep.ip)
                        else:
                            existing.last_seen = utcnow()
                            await db.upsert_camera(self._conn, existing)
                        continue
                    if rep.ip not in self._known_cameras:
                        stored = await db.get_camera_by_ip(self._conn, rep.ip)
                        # Use MAC OUI for manufacturer, fall back to RTSP header
                        rtsp_mfr = rep.manufacturer_hint or lookup_manufacturer_by_ip(rep.ip)
                        camera = Camera(
                            id=stored.id if stored else str(uuid.uuid4()),
                            ip=rep.ip,
                            xaddr=f"rtsp://{rep.ip}:{rep.port}",
                            manufacturer=rtsp_mfr,
                            model=None,
                            rtsp_uri=rep.rtsp_uri,
                            status="needs_auth",
                            username=stored.username if stored else None,
                            password=stored.password if stored else None,
                            name=stored.name if stored else None,
                            first_seen=stored.first_seen if stored else utcnow(),
                            last_seen=utcnow(),
                        )
                        camera = await db.upsert_camera(self._conn, camera)
                        self._known_cameras[rep.ip] = camera
                        new_count += 1
                        await self._event_bus.emit(
                            "camera_found",
                            {"camera": camera.model_dump(mode="json")},
                        )
                        logger.info(
                            "New camera (RTSP): %s at %s (server: %s)",
                            rep.manufacturer_hint or "Unknown",
                            rep.ip,
                            rep.server_header or "n/a",
                        )
            except Exception as e:
                logger.warning("RTSP scan failed: %s", e)

            # Detect cameras that went offline. For each potentially-lost
            # camera, do a quick TCP port check before marking it offline —
            # ONVIF/RTSP probes can be flaky on some cameras (e.g. Eufy)
            # but a plain socket connection is reliable.
            lost_ips = set(self._known_cameras.keys()) - current_ips
            for ip in list(lost_ips):
                camera = self._known_cameras[ip]
                if await is_port_alive(ip, 554):
                    # Still reachable — keep/restore as online (or needs_auth)
                    lost_ips.discard(ip)
                    current_ips.add(ip)
                    was_offline = camera.status == "offline"
                    # If we have an rtsp_uri it's online, otherwise needs_auth
                    camera.status = "online" if camera.rtsp_uri else "needs_auth"
                    camera.last_seen = utcnow()
                    await db.upsert_camera(self._conn, camera)
                    self._known_cameras[ip] = camera
                    if was_offline:
                        await self._event_bus.emit(
                            "camera_updated",
                            {"camera": camera.model_dump(mode="json")},
                        )
                        logger.info("Camera back online: %s", ip)
                    continue

                # Confirmed offline
                if camera.status != "offline":
                    camera.status = "offline"
                    await db.mark_camera_offline(self._conn, camera.id)
                    await self._event_bus.emit(
                        "camera_lost", {"camera_id": camera.id, "ip": ip}
                    )
                    logger.info("Camera offline: %s (%s)", camera.name or camera.model, ip)
                    self._known_cameras[ip] = camera

            self._last_scan = utcnow()

            await self._event_bus.emit(
                "scan_complete",
                {
                    "found": len(current_ips),
                    "new": new_count,
                    "lost": len(lost_ips),
                },
            )

        except Exception as e:
            logger.error("Scan failed: %s", e, exc_info=True)

        finally:
            self._scanning = False

    async def run_forever(self) -> None:
        """Run discovery scans in a loop."""
        # Load known cameras from DB on startup
        cameras = await db.get_all_cameras(self._conn)
        for cam in cameras:
            self._known_cameras[cam.ip] = cam

        logger.info("Scanner started. %d cameras loaded from database.", len(cameras))

        while True:
            await self.run_scan()
            await asyncio.sleep(SCAN_INTERVAL)

    async def authenticate_camera(
        self,
        camera: Camera,
        username: str,
        password: str,
        apply_to_manufacturer: bool = False,
    ) -> Camera:
        """Submit credentials for a camera and re-interrogate."""
        is_onvif = camera.xaddr.startswith("http")

        if is_onvif:
            # ONVIF camera — test via ONVIF protocol
            info = await interrogate_camera(
                camera.xaddr, camera.ip, username=username, password=password
            )
            camera.username = username
            camera.password = password
            camera.status = "needs_auth" if info.needs_auth else "online"
            camera.manufacturer = info.manufacturer or camera.manufacturer
            camera.model = info.model or camera.model
            camera.firmware = info.firmware or camera.firmware
            camera.serial_number = info.serial_number or camera.serial_number
            camera.resolutions = info.resolutions or camera.resolutions
            camera.rtsp_uri = info.rtsp_uri or camera.rtsp_uri
        else:
            # RTSP-only camera — test by trying RTSP URLs with credentials
            from .rtsp_probe import test_rtsp_credentials

            rtsp_uri = await test_rtsp_credentials(
                camera.ip, username, password, camera.manufacturer
            )
            camera.username = username
            camera.password = password
            if rtsp_uri:
                camera.rtsp_uri = rtsp_uri
                camera.status = "online"
                logger.info("RTSP auth success for %s: %s", camera.ip, rtsp_uri)
            else:
                camera.status = "needs_auth"
                logger.info("RTSP auth failed for %s", camera.ip)

        camera.last_seen = utcnow()

        camera = await db.upsert_camera(self._conn, camera)
        self._known_cameras[camera.ip] = camera

        await self._event_bus.emit(
            "camera_updated", {"camera": camera.model_dump(mode="json")}
        )

        # Apply same credentials to other cameras from same manufacturer
        if apply_to_manufacturer and camera.manufacturer:
            for other in list(self._known_cameras.values()):
                if (
                    other.id != camera.id
                    and other.status == "needs_auth"
                    and other.manufacturer == camera.manufacturer
                ):
                    await self.authenticate_camera(other, username, password, False)

        return camera
