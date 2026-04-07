"""
RTSP-based camera discovery.

Finds cameras by scanning the local network for devices with open RTSP ports,
then tries known RTSP URL patterns per manufacturer. This catches cameras
that don't have ONVIF enabled (e.g., Reolink with ONVIF off).
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
from dataclasses import dataclass, field

from ..ffmpeg_path import get_ffprobe
from urllib.parse import quote, urlparse

logger = logging.getLogger(__name__)

RTSP_PORT = 554
CONNECT_TIMEOUT = 1.0  # seconds per host

# Known RTSP URL patterns by manufacturer.
# We try these in order and check for a valid RTSP response.
RTSP_PATTERNS: dict[str, list[str]] = {
    "Reolink": [
        "rtsp://{ip}:554/h264Preview_01_main",
        "rtsp://{ip}:554/h264Preview_01_sub",
    ],
    "Tapo": [
        "rtsp://{ip}:554/stream1",
        "rtsp://{ip}:554/stream2",
    ],
    "Eufy": [
        "rtsp://{ip}:554/live0",
        "rtsp://{ip}:554/live1",
    ],
    "Hikvision": [
        "rtsp://{ip}:554/Streaming/Channels/101",
        "rtsp://{ip}:554/Streaming/Channels/102",
    ],
    "Dahua": [
        "rtsp://{ip}:554/cam/realmonitor?channel=1&subtype=0",
        "rtsp://{ip}:554/cam/realmonitor?channel=1&subtype=1",
    ],
    "Amcrest": [
        "rtsp://{ip}:554/cam/realmonitor?channel=1&subtype=0",
    ],
    "Generic": [
        "rtsp://{ip}:554/",
        "rtsp://{ip}:554/stream",
        "rtsp://{ip}:554/live",
        "rtsp://{ip}:554/ch0_0.h264",
    ],
}


@dataclass
class RtspEndpoint:
    ip: str
    port: int = 554
    manufacturer_hint: str | None = None
    rtsp_uri: str | None = None
    server_header: str | None = None


def _get_local_subnet() -> str | None:
    """Get the local IP and derive the /24 subnet."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        # Return the /24 prefix
        parts = local_ip.split(".")
        return f"{parts[0]}.{parts[1]}.{parts[2]}"
    except Exception:
        return None


async def _check_rtsp_port(ip: str, port: int = 554) -> RtspEndpoint | None:
    """Check if a host has RTSP port open and try to identify it."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port),
            timeout=CONNECT_TIMEOUT,
        )

        # Send RTSP OPTIONS to get server identity
        request = f"OPTIONS rtsp://{ip}:{port}/ RTSP/1.0\r\nCSeq: 1\r\n\r\n"
        writer.write(request.encode())
        await writer.drain()

        try:
            response = await asyncio.wait_for(reader.read(4096), timeout=2.0)
        except (asyncio.TimeoutError, Exception):
            response = b""

        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

        endpoint = RtspEndpoint(ip=ip, port=port)

        # Parse server header for manufacturer hints
        response_str = response.decode("utf-8", errors="ignore")
        server_match = re.search(r"Server:\s*(.+)", response_str, re.IGNORECASE)
        if server_match:
            endpoint.server_header = server_match.group(1).strip()
            endpoint.manufacturer_hint = _identify_manufacturer(
                endpoint.server_header
            )

        return endpoint

    except (asyncio.TimeoutError, OSError, ConnectionRefusedError):
        return None


def _identify_manufacturer(server_header: str) -> str | None:
    """Guess manufacturer from RTSP Server header."""
    header_lower = server_header.lower()
    patterns = {
        "Reolink": ["reolink"],
        "Hikvision": ["hikvision", "hikv"],
        "Dahua": ["dahua"],
        "Tapo": ["tapo", "tp-link"],
        "Eufy": ["eufy", "anker"],
        "Amcrest": ["amcrest"],
        "Axis": ["axis"],
        "Ubiquiti": ["ubnt", "unifi"],
    }
    for mfr, keywords in patterns.items():
        if any(kw in header_lower for kw in keywords):
            return mfr
    return None


async def is_port_alive(ip: str, port: int = 554, timeout: float = 2.0) -> bool:
    """
    Quick TCP connect check to verify a camera is still reachable.
    Doesn't do an RTSP handshake — just confirms the port is open.
    """
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout=timeout
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except (asyncio.TimeoutError, OSError, ConnectionRefusedError):
        return False


async def scan_rtsp_devices(
    known_ips: set[str] | None = None,
    subnet: str | None = None,
) -> list[RtspEndpoint]:
    """
    Scan the local /24 subnet for devices with open RTSP port.
    Skips IPs already known (e.g., already found via ONVIF).
    """
    if subnet is None:
        subnet = _get_local_subnet()
    if subnet is None:
        logger.warning("Could not determine local subnet for RTSP scan")
        return []

    known = known_ips or set()

    # Skip common non-camera IPs (gateways, DNS)
    skip_suffixes = {1, 255}

    # Scan all hosts in parallel, skip known IPs and gateways
    tasks = []
    for i in range(1, 255):
        if i in skip_suffixes:
            continue
        ip = f"{subnet}.{i}"
        if ip in known:
            continue
        tasks.append(_check_rtsp_port(ip))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    endpoints = []
    for r in results:
        if isinstance(r, RtspEndpoint) and r is not None:
            endpoints.append(r)

    logger.info(
        "RTSP scan found %d new device(s) on %s.0/24",
        len(endpoints),
        subnet,
    )
    return endpoints


async def test_rtsp_credentials(
    ip: str,
    username: str,
    password: str,
    manufacturer: str | None = None,
) -> str | None:
    """
    Try RTSP URLs with credentials and return the first one that works.
    Returns the working RTSP URI, or None if all fail.

    Uses FFmpeg as a probe — it handles Basic, Digest, and other auth
    schemes that a hand-rolled RTSP client wouldn't.
    """
    urls = get_rtsp_urls(ip, manufacturer)
    encoded_user = quote(username, safe="")
    encoded_pass = quote(password, safe="")

    for url in urls:
        parsed = urlparse(url)
        netloc = f"{encoded_user}:{encoded_pass}@{parsed.hostname}"
        if parsed.port:
            netloc += f":{parsed.port}"
        authed_url = parsed._replace(netloc=netloc).geturl()

        # Use ffprobe to test the URL — fast and handles all auth schemes
        cmd = [
            get_ffprobe(),
            "-rtsp_transport", "tcp",
            "-rw_timeout", "5000000",  # 5s in microseconds
            "-loglevel", "error",
            "-show_streams",
            "-of", "default=noprint_wrappers=1",
            authed_url,
        ]

        try:
            from asyncio import create_subprocess_exec as spawn
            from asyncio.subprocess import PIPE

            proc = await spawn(*cmd, stdout=PIPE, stderr=PIPE)
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=8.0
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                continue

            if proc.returncode == 0 and b"codec_type" in stdout:
                logger.info("RTSP auth success via %s", url)
                return authed_url
            else:
                err = stderr.decode("utf-8", errors="ignore").lower()
                logger.debug("RTSP test failed for %s: %s", url, err[:200])
                # If 401/403 explicit, credentials are bad — no point trying
                # other paths on the same camera
                if "401" in err or "403" in err or "not authorized" in err:
                    continue
                # Other errors (404, timeout, etc.) — try next URL
                continue

        except FileNotFoundError:
            logger.error("ffprobe not found in PATH")
            return None
        except Exception as e:
            logger.debug("RTSP probe error for %s: %s", url, e)
            continue

    return None


def get_rtsp_urls(ip: str, manufacturer: str | None = None) -> list[str]:
    """Get ordered list of RTSP URLs to try for a given camera."""
    urls = []

    # Try manufacturer-specific patterns first
    if manufacturer and manufacturer in RTSP_PATTERNS:
        urls.extend(
            pattern.format(ip=ip) for pattern in RTSP_PATTERNS[manufacturer]
        )

    # Then try all other patterns
    for mfr, patterns in RTSP_PATTERNS.items():
        if mfr != manufacturer:
            for pattern in patterns:
                url = pattern.format(ip=ip)
                if url not in urls:
                    urls.append(url)

    return urls
