"""
Unauthenticated network probes for camera identification.

Two probe functions, both time-bounded and safe against hanging
devices:

1. reverse_dns(ip) — look up the DHCP-registered hostname for an IP.
   Many camera brands reveal themselves in this name: Reolink firmware
   registers the hostname as "baichuan" (the parent company), Eufy
   battery cameras use product codes like "T8416..." (T8416 = eufyCam
   2C), Hikvision-family cameras often register as "NVR-...". This is
   one of the strongest unauthenticated identification signals
   available.

2. http_probe(ip) — fetch the unauthenticated web UI on common ports
   (80, 443, 8080, 8000) and capture the HTTP Server: header plus the
   HTML <title> of the login page. Most camera web UIs identify the
   brand in either the server banner ("nginx/..." is uninteresting
   but "Hikvision-Webs/..." is definitive) or the login page title
   ("Reolink Camera", "Dahua Web Service", etc.).

Both probes must be cancellable and time-bounded so an unresponsive
device cannot stall the discovery loop. Both use Python stdlib
where possible to avoid pulling new dependencies into the backend
bundle.
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


# ── Reverse DNS ─────────────────────────────────────────────────────

async def reverse_dns(ip: str, timeout: float = 2.0) -> str | None:
    """
    Look up the hostname for an IP via the system resolver.

    Runs socket.gethostbyaddr in a threadpool because the stdlib call
    is blocking and can take several seconds on a slow or down DNS
    server. We cap it at `timeout` seconds total.

    Returns None on any failure — host not found, DNS timeout, etc.
    Never raises.
    """
    loop = asyncio.get_running_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(None, socket.gethostbyaddr, ip),
            timeout=timeout,
        )
    except (asyncio.TimeoutError, socket.herror, socket.gaierror, OSError):
        return None
    except Exception as e:
        logger.debug("reverse_dns(%s) unexpected error: %s", ip, e)
        return None
    hostname = result[0] if result and result[0] else None
    if not hostname:
        return None
    return hostname


# ── HTTP probe ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class HttpProbeResult:
    """One successful HTTP probe result from a camera's web UI."""
    port: int
    scheme: str           # "http" or "https"
    status: int           # HTTP status code (200, 401, etc.)
    server: str | None    # HTTP Server: header
    title: str | None     # HTML <title> content


# Common ports and schemes to probe, in order of likelihood for
# camera web UIs. We stop at the first successful response.
HTTP_PROBE_TARGETS = [
    (80, "http"),
    (8080, "http"),
    (8000, "http"),
    (443, "https"),
    (8443, "https"),
]

# HTML title extraction — we're not parsing HTML properly, just
# looking for the first <title>...</title> in the response body.
TITLE_RE = re.compile(rb"<title[^>]*>([^<]*)</title>", re.IGNORECASE)


async def http_probe(ip: str, total_timeout: float = 4.0) -> HttpProbeResult | None:
    """
    Try common web UI ports on a camera and return the first successful
    response's identifying info (Server header + HTML title).

    Uses asyncio + raw sockets instead of aiohttp to avoid a new dep.
    We don't need full HTTP parsing — just the status line, the
    Server: header, and enough body to find a <title>. A 2 KB read
    is plenty for any camera login page header + title region.

    Returns None if no port responds within the total timeout.
    """
    deadline = asyncio.get_event_loop().time() + total_timeout
    for port, scheme in HTTP_PROBE_TARGETS:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0.5:
            # Not enough budget left to be worth trying another port
            break
        per_port = min(remaining, 1.5)
        result = await _probe_one(ip, port, scheme, per_port)
        if result is not None:
            return result
    return None


async def _probe_one(
    ip: str, port: int, scheme: str, timeout: float
) -> HttpProbeResult | None:
    """Single host:port probe. Returns None on any failure."""
    try:
        if scheme == "https":
            # For https we'd need ssl context. Skip for now — most
            # camera web UIs speak http on at least one port, and
            # adding ssl is extra deps / failure modes we don't need
            # for identification. We can revisit if a brand only
            # exposes https.
            return None
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout=timeout
        )
    except (OSError, asyncio.TimeoutError):
        return None
    except Exception as e:
        logger.debug("_probe_one(%s:%d) connect error: %s", ip, port, e)
        return None

    try:
        # Minimal HTTP/1.1 GET / request. Close the connection
        # immediately after so the camera doesn't keep-alive us.
        request = (
            f"GET / HTTP/1.1\r\n"
            f"Host: {ip}\r\n"
            f"User-Agent: SimpleNVR-Probe/0.1\r\n"
            f"Accept: text/html\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("ascii")
        try:
            writer.write(request)
            await asyncio.wait_for(writer.drain(), timeout=timeout)
        except (OSError, asyncio.TimeoutError):
            return None

        # Read up to 4 KB — enough for headers + title on every
        # camera web UI we've seen. A few cameras respond slowly, so
        # read in chunks and stop as soon as we have enough.
        buf = bytearray()
        read_deadline = asyncio.get_event_loop().time() + timeout
        while len(buf) < 4096:
            remaining = read_deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            try:
                chunk = await asyncio.wait_for(reader.read(1024), timeout=remaining)
            except (OSError, asyncio.TimeoutError):
                break
            if not chunk:
                break
            buf.extend(chunk)

        if not buf:
            return None

        return _parse_http_response(buf, port, scheme)
    finally:
        try:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), timeout=0.5)
        except Exception:
            pass


def _parse_http_response(
    data: bytes, port: int, scheme: str
) -> HttpProbeResult | None:
    """Extract status code, Server header, and <title> from a raw HTTP response."""
    try:
        header_end = data.find(b"\r\n\r\n")
        if header_end == -1:
            header_bytes = data
            body = b""
        else:
            header_bytes = data[:header_end]
            body = data[header_end + 4 :]

        header_lines = header_bytes.split(b"\r\n")
        if not header_lines:
            return None

        # Status line: "HTTP/1.1 200 OK"
        status_line = header_lines[0].decode("ascii", errors="replace")
        parts = status_line.split(" ", 2)
        try:
            status = int(parts[1]) if len(parts) >= 2 else 0
        except ValueError:
            status = 0

        server: Optional[str] = None
        for line in header_lines[1:]:
            decoded = line.decode("latin-1", errors="replace")
            if decoded.lower().startswith("server:"):
                server = decoded.split(":", 1)[1].strip() or None
                break

        title: Optional[str] = None
        title_match = TITLE_RE.search(body)
        if title_match:
            try:
                title = title_match.group(1).decode("utf-8", errors="replace").strip()
            except Exception:
                title = None
            if title is not None and not title:
                title = None

        return HttpProbeResult(
            port=port, scheme=scheme, status=status, server=server, title=title
        )
    except Exception as e:
        logger.debug("_parse_http_response error: %s", e)
        return None


# ── Combined probe helper ───────────────────────────────────────────

@dataclass(frozen=True)
class NetworkProbeResult:
    """All unauthenticated signals we could gather about a camera IP."""
    ip: str
    hostname: str | None
    http: HttpProbeResult | None


async def probe_all(ip: str, total_timeout: float = 5.0) -> NetworkProbeResult:
    """
    Run reverse DNS and HTTP probes in parallel and return the combined
    result. Both probes are subject to their own timeouts; the total
    wall-clock time is bounded by whichever is slower, capped at
    total_timeout.
    """
    dns_task = asyncio.create_task(reverse_dns(ip, timeout=2.0))
    http_task = asyncio.create_task(http_probe(ip, total_timeout=4.0))
    try:
        hostname, http_result = await asyncio.wait_for(
            asyncio.gather(dns_task, http_task, return_exceptions=True),
            timeout=total_timeout,
        )
    except asyncio.TimeoutError:
        # Cancel whichever one is still running
        for t in (dns_task, http_task):
            if not t.done():
                t.cancel()
        # Harvest whatever completed
        hostname = dns_task.result() if dns_task.done() and not dns_task.cancelled() else None
        http_result = (
            http_task.result()
            if http_task.done() and not http_task.cancelled()
            else None
        )

    if isinstance(hostname, Exception):
        hostname = None
    if isinstance(http_result, Exception):
        http_result = None
    return NetworkProbeResult(ip=ip, hostname=hostname, http=http_result)
