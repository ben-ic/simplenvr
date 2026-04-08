"""
MAC address lookup for camera manufacturer identification.

Single source of truth: the IEEE OUI registry (backend/discovery/_ieee_oui.py),
regenerated from https://standards-oui.ieee.org/ via scripts/fetch_ieee_oui.py.
52,490 prefixes covering every manufacturer that has ever registered a MAC
block with IEEE.

IEEE publishes raw legal-entity names ("Shenzhen Reolink Digital Technology
Co., Ltd.", "Smart Innovation LLC", "Hangzhou Hikvision Digital Technology
Co., Ltd.") rather than brand names. We normalize through a substring alias
table that maps common corporate names to the brand name users see on the
product box. Unknown manufacturers are returned as the raw IEEE name so the
UI can show "Unknown camera — <vendor>" instead of plain "Unknown".

There is deliberately NO hand-curated OUI dict in this module. Hand-curated
data drifts, accumulates typos, and has proven to carry silent bugs (a
previous version mislabeled an Oppo phone prefix as Eufy, and included a
prefix not in the IEEE registry at all). Refresh the IEEE data monthly by
running scripts/fetch_ieee_oui.py.
"""

from __future__ import annotations

import logging
import re
import subprocess

logger = logging.getLogger(__name__)


def _normalize_mac(mac: str) -> str:
    """Normalize MAC to lowercase colon-separated format."""
    mac = mac.lower().replace("-", ":").replace(".", ":")
    # Ensure each octet has 2 digits
    parts = mac.split(":")
    return ":".join(p.zfill(2) for p in parts)


# Short-lived ARP cache. Each `arp -a` invocation spawns a subprocess
# which can take ~100-500ms on macOS; calling it once per camera in a
# 32-camera scan would cost multiple seconds of pure overhead. The
# ARP table doesn't change meaningfully within a single scan pass, so
# we cache it for a few seconds.
_arp_cache: dict[str, str] = {}
_arp_cache_expires: float = 0.0
_ARP_TTL_SECONDS = 5.0


def get_arp_table() -> dict[str, str]:
    """
    Read the local ARP table and return {ip: mac} mapping.
    Works on macOS and Linux. Cached for ~5s to avoid re-spawning
    the arp subprocess on every lookup during a scan pass.
    """
    global _arp_cache, _arp_cache_expires
    import time
    now = time.monotonic()
    if _arp_cache and now < _arp_cache_expires:
        return _arp_cache

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
        # Return cached (possibly stale) data on failure rather than
        # None — better to hit an old entry than force every caller
        # to re-pay the subprocess cost.
        return _arp_cache

    _arp_cache = ip_to_mac
    _arp_cache_expires = now + _ARP_TTL_SECONDS
    return ip_to_mac


def invalidate_arp_cache() -> None:
    """Force the next get_arp_table() call to re-read from the OS."""
    global _arp_cache, _arp_cache_expires
    _arp_cache = {}
    _arp_cache_expires = 0.0


# ── Corporate-name → brand-name aliases ────────────────────────────
#
# IEEE's registry stores raw legal-entity names (the name on the
# paperwork when the manufacturer paid to register the OUI block).
# These are usually not the brand name a user sees on the product
# box. We normalize through this substring alias table so
# lookup_manufacturer returns "Reolink" instead of "Shenzhen Reolink
# Digital Technology Co., Ltd.", "Eufy" instead of "Smart Innovation
# LLC", "Hikvision" instead of "Hangzhou Hikvision Digital Technology
# Co., Ltd.".
#
# The FIRST matching substring wins, so entries are ordered from
# most-specific to least-specific. Matching is case-insensitive
# against the lowercased organization name.
_IEEE_CORPORATE_ALIASES: tuple[tuple[str, str], ...] = (
    # Tier 1 consumer — parent companies often don't match brand names
    ("reolink", "Reolink"),
    ("baichuan", "Reolink"),
    ("smart innovation", "Eufy"),  # Anker's Eufy holding entity
    ("anker", "Eufy"),
    ("tp-link systems", "Tapo"),   # TP-Link umbrella; could be Kasa too
    ("tp-link", "Tapo"),
    ("wyze", "Wyze"),
    ("arlo technologies", "Arlo"),
    ("nest labs", "Nest"),
    ("google", "Nest"),
    ("amazon technologies", "Ring"),
    ("ubiquiti", "Ubiquiti"),
    ("ubnt", "Ubiquiti"),
    ("amcrest", "Amcrest"),
    # Tier 2 prosumer
    ("hangzhou hikvision", "Hikvision"),
    ("hikvision", "Hikvision"),
    ("zhejiang dahua", "Dahua"),
    ("dahua technology", "Dahua"),
    ("dahua", "Dahua"),
    ("axis communications", "Axis"),
    ("zhejiang uniview", "Uniview"),
    ("uniview", "Uniview"),
    ("flir systems", "Lorex"),
    ("swann communications", "Swann"),
    ("swann", "Swann"),
    ("shenzhen foscam", "Foscam"),
    ("foscam", "Foscam"),
    ("annke", "Annke"),
    # Tier 3 niche
    ("robert bosch", "Bosch"),
    ("bosch", "Bosch"),
    ("hanwha", "Hanwha"),
    ("samsung techwin", "Hanwha"),
    ("pelco", "Pelco"),
    ("avigilon", "Avigilon"),
    ("vivotek", "Vivotek"),
    ("mobotix", "Mobotix"),
    ("i-pro", "i-PRO"),
    ("panasonic i-pro", "i-PRO"),
    ("geovision", "GeoVision"),
    ("xiaomi", "Xiaomi"),
    ("ezviz", "Ezviz"),
    ("hangzhou ezviz", "Ezviz"),
)


def _normalize_ieee_org(org_name: str) -> str | None:
    """
    Map an IEEE corporate name to a friendly brand name if possible.

    Returns the brand name for a known alias, or None if no alias
    matches. Matching is case-insensitive substring against the
    organization name; the FIRST matching alias wins, so the alias
    table should be ordered from most-specific to least-specific.
    """
    lower = org_name.lower()
    for needle, brand in _IEEE_CORPORATE_ALIASES:
        if needle in lower:
            return brand
    return None


def _ieee_lookup(mac: str) -> str | None:
    """
    Look up a MAC prefix in the IEEE registry.

    Tries longest-prefix match first (36-bit → 28-bit → 24-bit) so a
    sub-block (MA-S or MA-M) registration wins over the parent 24-bit
    block when both exist. Returns the raw IEEE organization name, or
    None if the prefix is not in the registry.

    The generated IEEE module is imported lazily so the 2.2 MB file
    is only loaded on first use, not at module import time.
    """
    from . import _ieee_oui

    mac = _normalize_mac(mac)
    parts = mac.split(":")
    if len(parts) < 6:
        return None

    # Build the three lookup keys:
    #   24-bit: first 3 bytes as "aa:bb:cc"
    #   28-bit: first 3 bytes + first nibble of byte 4 as "aa:bb:cc:d"
    #   36-bit: first 4 bytes + first nibble of byte 5 as "aa:bb:cc:dd:e"
    key_24 = ":".join(parts[:3])
    key_28 = key_24 + ":" + parts[3][0] if parts[3] else None
    key_36 = (
        ":".join(parts[:4]) + ":" + parts[4][0]
        if parts[3] and parts[4]
        else None
    )

    # Most-specific first.
    if key_36 and key_36 in _ieee_oui.OUI_36:
        return _ieee_oui.OUI_36[key_36]
    if key_28 and key_28 in _ieee_oui.OUI_28:
        return _ieee_oui.OUI_28[key_28]
    return _ieee_oui.OUI_24.get(key_24)


def lookup_manufacturer(mac: str) -> str | None:
    """
    Look up the brand name for a MAC address via the IEEE registry.

    Returns a user-friendly brand name ("Reolink", "Eufy", "Hikvision")
    when the corporate name matches a known alias, or the raw IEEE
    organization name when it doesn't, or None if the prefix is not
    in the registry.

    The raw-name case is intentional: when we encounter a camera from
    a manufacturer we don't have an alias for, returning the IEEE name
    lets the UI show "Unknown camera — <vendor>" instead of plain
    "Unknown", which is measurably more helpful.
    """
    ieee_name = _ieee_lookup(mac)
    if ieee_name is None:
        return None
    normalized = _normalize_ieee_org(ieee_name)
    return normalized if normalized is not None else ieee_name


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
        "Reolink": [
            "rlc-", "rlk", "argus", "trackmix", "duo", "go plt", "go pt",
            "ipc_5",  # Internal Reolink names like IPC_523128M5MP_V2
            "ipc_4", "ipc_3", "ipc_6",
        ],
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
