"""
MAC address OUI lookup for camera manufacturer identification.

Uses the local ARP table to get MAC addresses, then matches the first 3 bytes
(OUI prefix) against known camera manufacturers.
"""

from __future__ import annotations

import logging
import re
import subprocess

logger = logging.getLogger(__name__)

# OUI prefixes (first 3 bytes of MAC) mapped to manufacturers.
# Sources: IEEE OUI database + camera manufacturer registrations.
OUI_DATABASE: dict[str, str] = {
    # Reolink
    "ec:71:db": "Reolink",
    "b4:a3:82": "Reolink",
    "9c:8e:cd": "Reolink",
    # TP-Link / Tapo
    "30:de:4b": "Tapo",
    "60:a4:b7": "Tapo",
    "98:25:4a": "Tapo",
    "b0:a7:b9": "Tapo",
    "50:c7:bf": "Tapo",
    "68:ff:7b": "Tapo",
    "14:eb:b6": "Tapo",
    "a8:42:a1": "Tapo",
    "5c:a6:e6": "Tapo",
    "10:27:f5": "Tapo",
    "e8:48:b8": "Tapo",
    "30:83:98": "Tapo",
    "b0:19:21": "Tapo",
    # Eufy / Anker
    "78:c9:4e": "Eufy",
    "10:2c:b1": "Eufy",
    "e4:47:90": "Eufy",
    # Hikvision
    "c0:56:e3": "Hikvision",
    "44:19:b6": "Hikvision",
    "bc:ad:28": "Hikvision",
    "a4:14:37": "Hikvision",
    "28:57:be": "Hikvision",
    "54:c4:15": "Hikvision",
    "c4:2f:90": "Hikvision",
    "e0:50:8b": "Hikvision",
    # Dahua
    "3c:ef:8c": "Dahua",
    "40:f4:fd": "Dahua",
    "a0:bd:1d": "Dahua",
    "b0:02:47": "Dahua",
    "e0:50:8b": "Dahua",
    "00:1f:54": "Dahua",
    # Amcrest (uses Dahua OUIs too)
    "9c:8e:cd": "Amcrest",
    # Axis
    "00:40:8c": "Axis",
    "ac:cc:8e": "Axis",
    "b8:a4:4f": "Axis",
    # Ubiquiti (UniFi Protect)
    "24:5a:4c": "Ubiquiti",
    "68:d7:9a": "Ubiquiti",
    "74:83:c2": "Ubiquiti",
    "f0:9f:c2": "Ubiquiti",
    "fc:ec:da": "Ubiquiti",
}


def _normalize_mac(mac: str) -> str:
    """Normalize MAC to lowercase colon-separated format."""
    mac = mac.lower().replace("-", ":").replace(".", ":")
    # Ensure each octet has 2 digits
    parts = mac.split(":")
    return ":".join(p.zfill(2) for p in parts)


def get_arp_table() -> dict[str, str]:
    """
    Read the local ARP table and return {ip: mac} mapping.
    Works on macOS and Linux.
    """
    ip_to_mac: dict[str, str] = {}
    try:
        result = subprocess.run(
            ["arp", "-a"], capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            # macOS: ? (10.0.0.1) at 4:17:b6:64:87:1e on en0 ...
            # Linux: ? (10.0.0.1) at 04:17:b6:64:87:1e [ether] on eth0
            match = re.search(
                r"\((\d+\.\d+\.\d+\.\d+)\)\s+at\s+([0-9a-fA-F:.-]+)", line
            )
            if match:
                ip = match.group(1)
                mac = _normalize_mac(match.group(2))
                if mac != "ff:ff:ff:ff:ff:ff" and mac != "(incomplete)":
                    ip_to_mac[ip] = mac
    except Exception as e:
        logger.warning("Failed to read ARP table: %s", e)

    return ip_to_mac


def lookup_manufacturer(mac: str) -> str | None:
    """Look up manufacturer from a MAC address using OUI prefix."""
    mac = _normalize_mac(mac)
    prefix = ":".join(mac.split(":")[:3])
    return OUI_DATABASE.get(prefix)


def lookup_manufacturer_by_ip(ip: str) -> str | None:
    """Look up manufacturer for an IP address via ARP → OUI."""
    arp = get_arp_table()
    mac = arp.get(ip)
    if mac:
        mfr = lookup_manufacturer(mac)
        if mfr:
            logger.debug("MAC lookup: %s → %s → %s", ip, mac, mfr)
        return mfr
    return None


def lookup_manufacturer_by_model(model: str | None) -> str | None:
    """
    Guess manufacturer from a camera model name.
    Many camera models follow brand-specific naming conventions.
    """
    if not model:
        return None

    model_lower = model.lower()

    patterns = {
        "Reolink": ["rlc-", "rlk", "argus", "trackmix", "duo", "go plt", "go pt"],
        "Tapo": ["c1", "c2", "c3", "c4", "c5"],  # Tapo C100, C110, C200, C210, C310, C320
        "Eufy": ["t8", "eufycam"],  # Eufy T8210, T8400, etc.
        "Hikvision": ["ds-2", "ds-i", "ipc-h", "ds-cd"],
        "Dahua": ["ipc-hf", "ipc-hd", "ipc-h", "dh-"],
        "Amcrest": ["ip2m", "ip3m", "ip4m", "ip5m", "ip8m"],
        "Axis": ["m10", "m11", "m20", "m30", "p13", "p14", "p32"],
        "Wyze": ["wyzec", "wyzecam"],
        "Ubiquiti": ["uvc-", "g3", "g4", "g5"],
    }

    for mfr, prefixes in patterns.items():
        for prefix in prefixes:
            if model_lower.startswith(prefix) or f" {prefix}" in model_lower:
                return mfr

    # Special cases
    if "ipc-122" in model_lower or "ipc-123" in model_lower:
        return "Reolink"  # Reolink IPC-122 etc.

    return None


def get_all_camera_macs() -> dict[str, tuple[str, str | None]]:
    """
    Return {ip: (mac, manufacturer)} for all IPs in ARP table
    that match a known camera manufacturer.
    """
    arp = get_arp_table()
    results = {}
    for ip, mac in arp.items():
        mfr = lookup_manufacturer(mac)
        if mfr:
            results[ip] = (mac, mfr)
    return results
