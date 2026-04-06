"""
Custom WS-Discovery implementation for ONVIF camera discovery.

Replaces the LGPL-licensed WSDiscovery library with a minimal MIT-compatible
implementation. Sends a SOAP Probe via UDP multicast to 239.255.255.250:3702
and parses ProbeMatch responses to find ONVIF device endpoints.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

MULTICAST_GROUP = "239.255.255.250"
MULTICAST_PORT = 3702

# XML namespaces used in WS-Discovery
NS = {
    "s": "http://www.w3.org/2003/05/soap-envelope",
    "a": "http://schemas.xmlsoap.org/ws/2004/08/addressing",
    "d": "http://schemas.xmlsoap.org/ws/2005/04/discovery",
}

PROBE_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing"
            xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
            xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <s:Header>
    <a:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</a:Action>
    <a:MessageID>uuid:{message_id}</a:MessageID>
    <a:ReplyTo>
      <a:Address>http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous</a:Address>
    </a:ReplyTo>
    <a:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</a:To>
  </s:Header>
  <s:Body>
    <d:Probe>
      <d:Types>dn:NetworkVideoTransmitter</d:Types>
    </d:Probe>
  </s:Body>
</s:Envelope>"""


@dataclass
class DiscoveredEndpoint:
    xaddrs: str  # ONVIF device service URL
    ip: str
    scopes: list[str] = field(default_factory=list)


def _parse_probe_match(xml_data: bytes) -> list[DiscoveredEndpoint]:
    """Parse a ProbeMatches SOAP response into discovered endpoints."""
    endpoints = []
    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError:
        logger.debug("Failed to parse XML response")
        return endpoints

    for match in root.findall(".//d:ProbeMatch", NS):
        xaddrs_el = match.find("d:XAddrs", NS)
        scopes_el = match.find("d:Scopes", NS)

        if xaddrs_el is None or not xaddrs_el.text:
            continue

        # XAddrs may contain multiple space-separated URLs
        for xaddr in xaddrs_el.text.strip().split():
            try:
                parsed = urlparse(xaddr)
                ip = parsed.hostname or ""
            except Exception:
                continue

            scopes = []
            if scopes_el is not None and scopes_el.text:
                scopes = scopes_el.text.strip().split()

            endpoints.append(DiscoveredEndpoint(xaddrs=xaddr, ip=ip, scopes=scopes))

    return endpoints


class _ProbeProtocol(asyncio.DatagramProtocol):
    def __init__(self):
        self.responses: list[bytes] = []
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr):
        self.responses.append(data)

    def error_received(self, exc):
        logger.debug("UDP error: %s", exc)


async def probe_onvif_devices(timeout: float = 5.0) -> list[DiscoveredEndpoint]:
    """
    Send a WS-Discovery Probe and collect ONVIF device responses.

    Returns a list of discovered endpoints with their ONVIF service URLs.
    """
    message_id = str(uuid.uuid4())
    probe_xml = PROBE_TEMPLATE.format(message_id=message_id).encode("utf-8")

    loop = asyncio.get_running_loop()

    # Create UDP socket for multicast
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    # Don't receive our own multicast
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
    sock.setblocking(False)

    transport, protocol = await loop.create_datagram_endpoint(
        _ProbeProtocol, sock=sock
    )

    try:
        transport.sendto(probe_xml, (MULTICAST_GROUP, MULTICAST_PORT))
        logger.debug("Sent WS-Discovery probe (message_id=%s)", message_id)

        await asyncio.sleep(timeout)

        endpoints = []
        seen_ips: set[str] = set()
        for response in protocol.responses:
            for ep in _parse_probe_match(response):
                if ep.ip and ep.ip not in seen_ips:
                    seen_ips.add(ep.ip)
                    endpoints.append(ep)

        logger.info("WS-Discovery found %d device(s)", len(endpoints))
        return endpoints

    finally:
        transport.close()


def parse_scopes(scopes: list[str]) -> dict[str, str]:
    """
    Extract structured metadata from ONVIF scope URIs.

    Scopes look like:
      onvif://www.onvif.org/name/CameraName
      onvif://www.onvif.org/hardware/ModelX
      onvif://www.onvif.org/location/city/Building
    """
    metadata: dict[str, str] = {}
    for scope in scopes:
        parts = scope.rstrip("/").split("/")
        if len(parts) >= 2:
            key = parts[-2].lower()
            value = parts[-1]
            if key in ("name", "hardware", "location"):
                metadata[key] = value.replace("%20", " ")
    return metadata


# Standalone test
if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.DEBUG)

    async def main():
        print("Sending WS-Discovery probe...")
        endpoints = await probe_onvif_devices(timeout=5.0)
        if not endpoints:
            print("No ONVIF devices found.")
            sys.exit(0)
        for ep in endpoints:
            meta = parse_scopes(ep.scopes)
            print(f"  {ep.ip}: {ep.xaddrs}")
            if meta:
                print(f"    Scopes: {meta}")

    asyncio.run(main())
