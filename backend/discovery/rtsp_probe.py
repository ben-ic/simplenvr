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
from collections.abc import Iterable
from dataclasses import dataclass, field

from ..ffmpeg_path import get_ffprobe
from ..rtsp_url import strip_creds
from urllib.parse import quote, urlparse

logger = logging.getLogger(__name__)

RTSP_PORT = 554
CONNECT_TIMEOUT = 1.0  # seconds per host

# Cap concurrent RTSP port probes. A single /24 sweep is 253 hosts, which is
# fine to fan out at once, but multi-network scans can reach thousands of
# hosts — unbounded that would exhaust file descriptors and saturate the link.
# A bounded semaphore keeps the in-flight probe count flat regardless of how
# many networks are being swept.
MAX_CONCURRENT_PROBES = 256

# Redact embedded userinfo from text that may echo an authenticated RTSP URL
# (ffprobe stderr often includes the full URL it attempted). Applied to any
# string before it's handed to the logger so credentials never reach log
# files, support bundles, or the dev-mode console.
_CRED_ECHO_RE = re.compile(r"://[^@/\s]+@")


def _redact_creds(text: str) -> str:
    return _CRED_ECHO_RE.sub("://***@", text)

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

# Sub-stream URL candidates per brand. Given a working main stream,
# these are probed (with credentials) to find a lower-bitrate companion
# stream for the live preview grid. Order matters: first hit wins.
SUBSTREAM_PATTERNS: dict[str, list[str]] = {
    "Reolink": [
        "rtsp://{ip}:554/h264Preview_01_sub",
    ],
    "Tapo": [
        "rtsp://{ip}:554/stream2",
    ],
    "Eufy": [
        "rtsp://{ip}:554/live1",
    ],
    "Hikvision": [
        "rtsp://{ip}:554/Streaming/Channels/102",
    ],
    "Dahua": [
        "rtsp://{ip}:554/cam/realmonitor?channel=1&subtype=1",
    ],
    "Amcrest": [
        "rtsp://{ip}:554/cam/realmonitor?channel=1&subtype=1",
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


async def verify_rtsp_uri(uri: str, timeout: float = 6.0) -> str:
    """
    Check whether a stored RTSP URI still yields a valid stream.

    Returns one of:
      - "ok"      — stream responds with valid video/audio descriptors
      - "stale"   — server answered with 401/403/404 or 'stream not found',
                    meaning the URI no longer refers to a working resource
                    (credentials rotated or path renamed — the Eufy case)
      - "unknown" — timeout, connection refused, DNS failure, or any other
                    non-authoritative error. Caller should NOT treat this
                    as a signal to demote; let the lost-camera detector or
                    next probe cycle figure it out.

    ffprobe is the probe backend because it already handles Digest auth,
    URL-encoded credentials, multiple RTSP dialects, and is already bundled.
    """
    cmd = [
        get_ffprobe(),
        "-rtsp_transport", "tcp",
        "-rw_timeout", str(int(timeout * 1_000_000)),
        "-loglevel", "error",
        "-show_streams",
        "-of", "default=noprint_wrappers=1",
        uri,
    ]

    try:
        from asyncio import create_subprocess_exec as spawn
        from asyncio.subprocess import PIPE

        proc = await spawn(*cmd, stdout=PIPE, stderr=PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout + 2.0
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return "unknown"

        if proc.returncode == 0 and b"codec_type" in stdout:
            return "ok"

        err = stderr.decode("utf-8", errors="ignore").lower()
        # Authoritative "the URI is dead" signals. Match substrings so we
        # catch "server returned 404", "404 not found", "401 unauthorized",
        # "stream not found", etc. from various server implementations.
        stale_markers = (
            "401",
            "403",
            "404",
            "not found",
            "not authorized",
            "unauthorized",
            "forbidden",
        )
        if any(marker in err for marker in stale_markers):
            return "stale"

        # Non-authoritative failures (timeout, ECONNREFUSED, EHOSTUNREACH,
        # DNS, TLS handshake) — could be transient.
        return "unknown"

    except FileNotFoundError:
        logger.error("ffprobe not found in PATH — cannot verify RTSP URI")
        return "unknown"
    except Exception as e:
        logger.debug("verify_rtsp_uri error for %s: %s", strip_creds(uri), e)
        return "unknown"


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
    hosts: "Iterable[str] | None" = None,
) -> list[RtspEndpoint]:
    """
    Scan a set of hosts for devices with an open RTSP port.

    Three modes, in priority order:
      * `hosts` given   — probe exactly those IPs. This is the multi-network
                          path: the caller (DiscoveryScanner) resolves every
                          authorised interface subnet + routed CIDR into a
                          flat host list via discovery.networks and passes it
                          here. Probes are bounded by MAX_CONCURRENT_PROBES.
      * `subnet` given  — legacy single-/24 sweep of "<subnet>.1-254".
      * neither         — derive the host's primary /24 and sweep that
                          (original behaviour, preserved for callers/tests
                          that relied on it).

    Skips IPs already known (e.g. already found via ONVIF).
    """
    known = known_ips or set()

    if hosts is not None:
        host_list = [ip for ip in hosts if ip not in known]
    else:
        if subnet is None:
            subnet = _get_local_subnet()
        if subnet is None:
            logger.warning("Could not determine local subnet for RTSP scan")
            return []
        # Skip common non-camera IPs (gateways, DNS, broadcast).
        skip_suffixes = {1, 255}
        host_list = [
            f"{subnet}.{i}"
            for i in range(1, 255)
            if i not in skip_suffixes and f"{subnet}.{i}" not in known
        ]

    if not host_list:
        return []

    # Bound concurrency so a multi-network scan can't open thousands of
    # sockets at once. A single /24 still effectively runs unbounded
    # (253 < 256), preserving the original sweep latency.
    sem = asyncio.Semaphore(MAX_CONCURRENT_PROBES)

    async def _bounded(ip: str) -> RtspEndpoint | None:
        async with sem:
            return await _check_rtsp_port(ip)

    results = await asyncio.gather(
        *(_bounded(ip) for ip in host_list), return_exceptions=True
    )

    endpoints = []
    for r in results:
        if isinstance(r, RtspEndpoint) and r is not None:
            endpoints.append(r)

    logger.info(
        "RTSP scan found %d new device(s) across %d host(s)",
        len(endpoints),
        len(host_list),
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
                # Return the credential-free URL for storage. Callers
                # rebuild the authenticated URL at use time from the
                # camera row's username/password via rtsp_url.with_creds.
                return url
            else:
                err = stderr.decode("utf-8", errors="ignore").lower()
                # ffprobe stderr may echo the authenticated URL it tried;
                # redact before logging.
                logger.debug(
                    "RTSP test failed for %s: %s", url, _redact_creds(err[:200])
                )
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


def _derive_substream_candidates(main_uri: str, ip: str) -> list[str]:
    """
    Derive plausible sub-stream URLs from a working main stream URI.

    Covers brand-specific conventions (main→sub, stream1→stream2,
    Channels/101→102, subtype=0→1) plus generic mutations. Returns
    deduplicated candidates ordered from most to least likely.
    """
    parsed = urlparse(main_uri)
    path = parsed.path
    query = parsed.query
    base = f"rtsp://{ip}:{parsed.port or 554}"
    candidates: list[str] = []

    def _add(url: str) -> None:
        if url != main_uri and url not in candidates:
            candidates.append(url)

    # Reolink: /h264Preview_01_main → /h264Preview_01_sub
    if "_main" in path:
        _add(f"{base}{path.replace('_main', '_sub')}")

    # Tapo / generic: /stream1 → /stream2
    if path.endswith("/stream1"):
        _add(f"{base}{path[:-1]}2")

    # Eufy: /live0 → /live1
    if path.endswith("/live0"):
        _add(f"{base}{path[:-1]}1")

    # Hikvision: /Streaming/Channels/101 → /Streaming/Channels/102
    if re.search(r"/Channels/\d+01$", path):
        _add(f"{base}{path[:-2]}02")

    # Dahua/Amcrest: subtype=0 → subtype=1
    if "subtype=0" in query:
        _add(f"{base}{path}?{query.replace('subtype=0', 'subtype=1')}")

    # Generic: /channel/0 → /channel/1, /ch0 → /ch1
    if re.search(r"/ch(annel)?[/_]?0", path):
        sub_path = re.sub(r"(/ch(?:annel)?[/_]?)0", r"\g<1>1", path)
        _add(f"{base}{sub_path}")

    # Generic: path ends in /0 or /1 (some OEM cams)
    if re.search(r"/[01]$", path):
        last = path[-1]
        alt = "1" if last == "0" else "0"
        sub_path = path[:-1] + alt
        if sub_path != path:
            _add(f"{base}{sub_path}")

    return candidates


async def probe_substream(
    ip: str,
    username: str,
    password: str,
    main_uri: str,
    manufacturer: str | None = None,
) -> str | None:
    """
    Given a working main stream, probe for a sub-stream on the same camera.

    Tries brand-specific sub-stream paths first, then generic derivations
    from the main URI's path structure. Returns the credential-free
    sub-stream URI, or None. Typically completes in ~0.5-1s on LAN per
    candidate; short-circuits on first hit.
    """
    candidates: list[str] = []

    # Brand-specific candidates first (highest confidence)
    if manufacturer and manufacturer in SUBSTREAM_PATTERNS:
        for p in SUBSTREAM_PATTERNS[manufacturer]:
            url = p.format(ip=ip)
            if url != main_uri and url not in candidates:
                candidates.append(url)

    # Generic derivations from the main URI's path structure
    for url in _derive_substream_candidates(main_uri, ip):
        if url not in candidates:
            candidates.append(url)

    # Other brands' patterns (lowest priority)
    for mfr, patterns in SUBSTREAM_PATTERNS.items():
        if mfr != manufacturer:
            for p in patterns:
                url = p.format(ip=ip)
                if url != main_uri and url not in candidates:
                    candidates.append(url)

    if not candidates:
        return None

    encoded_user = quote(username, safe="")
    encoded_pass = quote(password, safe="")

    for url in candidates:
        parsed = urlparse(url)
        netloc = f"{encoded_user}:{encoded_pass}@{parsed.hostname}"
        if parsed.port:
            netloc += f":{parsed.port}"
        authed_url = parsed._replace(netloc=netloc).geturl()

        result = await verify_rtsp_uri(authed_url, timeout=4.0)
        if result == "ok":
            logger.info("Sub-stream found for %s: %s", ip, url)
            return url
        if result == "stale":
            logger.debug(
                "Sub-stream candidate rejected (stale) for %s: %s", ip, url
            )
            break

    logger.debug("No sub-stream found for %s (tried %d candidates)", ip, len(candidates))
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
