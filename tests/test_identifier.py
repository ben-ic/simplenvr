"""
Golden tests for the camera fingerprint identifier.

These tests pin down the behavior of the anchor-gate scoring system
introduced in 2026-04-08. The original bug they prevent: cheap
unauthenticated cameras being mis-identified as Axis (or Reolink, or
Hikvision) because brand fingerprints contained generic strings like
"lighttpd", "Profile/Streaming", or "webserver" that matched any cheap
ONVIF camera on the network.

The fix has two layers:
  1. Data layer (already shipped): the 9-fingerprint generic-string
     audit removed obvious cross-contamination.
  2. Structural layer (this file's subject): every signal is now
     either Anchored (brand-bearing) or Supporting (consistent but
     not exclusive). The scorer requires at least one Anchored hit
     before counting Supporting hits at all. This makes it
     IMPOSSIBLE for a fingerprint to win on generic signals alone,
     regardless of who edits the fingerprint database in the future.

Run with:
    .venv/bin/python -m pytest tests/test_identifier.py -v
"""

from __future__ import annotations

import pytest

from backend.discovery.identifier import IdentifySignals, identify


# ─── 1. Real cameras for each major brand identify correctly ────────

def test_real_axis_identifies_as_axis():
    """A real Axis M3045 with all anchored signals → Axis Communications."""
    sig = IdentifySignals(
        ip="10.0.0.10",
        hostname="axis-00408c123456",
        onvif_scopes=(
            "onvif://www.onvif.org/name/AXIS",
            "onvif://www.onvif.org/hardware/AXIS-M3045",
        ),
        http_title="AXIS M3045",
        rtsp_path="/axis-media/media.amp",
        normalized_mac_brand="AXIS COMMUNICATIONS AB",
    )
    result = identify(sig)
    assert result is not None
    assert result.brand == "Axis Communications"


def test_real_reolink_identifies_as_reolink():
    """A real Reolink RLC-410 with all anchored signals → Reolink."""
    sig = IdentifySignals(
        ip="10.0.0.46",
        hostname="Camera1",
        onvif_scopes=(
            "onvif://www.onvif.org/type/video_encoder",
            "onvif://www.onvif.org/name/Reolink",
            "onvif://www.onvif.org/hardware/RLC-410",
        ),
        http_server="webserver",
        http_title="Reolink",
        rtsp_path="/h264Preview_01_main",
        normalized_mac_brand="Reolink",
    )
    result = identify(sig)
    assert result is not None
    assert result.brand == "Reolink"


def test_real_tapo_identifies_as_tapo():
    """A real Tapo C120 with name/Tapo ONVIF scope and MAC alias."""
    sig = IdentifySignals(
        ip="10.0.0.99",
        hostname="C120",
        onvif_scopes=(
            "onvif://www.onvif.org/type/video_encoder",
            "onvif://www.onvif.org/Profile/Streaming",
            "onvif://www.onvif.org/name/Tapo",
        ),
        http_server="lighttpd/1.4.35",
        http_title="Tapo Camera",
        normalized_mac_brand="TP-LINK",
    )
    result = identify(sig)
    assert result is not None
    assert result.brand == "TP-Link Tapo"


# ─── 2. Bare generic ONVIF cam returns None (the original bug) ───────

def test_bare_generic_onvif_cam_returns_none():
    """
    The original "Tapo → Axis" bug: a cheap ONVIF cam with only
    generic signals (lighttpd webserver, generic Profile/Streaming
    scope) used to confidently identify as Axis. With the anchor
    gate, it now correctly returns None ("Unknown camera"), which
    is a much better UX than confidently wrong.
    """
    sig = IdentifySignals(
        ip="10.0.0.50",
        onvif_scopes=("onvif://www.onvif.org/Profile/Streaming",),
        http_server="lighttpd/1.4.35",
    )
    result = identify(sig)
    assert result is None, (
        "A cam with only generic signals should NOT identify as any brand. "
        f"Got: {result.brand if result else None} (confidence={result.confidence if result else 0})"
    )


# ─── 3. Tapo C120 with no MAC still identifies via ONVIF anchor ─────

def test_tapo_c120_no_mac_identifies_via_onvif_anchor():
    """
    The exact regression case from the 2026-04-08 audit. A Tapo with
    no MAC available (cross-subnet, ARP table empty, etc.) but with
    its name/Tapo ONVIF scope present must still identify as Tapo
    via the anchored ONVIF scope, not as Axis via phantom generics.
    """
    sig = IdentifySignals(
        ip="10.0.0.99",
        hostname="C120",  # supporting (model-only shape)
        onvif_scopes=(
            "onvif://www.onvif.org/Profile/Streaming",
            "onvif://www.onvif.org/name/Tapo",  # anchored
        ),
        http_server="lighttpd/1.4.35",
        # NO normalized_mac_brand
    )
    result = identify(sig)
    assert result is not None
    assert result.brand == "TP-Link Tapo"


# ─── 4. Tapo with MAC alone identifies via IEEE anchor ─────────────

def test_tapo_with_only_mac_identifies():
    """
    IEEE MAC OUI is implicitly anchored (registry-backed). A Tapo
    that has nothing else — no hostname, no ONVIF, no HTTP — should
    still identify if the MAC alias resolves to TP-LINK.
    """
    sig = IdentifySignals(
        ip="10.0.0.99",
        normalized_mac_brand="TP-LINK",
    )
    result = identify(sig)
    assert result is not None
    assert result.brand == "TP-Link Tapo"


# ─── 5. Supporting hostname alone is NOT enough ─────────────────────

def test_supporting_hostname_alone_returns_none():
    """
    The structural test: a camera with ONLY a supporting-tier signal
    matching (here, the Tapo `^C\\d{3}` model-only hostname) and no
    anchored signal must NOT win. This is what the anchor gate is
    structurally protecting against — without it, any device with
    DHCP hostname "C120" would identify as Tapo.
    """
    sig = IdentifySignals(
        ip="10.0.0.99",
        hostname="C120",  # matches Tapo's Supporting(^C\d{3}) only
        # No ONVIF, no MAC, no HTTP, no RTSP — nothing anchored
    )
    result = identify(sig)
    assert result is None, (
        "A device with only a supporting hostname match should NOT identify. "
        f"Got: {result.brand if result else None}"
    )


# ─── 6. Reolink camera vs Reolink NVR disambiguation ────────────────

def test_reolink_nvr_vs_camera_disambiguation():
    """
    Both fingerprints have name/Reolink as an anchored ONVIF scope.
    For an NVR, the RLN8-410 hostname should pull the result toward
    the NVR fingerprint (device_type='hub'), not the camera.
    """
    sig = IdentifySignals(
        ip="10.0.0.5",
        hostname="RLN8-410",
        onvif_scopes=("onvif://www.onvif.org/name/Reolink",),
        normalized_mac_brand="Reolink",
    )
    result = identify(sig)
    assert result is not None
    # The NVR fingerprint should win because its RLN\d+ hostname
    # pattern fires; the camera fingerprint's hostname patterns
    # don't match RLN8-410.
    assert result.brand == "Reolink Home Hub / NVR"
    assert result.device_type == "hub"


# ─── 7. Annke (Hikvision OEM) with Hikvision-Webs server header ─────

def test_annke_with_hikvision_server_still_identifies_as_annke():
    """
    Annke is a Hikvision OEM rebadge, so an Annke camera legitimately
    reports a Hikvision-Webs HTTP server header. With both Annke's
    and Hikvision's anchored signals available, the more-specific
    match (Annke hostname + Annke MAC alias) should win the brand,
    NOT the parent (Hikvision). This tests that the anchor gate
    doesn't accidentally favor the rebadge parent.
    """
    sig = IdentifySignals(
        ip="10.0.0.20",
        hostname="ANNKE-Cam-1",
        onvif_scopes=("onvif://www.onvif.org/name/Annke",),
        http_server="Hikvision-Webs",
        normalized_mac_brand="ANNKE",
    )
    result = identify(sig)
    assert result is not None
    assert result.brand == "Annke", (
        f"Annke (with brand-specific anchors) should beat Hikvision "
        f"(only HTTP server matches). Got: {result.brand}"
    )


# ─── 8. The historical cross-brand bug: explicit regression test ────

def test_no_brand_can_win_from_lighttpd_alone():
    """
    Historical bug: any cam reporting `Server: lighttpd/1.4.35` got
    +6 Axis points because Axis listed "lighttpd" as a server
    substring. With the anchor gate, this is now impossible
    regardless of whether a future fingerprint edit re-introduces
    "lighttpd" or any other generic string into ANY brand's list:
    a single supporting match cannot satisfy the anchor gate.
    """
    sig = IdentifySignals(
        ip="10.0.0.51",
        http_server="lighttpd/1.4.35",
    )
    result = identify(sig)
    assert result is None
