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

import time

from .. import db, go2rtc_client
from ..config import PROBE_TIMEOUT, SCAN_INTERVAL, URI_PROBE_INTERVAL
from ..models import Camera, ScanStatus, utcnow
from .auth_backoff import AuthBackoffTracker
from .identifier import IdentifySignals, IdentifyResult, identify
from .mac_lookup import (
    get_arp_table,
    invalidate_arp_cache,
    lookup_manufacturer_by_ip,
    lookup_manufacturer_by_model,
)
from .network_probe import probe_all
from ..rtsp_url import authed_uri, strip_creds
from .onvif_client import interrogate_camera
from .rtsp_probe import is_port_alive, scan_rtsp_devices, verify_rtsp_uri
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
        self._auth_backoff = AuthBackoffTracker()
        # monotonic timestamp of the most recent URI verification per
        # camera id; used by _verify_stream_uris to enforce a cooldown
        self._last_uri_probe: dict[str, float] = {}

    async def _load_declared_brands(self) -> list[str]:
        """
        Read the user's declared camera brands from the settings table.

        This is the list the user picked during onboarding and is used
        as a confidence HINT by the fingerprint identifier (see
        identifier.py for the scoring semantics). Absent or malformed
        entries are treated as empty — the user might have skipped
        onboarding, which is a first-class option.
        """
        import json

        raw = await db.get_setting(self._conn, "declared_brands")
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
            return [b for b in parsed if isinstance(b, str) and b.strip()]
        except Exception:
            return []

    async def _identify_endpoint(
        self,
        ip: str,
        onvif_scopes: tuple[str, ...] = (),
        declared_brands: list[str] | None = None,
    ) -> tuple[IdentifyResult | None, str | None, str | None]:
        """
        Gather unauthenticated signals for an IP and run the fingerprint
        identifier over them. Returns (result, hostname, mac).

        This is the one call site that ties together network_probe,
        mac_lookup, and the fingerprint identifier. It runs
        probe_all() first because the HTTP probe's TCP connection
        triggers kernel-level ARP resolution as a side effect — by
        the time we read the ARP table immediately after, any
        reachable device's MAC is in the cache. For devices we can't
        probe (unreachable, firewalled, sleeping), the MAC lookup
        simply returns None and the identifier works off the
        remaining signals.
        """
        try:
            probe = await probe_all(ip, total_timeout=5.0)
        except Exception as e:
            logger.debug("probe_all(%s) failed: %s", ip, e)
            probe = None

        # Now read the (likely freshly-populated) ARP table and
        # normalize through IEEE + aliases.
        arp = get_arp_table()
        mac = arp.get(ip)
        normalized_brand = lookup_manufacturer_by_ip(ip) if mac else None

        signals = IdentifySignals(
            ip=ip,
            mac_address=mac,
            hostname=probe.hostname if probe else None,
            onvif_scopes=onvif_scopes,
            http_server=probe.http.server if (probe and probe.http) else None,
            http_title=probe.http.title if (probe and probe.http) else None,
            rtsp_path=None,  # not known until ONVIF GetStreamUri returns
            normalized_mac_brand=normalized_brand,
        )
        result = identify(signals, declared_brands=declared_brands or [])
        if result:
            top_reasons = ", ".join(m.signal for m in result.matches)
            logger.info(
                "Identified %s as %s (conf=%d, signals=[%s])",
                ip,
                result.brand,
                result.confidence,
                top_reasons,
            )
        else:
            logger.debug(
                "No fingerprint match for %s (host=%r mac_brand=%r)",
                ip,
                signals.hostname,
                signals.normalized_mac_brand,
            )
        return result, probe.hostname if probe else None, mac

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

        # Invalidate the ARP cache at the start of every scan pass.
        # The cache has a 5-second TTL intended to avoid hammering
        # the arp subprocess within a single pass, but across scans
        # we want fresh data so newly-plugged-in cameras get picked
        # up on the next pass rather than waiting for the TTL to
        # expire. Each _identify_endpoint() call below re-reads the
        # (now stale) cache *after* probing the device, which
        # populates the ARP table via kernel-level side effects.
        invalidate_arp_cache()

        # Load the user's declared brands once per scan so the
        # identifier can apply the onboarding hint. Safe to reload
        # every scan because it's a tiny query; this also means
        # settings changes (e.g. user adds a new brand) take effect
        # on the next scan rather than requiring a restart.
        declared_brands = await self._load_declared_brands()

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

                    # If we've been retrying auth on this camera and failing,
                    # back off. Only applies when we have a prior identity
                    # (stored.id) to key the tracker against — a genuinely
                    # never-before-seen camera always gets a first attempt.
                    if stored and not self._auth_backoff.should_attempt(stored.id):
                        wait = self._auth_backoff.seconds_until_next_attempt(stored.id)
                        logger.debug(
                            "Skipping auth retry for %s (%s): %.0fs until next attempt",
                            ep.ip,
                            stored.id,
                            wait,
                        )
                        # Preserve UI visibility — treat it as a known camera
                        # for this scan so the lost-camera detector doesn't
                        # flip it to offline.
                        stored.last_seen = utcnow()
                        self._known_cameras[ep.ip] = stored
                        continue

                    username = stored.username if stored else None
                    password = stored.password if stored else None

                    # Run the unauthenticated fingerprint identifier
                    # FIRST, before attempting ONVIF interrogation. This
                    # gives us a brand guess plus hostname and mac_address
                    # even for cameras we don't have credentials for yet,
                    # so the setup screen can show rich info instead of
                    # "Unknown Camera".
                    id_result, hostname, mac_address = await self._identify_endpoint(
                        ep.ip,
                        onvif_scopes=tuple(ep.scopes),
                        declared_brands=declared_brands,
                    )

                    info = await interrogate_camera(
                        ep.xaddrs, ep.ip, scope_meta, username, password
                    )

                    # Manufacturer detection hierarchy:
                    # 1. Authenticated ONVIF GetDeviceInformation (highest
                    #    confidence — the camera itself told us)
                    # 2. Unauthenticated fingerprint identifier (medium —
                    #    multi-signal match against the brand database)
                    # 3. MAC OUI fallback via raw IEEE (lowest — raw
                    #    corporate name, may not be a camera brand at all)
                    if info.manufacturer:
                        manufacturer = info.manufacturer
                        identification_source = "onvif"
                    elif id_result is not None:
                        manufacturer = id_result.brand
                        identification_source = "fingerprint"
                    else:
                        # No ONVIF info, no fingerprint match. Fall through
                        # to raw IEEE vendor name so the UI can at least
                        # show "Unknown camera — <vendor>".
                        manufacturer = lookup_manufacturer_by_ip(ep.ip)
                        identification_source = (
                            "fingerprint" if manufacturer else None
                        )

                    # Model: prefer authenticated ONVIF, else fall back
                    # to whatever lookup_manufacturer_by_model can infer.
                    # The fingerprint identifier does not currently
                    # extract models from hostnames (future: regex the
                    # hostname for known model patterns per brand).
                    model = info.model or lookup_manufacturer_by_model(info.model)

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
                        substream_uri=info.substream_uri,
                        status=status,
                        username=username,
                        password=password,
                        name=stored.name if stored else None,
                        first_seen=stored.first_seen if stored else utcnow(),
                        last_seen=utcnow(),
                        hostname=hostname,
                        mac_address=mac_address,
                        identification_source=identification_source,
                        device_type=id_result.device_type if id_result else "camera",
                    )

                    # Update backoff tracker with the interrogation result
                    # BEFORE the upsert so a DB hiccup can't mask a real
                    # failure signal.
                    if is_authenticated:
                        self._auth_backoff.record_success(camera.id)
                    else:
                        delay = self._auth_backoff.record_failure(camera.id)
                        logger.info(
                            "Auth failed for %s, next retry in %.0fs (failure #%d)",
                            ep.ip,
                            delay,
                            self._auth_backoff.failure_count(camera.id),
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
                        # Run the unauthenticated fingerprint identifier
                        # for RTSP-only devices too. These don't respond
                        # to ONVIF WS-Discovery but we can still probe
                        # hostname + HTTP + MAC.
                        (
                            id_result,
                            rtsp_hostname,
                            rtsp_mac,
                        ) = await self._identify_endpoint(
                            rep.ip,
                            onvif_scopes=(),
                            declared_brands=declared_brands,
                        )
                        # Manufacturer hierarchy (identical shape to the
                        # ONVIF branch but without the authenticated
                        # tier): fingerprint → RTSP header → raw IEEE.
                        if id_result is not None:
                            rtsp_mfr = id_result.brand
                            identification_source = "fingerprint"
                        elif rep.manufacturer_hint:
                            rtsp_mfr = rep.manufacturer_hint
                            identification_source = "fingerprint"
                        else:
                            rtsp_mfr = lookup_manufacturer_by_ip(rep.ip)
                            identification_source = (
                                "fingerprint" if rtsp_mfr else None
                            )

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
                            hostname=rtsp_hostname,
                            mac_address=rtsp_mac,
                            identification_source=identification_source,
                            device_type=id_result.device_type if id_result else "camera",
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
                    # Still reachable — keep the camera visible. But only
                    # *upgrade* the status if it was previously offline;
                    # a camera in needs_auth state requires user action
                    # to recover, and "port is open" is not proof the
                    # credentials or stored URI are still valid.
                    lost_ips.discard(ip)
                    current_ips.add(ip)
                    was_offline = camera.status == "offline"
                    if was_offline:
                        camera.status = (
                            "online" if camera.rtsp_uri else "needs_auth"
                        )
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

            # Periodically verify stored RTSP URIs still resolve — catches
            # cameras (Eufy) whose stream URL silently rotates while the
            # port stays open.
            await self._verify_stream_uris()

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

    async def _verify_stream_uris(self) -> None:
        """
        Re-probe each online camera's stored rtsp_uri at most once per
        URI_PROBE_INTERVAL seconds. A confirmed-stale URI demotes the
        camera to needs_auth, drops it from go2rtc, and emits a
        camera_updated event so the recorder manager tears down the
        failing ffmpeg pipeline.

        Probes run in parallel so the total wall-clock stays bounded by
        a single ffprobe timeout even for the full 32-camera fleet.
        """
        now = time.monotonic()
        # Snapshot the due list BEFORE launching any probes so late
        # results can't shift the cooldown window mid-cycle.
        due = [
            cam
            for cam in list(self._known_cameras.values())
            if cam.status == "online"
            and cam.rtsp_uri
            and (now - self._last_uri_probe.get(cam.id, 0.0)) >= URI_PROBE_INTERVAL
        ]
        if not due:
            return

        # verify_rtsp_uri talks to the camera, so it needs the authenticated
        # form rebuilt from the credential-free stored URI plus the row's
        # username/password.
        results = await asyncio.gather(
            *(verify_rtsp_uri(authed_uri(cam)) for cam in due),
            return_exceptions=True,
        )

        # Serialize the state mutations. Order matters because multiple
        # stale cameras would otherwise race on _known_cameras writes and
        # on the go2rtc admin API (which is single-flight anyway).
        probe_ts = time.monotonic()
        for camera, result in zip(due, results):
            self._last_uri_probe[camera.id] = probe_ts

            if isinstance(result, Exception):
                logger.debug(
                    "URI probe raised for %s (%s): %r",
                    camera.name or camera.ip,
                    camera.id,
                    result,
                )
                continue

            if result == "ok":
                continue

            if result == "unknown":
                # Transient / inconclusive — leave the camera alone and
                # let the next probe cycle try again. Most likely causes:
                # brief network glitch, ffprobe timeout, or the camera
                # itself is mid-reboot.
                logger.debug(
                    "URI probe inconclusive for %s (%s); will retry later",
                    camera.name or camera.ip,
                    camera.id,
                )
                continue

            # result == "stale" — authoritative signal that the stored
            # URI no longer points at a working stream. Demote.
            logger.warning(
                "Stale RTSP URI detected for %s (%s): %s — demoting to needs_auth",
                camera.name or camera.ip,
                camera.id,
                camera.rtsp_uri,
            )
            camera.status = "needs_auth"
            camera.last_seen = utcnow()
            camera = await db.upsert_camera(self._conn, camera)
            self._known_cameras[camera.ip] = camera

            # Drop from go2rtc so it stops hammering the dead URL. The
            # stream will be re-added when the user supplies fresh
            # credentials via authenticate_camera.
            if go2rtc_client.is_enabled():
                await go2rtc_client.remove_stream(camera.id)

            await self._event_bus.emit(
                "camera_updated",
                {"camera": camera.model_dump(mode="json")},
            )

    async def run_forever(self) -> None:
        """Run discovery scans in a loop."""
        # Load known cameras from DB on startup
        cameras = await db.get_all_cameras(self._conn)
        # Refresh manufacturer for any camera with missing or placeholder
        # values from old buggy firmware (e.g. Reolink returning literal
        # "Manufacturer"). Use MAC OUI lookup as the source of truth.
        BAD_MFR = {None, "", "Manufacturer", "manufacturer", "Unknown", "unknown"}
        for cam in cameras:
            if cam.manufacturer in BAD_MFR:
                new_mfr = lookup_manufacturer_by_ip(cam.ip)
                if not new_mfr:
                    new_mfr = lookup_manufacturer_by_model(cam.model)
                if new_mfr and new_mfr != cam.manufacturer:
                    cam.manufacturer = new_mfr
                    # Direct UPDATE — upsert's COALESCE would preserve old value
                    await self._conn.execute(
                        "UPDATE cameras SET manufacturer = ? WHERE id = ?",
                        (new_mfr, cam.id),
                    )
                    await self._conn.commit()
                    logger.info("Refreshed manufacturer for %s → %s", cam.ip, new_mfr)
            self._known_cameras[cam.ip] = cam

        logger.info("Scanner started. %d cameras loaded from database.", len(cameras))

        # Register every camera that has a working RTSP URL with go2rtc
        # so the recording layer can use the loopback path immediately
        # at startup. No-op when go2rtc is not configured. Failures here
        # are non-fatal — the recorder will retry per-camera on its
        # spawn path and fall back to direct URLs if go2rtc stays down.
        if go2rtc_client.is_enabled():
            registered = 0
            for cam in self._known_cameras.values():
                if cam.rtsp_uri:
                    auth_url = authed_uri(cam)
                    if auth_url and await go2rtc_client.add_stream(
                        cam.id, auth_url
                    ):
                        registered += 1
            logger.info(
                "Registered %d/%d cameras with go2rtc",
                registered,
                sum(1 for c in self._known_cameras.values() if c.rtsp_uri),
            )

        while True:
            await self.run_scan()
            await asyncio.sleep(SCAN_INTERVAL)

    async def manually_add_camera(
        self,
        ip: str,
        port: int,
        username: str,
        password: str,
        path: str | None = None,
        brand: str | None = None,
        name: str | None = None,
    ) -> Camera:
        """
        Escape hatch: create a camera entry from user-supplied details
        when auto-discovery didn't find it. The user tells us the IP
        and credentials, we probe common RTSP URL patterns (optionally
        biased toward the user's brand hint), and create a Camera row
        if any of them yield a valid stream.

        This is deliberately a secondary path — most users should rely
        on auto-discovery. Manual add is for VLAN-isolated cameras,
        cameras with ONVIF disabled, cameras on a routed subnet, or
        power-user workflows where the user already knows the URL.

        Raises ValueError with a user-facing message on any failure.
        """
        from urllib.parse import quote
        from .rtsp_probe import verify_rtsp_uri
        from .fingerprints import FINGERPRINTS

        ip_clean = (ip or "").strip()
        if not ip_clean:
            raise ValueError("IP address is required.")
        if not username or not password:
            raise ValueError("Username and password are required.")

        # Check for existing camera at this IP so we don't create
        # duplicate rows. If one exists, treat this as a credential
        # update instead — the user may have discovered the camera
        # auto but couldn't sign in and is now entering details
        # manually.
        existing = await db.get_camera_by_ip(self._conn, ip_clean)
        if existing is not None:
            return await self.authenticate_camera(
                existing, username, password, apply_to_manufacturer=False
            )

        # Build the candidate RTSP URL list. Order (in decreasing
        # priority):
        #   1. The user's exact path if they provided one
        #   2. The brand's known paths from fingerprints.py
        #   3. A shortlist of universal fallback paths that work on
        #      many generic cheap cameras
        candidates: list[str] = []
        cred = f"{quote(username, safe='')}:{quote(password, safe='')}"
        base = f"rtsp://{cred}@{ip_clean}:{port}"

        def add(path_str: str) -> None:
            normalized = path_str if path_str.startswith("/") else f"/{path_str}"
            url = f"{base}{normalized}"
            if url not in candidates:
                candidates.append(url)

        if path and path.strip():
            add(path.strip())

        if brand:
            brand_lower = brand.strip().lower()
            for fp in FINGERPRINTS:
                if brand_lower in fp.brand.lower() or fp.brand.lower() in brand_lower:
                    for p in fp.rtsp_example_paths:
                        add(p)
                    break

        # Universal fallbacks — commonly seen on generic/unknown
        # cameras. Only added if we don't already have a candidate
        # from the brand hint, to keep the probe budget small.
        universal_fallbacks = [
            "/Streaming/Channels/101",   # Hikvision, many OEMs
            "/cam/realmonitor?channel=1&subtype=0",  # Dahua family
            "/live",
            "/live0",
            "/stream1",
            "/h264Preview_01_main",      # Reolink
            "/videoMain",                 # Foscam
            "/axis-media/media.amp",      # Axis
        ]
        for p in universal_fallbacks:
            add(p)

        logger.info(
            "manually_add_camera(%s:%d): probing %d candidate RTSP URLs",
            ip_clean,
            port,
            len(candidates),
        )

        # Probe each candidate in order until one succeeds. Each probe
        # has its own timeout; we cap the total budget at ~40s (5s ×
        # 8 candidates) to keep the UX responsive. If no candidate
        # succeeds, report back the last error.
        working_uri: str | None = None
        last_result = "unknown"
        for url in candidates[:8]:  # budget cap
            result = await verify_rtsp_uri(url, timeout=5.0)
            last_result = result
            logger.info(
                "  probe %s → %s", url.replace(password, "***"), result
            )
            if result == "ok":
                working_uri = url
                break

        if working_uri is None:
            if last_result == "stale":
                raise ValueError(
                    f"Could not authenticate to {ip_clean}:{port}. "
                    f"Double-check the username and password."
                )
            raise ValueError(
                f"Could not reach a valid stream on {ip_clean}:{port}. "
                f"Check that the camera is powered on and that the IP "
                f"address is correct. If your camera uses a non-standard "
                f"RTSP path, enter it in the 'RTSP path' field."
            )

        # Build the Camera row. We intentionally skip ONVIF interrogation
        # here — this code path is for cameras that couldn't be
        # auto-discovered, which usually means ONVIF is off or
        # unreachable. If ONVIF happens to work too, the next scan cycle
        # will pick it up and upgrade the row.
        camera = Camera(
            id=str(uuid.uuid4()),
            ip=ip_clean,
            xaddr=f"rtsp://{ip_clean}:{port}",
            manufacturer=brand.strip() if brand else None,
            model=None,
            # Strip embedded credentials — storage convention is that
            # rtsp_uri is credential-free and username/password live in
            # their own columns. The probe loop above needed the authed
            # URL; at rest we store the clean form.
            rtsp_uri=strip_creds(working_uri),
            status="online",
            username=username,
            password=password,
            name=name.strip() if name else None,
            first_seen=utcnow(),
            last_seen=utcnow(),
            identification_source="manual",
        )
        camera = await db.upsert_camera(self._conn, camera)
        self._known_cameras[ip_clean] = camera

        await self._event_bus.emit(
            "camera_found", {"camera": camera.model_dump(mode="json")}
        )
        logger.info(
            "Manually added camera: %s at %s",
            camera.manufacturer or "Unknown",
            ip_clean,
        )
        return camera

    async def authenticate_camera(
        self,
        camera: Camera,
        username: str,
        password: str,
        apply_to_manufacturer: bool = False,
    ) -> Camera:
        """Submit credentials for a camera and re-interrogate."""
        # Manual reauth — user clicked the button, they want an attempt
        # right now. Clear any scheduled backoff so the attempt runs
        # immediately; the tracker will get repopulated below based on
        # the result.
        self._auth_backoff.reset(camera.id)

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
            camera.substream_uri = info.substream_uri or camera.substream_uri
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

        # Only record a success in the backoff tracker. Manual reauth
        # intentionally does NOT extend backoff on failure — a user
        # spamming the button must never make automatic retries wait
        # longer than they already would have.
        if camera.status == "online":
            self._auth_backoff.record_success(camera.id)

        camera = await db.upsert_camera(self._conn, camera)
        self._known_cameras[camera.ip] = camera

        # Push the new credentials into go2rtc so the loopback stream
        # picks up the working URL. PUT is idempotent — replaces the
        # producer if the stream already existed. The stored rtsp_uri
        # is credential-free; go2rtc still needs the authed form to
        # actually open the upstream connection.
        if camera.rtsp_uri and camera.status == "online":
            auth_url = authed_uri(camera)
            if auth_url:
                await go2rtc_client.add_stream(camera.id, auth_url)

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
