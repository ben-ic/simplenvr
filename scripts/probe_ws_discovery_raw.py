"""One-shot WS-Discovery probe that dumps the FULL raw XML response from
each camera on the LAN, so we can see exactly what's available
unauthenticated. Run from repo root with the .venv activated:

    PYTHONPATH=. python scripts/probe_ws_discovery_raw.py

For the MAC-fallback IP-reconciliation plan we want to know whether
the EndpointReference UUID (urn:uuid:...), hardware/model strings, or
any other stable identity field shows up in unauthenticated WS-Discovery
responses. Current ws_discovery.py only extracts XAddrs + Scopes; this
script bypasses that parser and prints the entire envelope so we can
see what we're throwing away.
"""
from __future__ import annotations

import socket
import struct
import sys
import time
import uuid
import xml.dom.minidom as minidom

MULTICAST_GROUP = "239.255.255.250"
MULTICAST_PORT = 3702

PROBE = f"""<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing"
            xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
            xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <s:Header>
    <a:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</a:Action>
    <a:MessageID>uuid:{uuid.uuid4()}</a:MessageID>
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
</s:Envelope>""".encode("utf-8")


def main() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except (AttributeError, OSError):
        pass
    sock.bind(("", 0))
    sock.settimeout(0.5)

    sock.sendto(PROBE, (MULTICAST_GROUP, MULTICAST_PORT))
    print(f"[sent] WS-Discovery Probe to {MULTICAST_GROUP}:{MULTICAST_PORT}")
    print(f"[listening for 4s]\n", flush=True)

    deadline = time.monotonic() + 4.0
    seen: set[str] = set()
    responses: list[tuple[str, bytes]] = []

    while time.monotonic() < deadline:
        try:
            data, addr = sock.recvfrom(65535)
        except socket.timeout:
            continue
        key = f"{addr[0]}:{len(data)}"
        if key in seen:
            continue
        seen.add(key)
        responses.append((addr[0], data))

    sock.close()

    if not responses:
        print("[no responses]", file=sys.stderr)
        return 1

    for ip, data in responses:
        print("=" * 78)
        print(f"FROM {ip}  ({len(data)} bytes)")
        print("=" * 78)
        try:
            pretty = minidom.parseString(data).toprettyxml(indent="  ")
            for line in pretty.splitlines():
                if line.strip():
                    print(line)
        except Exception:
            print(data.decode("utf-8", errors="replace"))
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
