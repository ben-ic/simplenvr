"""Tests for the tiered camera reconciliation engine.

Covers the 13 table-driven scenarios from
`plans/mac-fallback-ip-change-plan.md` §Tests. Every test runs against
`narrow()` (pure) and/or `reconcile()` (pure + stubbed interrogate_fn)
— no DB, no ONVIF, no network.

The load-bearing assertion is counted auth calls: the engine must
make AT MOST one verify-auth call per reconcile event, and only in
the MAC_MODERATE branch after a singleton prime suspect emerges from
unauthenticated narrowing. A regression that silently introduces
speculative auth against multiple candidates is the specific
failure mode these tests guard against.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from backend.models import Camera
from backend.discovery.reconcile import (
    MatchConfidence,
    NarrowSignals,
    narrow,
    reconcile,
)


def _run(coro):
    """Drive an async reconcile() call inside a sync test function.

    Avoids a pytest-asyncio dependency (not installed in this project;
    no other test in the suite needs it).
    """
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------

def make_camera(
    *,
    id: str = "cam-1",
    ip: str = "10.0.0.5",
    xaddr: str = "http://10.0.0.5:2020/onvif/device_service",
    manufacturer: str | None = None,
    model: str | None = None,
    name: str | None = None,
    hardware_id: str | None = None,
    serial_number: str | None = None,
    username: str | None = None,
    password: str | None = None,
    mac_address: str | None = None,
    alt_macs: list[str] | None = None,
    endpoint_reference: str | None = None,
    status: str = "offline",
) -> Camera:
    return Camera(
        id=id,
        ip=ip,
        xaddr=xaddr,
        manufacturer=manufacturer,
        model=model,
        name=name,
        hardware_id=hardware_id,
        serial_number=serial_number,
        username=username,
        password=password,
        mac_address=mac_address,
        alt_macs=alt_macs or [],
        endpoint_reference=endpoint_reference,
        status=status,  # type: ignore[arg-type]
        first_seen=datetime.now(timezone.utc),
        last_seen=datetime.now(timezone.utc),
    )


def make_signals(
    *,
    new_ip: str = "10.0.0.181",
    xaddr: str | None = "http://10.0.0.181:2020/onvif/device_service",
    endpoint_reference: str | None = None,
    mac: str | None = None,
    mac_brand: str | None = None,
    scope_hardware: str | None = None,
    scope_name: str | None = None,
    rtsp_server_banner: str | None = None,
) -> NarrowSignals:
    return NarrowSignals(
        new_ip=new_ip,
        xaddr=xaddr,
        endpoint_reference=endpoint_reference,
        mac=mac,
        mac_brand=mac_brand,
        scope_hardware=scope_hardware,
        scope_name=scope_name,
        rtsp_server_banner=rtsp_server_banner,
    )


class AuthRecorder:
    """Stub interrogate_fn. Records every call + returns a scripted result.

    A live implementation calls into ONVIF; here we assert the sequence
    of auth attempts directly. `result` is whatever the real function
    would return: a `CameraInfo`-shaped object or None. Using
    SimpleNamespace lets tests hand back ad-hoc objects without
    depending on the real CameraInfo class.
    """

    def __init__(self, result=None):
        self.calls: list[tuple[str, str, str, str]] = []
        self.result = result

    async def __call__(self, ip: str, xaddr: str, username: str, password: str):
        self.calls.append((ip, xaddr, username, password))
        return self.result


# ---------------------------------------------------------------------------
# Case 1: EPR matches one candidate, mac_brand compatible → EPR_EXACT, 0 auth
# ---------------------------------------------------------------------------


def test_case_01_epr_exact_match_no_auth():
    cam = make_camera(
        endpoint_reference="urn:uuid:abc", manufacturer="Hikvision",
    )
    signals = make_signals(
        endpoint_reference="urn:uuid:abc", mac="aa:bb:cc:dd:ee:01",
        mac_brand="Hikvision",
    )
    auth = AuthRecorder()

    result = _run(reconcile(signals, [cam], auth))

    assert result is not None
    candidate, confidence, evidence = result
    assert candidate.id == "cam-1"
    assert confidence is MatchConfidence.EPR_EXACT
    assert evidence == ["endpoint_reference"]
    assert auth.calls == []


# ---------------------------------------------------------------------------
# Case 2: EPR matches one candidate, mac_brand disagrees → None, 0 auth
# ---------------------------------------------------------------------------


def test_case_02_epr_brand_disagreement_refuses():
    # OUI says Hikvision, stored manufacturer is Dahua — the EPR match
    # is filtered out by the brand-compatibility gate. With no MAC
    # signal the engine falls through to NONE.
    cam = make_camera(
        endpoint_reference="urn:uuid:abc", manufacturer="Dahua",
    )
    signals = make_signals(
        endpoint_reference="urn:uuid:abc", mac_brand="Hikvision",
    )
    auth = AuthRecorder()

    result = _run(reconcile(signals, [cam], auth))

    assert result is None
    assert auth.calls == []


# ---------------------------------------------------------------------------
# Case 3: EPR matches two candidates → None, 0 auth
# ---------------------------------------------------------------------------


def test_case_03_epr_ambiguous_refuses():
    # Silent mis-reconcile is strictly worse than a stranded offline
    # row — the ambiguity policy from §Architecture.
    cam1 = make_camera(id="cam-1", endpoint_reference="urn:uuid:abc")
    cam2 = make_camera(
        id="cam-2", ip="10.0.0.6", endpoint_reference="urn:uuid:abc",
    )
    signals = make_signals(endpoint_reference="urn:uuid:abc")
    auth = AuthRecorder()

    result = _run(reconcile(signals, [cam1, cam2], auth))

    assert result is None
    assert auth.calls == []


# ---------------------------------------------------------------------------
# Case 4: MAC match + 2 corroborations → MAC_HIGH, 0 auth
# ---------------------------------------------------------------------------


def test_case_04_mac_high_two_corroborations_no_auth():
    cam = make_camera(
        mac_address="aa:bb:cc:dd:ee:01",
        manufacturer="Hikvision",
        model="DS-2CD2",
        name="Front Door",
    )
    signals = make_signals(
        mac="aa:bb:cc:dd:ee:01",
        mac_brand="Hikvision",
        scope_hardware="DS-2CD2",
        scope_name="Front Door",
    )
    auth = AuthRecorder()

    result = _run(reconcile(signals, [cam], auth))

    assert result is not None
    candidate, confidence, evidence = result
    assert candidate.id == "cam-1"
    assert confidence is MatchConfidence.MAC_HIGH
    # Primary-mac tag + every matching corroboration
    assert evidence[0] == "mac"
    assert "scope_hardware" in evidence
    assert "scope_name" in evidence
    assert "mac_brand" in evidence
    assert auth.calls == []


# ---------------------------------------------------------------------------
# Case 5: MAC match + 1 corroboration, verify-auth passes → MAC_MODERATE
# ---------------------------------------------------------------------------


def test_case_05_mac_moderate_verify_auth_pass():
    cam = make_camera(
        mac_address="aa:bb:cc:dd:ee:01",
        manufacturer="Hikvision",
        model="DS-2CD2",
        username="admin",
        password="hunter2",
        hardware_id="HWID-1234",
        serial_number="SN-5678",
    )
    signals = make_signals(
        mac="aa:bb:cc:dd:ee:01",
        mac_brand="Hikvision",
        scope_hardware="DS-2CD2",  # 1 corroboration; mac_brand also matches = 2
        # Actually with mac_brand also compatible that's 2. Drop mac_brand
        # to get a true 1-corroboration case.
    )
    # Drop the mac_brand signal so corroborations == 1 (scope_hardware only)
    signals.mac_brand = None
    auth = AuthRecorder(
        result=SimpleNamespace(
            hardware_id="HWID-1234", serial_number="SN-5678",
        )
    )

    result = _run(reconcile(signals, [cam], auth))

    assert result is not None
    candidate, confidence, evidence = result
    assert candidate.id == "cam-1"
    assert confidence is MatchConfidence.MAC_MODERATE
    assert evidence[0] == "mac"
    assert "scope_hardware" in evidence
    assert evidence[-1] == "verify_auth"
    assert len(auth.calls) == 1
    ip, xaddr, u, p = auth.calls[0]
    assert ip == signals.new_ip
    assert xaddr == signals.xaddr
    assert (u, p) == ("admin", "hunter2")


# ---------------------------------------------------------------------------
# Case 6: MAC match + 1 corroboration, verify-auth mismatch → None
# ---------------------------------------------------------------------------


def test_case_06_mac_moderate_verify_auth_mismatch_refuses():
    # HardwareId differs at the new IP → hardware was swapped while
    # keeping the same MAC prefix, or a different camera altogether.
    # We refuse: the old offline row stays stranded (user can delete
    # via Camera Setup).
    cam = make_camera(
        mac_address="aa:bb:cc:dd:ee:01",
        manufacturer="Hikvision",
        model="DS-2CD2",
        username="admin",
        password="hunter2",
        hardware_id="HWID-ORIGINAL",
        serial_number="SN-ORIGINAL",
    )
    signals = make_signals(
        mac="aa:bb:cc:dd:ee:01",
        scope_hardware="DS-2CD2",  # 1 corroboration
    )
    auth = AuthRecorder(
        result=SimpleNamespace(
            hardware_id="HWID-DIFFERENT", serial_number="SN-ORIGINAL",
        )
    )

    result = _run(reconcile(signals, [cam], auth))

    assert result is None
    assert len(auth.calls) == 1  # verify-auth was attempted before refusal


# ---------------------------------------------------------------------------
# Case 7: MAC match, 0 corroborations → None, 0 auth
# ---------------------------------------------------------------------------


def test_case_07_mac_alone_zero_corroborations_refuses():
    # Modern phones/IoT randomize MACs. A bare MAC match with zero
    # other evidence is not enough to reconcile — and specifically
    # must NOT trigger a speculative auth call to "figure it out".
    cam = make_camera(
        mac_address="aa:bb:cc:dd:ee:01",
        username="admin",
        password="hunter2",
        hardware_id="HWID-1234",
    )
    signals = make_signals(mac="aa:bb:cc:dd:ee:01")  # no other signals
    auth = AuthRecorder()

    result = _run(reconcile(signals, [cam], auth))

    assert result is None
    assert auth.calls == []


# ---------------------------------------------------------------------------
# Case 8: MAC match on two candidates → None, 0 auth
# ---------------------------------------------------------------------------


def test_case_08_mac_duplicate_refuses():
    # Shared MAC across two rows (clone hardware, VM bridging, or
    # OEM collision). Ambiguity policy refuses to pick a winner.
    cam1 = make_camera(id="cam-1", mac_address="aa:bb:cc:dd:ee:01")
    cam2 = make_camera(
        id="cam-2", ip="10.0.0.6", mac_address="aa:bb:cc:dd:ee:01",
    )
    signals = make_signals(
        mac="aa:bb:cc:dd:ee:01", mac_brand="Hikvision",
    )
    auth = AuthRecorder()

    result = _run(reconcile(signals, [cam1, cam2], auth))

    assert result is None
    assert auth.calls == []


# ---------------------------------------------------------------------------
# Case 9: Alt-MAC match, verify-auth passes → MAC_MODERATE
# ---------------------------------------------------------------------------


def test_case_09_alt_mac_verify_auth_pass():
    # Dual-NIC camera: wired MAC is the stored primary; the camera
    # rebound on its wireless NIC, whose MAC sits in alt_macs.
    cam = make_camera(
        mac_address="aa:bb:cc:dd:ee:01",
        alt_macs=["aa:bb:cc:dd:ee:02"],
        username="admin",
        password="hunter2",
        hardware_id="HWID-1234",
        serial_number="SN-5678",
    )
    signals = make_signals(mac="aa:bb:cc:dd:ee:02")
    auth = AuthRecorder(
        result=SimpleNamespace(
            hardware_id="HWID-1234", serial_number="SN-5678",
        )
    )

    result = _run(reconcile(signals, [cam], auth))

    assert result is not None
    candidate, confidence, evidence = result
    assert confidence is MatchConfidence.MAC_MODERATE
    assert evidence[0] == "alt_mac"
    assert "verify_auth" in evidence
    assert len(auth.calls) == 1


# ---------------------------------------------------------------------------
# Case 10: Alt-MAC match but no stored creds → None, 0 auth
# ---------------------------------------------------------------------------


def test_case_10_alt_mac_no_stored_creds_refuses_without_auth():
    # A camera that was added manually without credentials, or whose
    # auth was cleared, can't be verified. The verify-auth gate
    # catches this BEFORE calling interrogate_fn — no speculative auth.
    cam = make_camera(
        mac_address="aa:bb:cc:dd:ee:01",
        alt_macs=["aa:bb:cc:dd:ee:02"],
        username=None,
        password=None,
        hardware_id="HWID-1234",
    )
    signals = make_signals(mac="aa:bb:cc:dd:ee:02")
    auth = AuthRecorder()

    result = _run(reconcile(signals, [cam], auth))

    assert result is None
    assert auth.calls == []


# ---------------------------------------------------------------------------
# Case 11: MAC_MODERATE reached in RTSP-only branch (xaddr=None) → None
# ---------------------------------------------------------------------------


def test_case_11_rtsp_only_branch_no_xaddr_refuses_mac_moderate():
    # Tapo/Eufy path: no ONVIF xaddr, so GetDeviceInformation is
    # unreachable. MAC_MODERATE can't be verified → fall through.
    # MAC_HIGH would still work in this branch — only MAC_MODERATE
    # is gated by xaddr availability.
    cam = make_camera(
        mac_address="aa:bb:cc:dd:ee:01",
        model="C200",
        username="admin",
        password="hunter2",
        hardware_id="HWID-1234",
    )
    signals = make_signals(
        xaddr=None,
        mac="aa:bb:cc:dd:ee:01",
        scope_hardware="C200",  # 1 corroboration → MAC_MODERATE
    )
    auth = AuthRecorder()

    result = _run(reconcile(signals, [cam], auth))

    assert result is None
    assert auth.calls == []


# ---------------------------------------------------------------------------
# Case 12: No EPR, no MAC → None, 0 auth
# ---------------------------------------------------------------------------


def test_case_12_no_signals_refuses():
    # New endpoint on a network the scanner can't reach ARP for, and
    # no WS-Discovery ProbeMatch either (manual add style). Treat as
    # a brand-new device with no auth probing.
    cam = make_camera(
        mac_address="aa:bb:cc:dd:ee:01",
        endpoint_reference="urn:uuid:abc",
        username="admin",
        password="hunter2",
        hardware_id="HWID-1234",
    )
    signals = make_signals()  # all None
    auth = AuthRecorder()

    result = _run(reconcile(signals, [cam], auth))

    assert result is None
    assert auth.calls == []


# ---------------------------------------------------------------------------
# Case 13: EPR match but empty offline-candidate list → None, 0 auth
# ---------------------------------------------------------------------------


def test_case_13_empty_candidate_list_refuses():
    # First-boot path or fresh-install: nobody to reconcile against.
    # The function must not crash on the empty list and must not
    # fall back to any global auth stuffing.
    signals = make_signals(
        endpoint_reference="urn:uuid:abc",
        mac="aa:bb:cc:dd:ee:01",
        mac_brand="Hikvision",
    )
    auth = AuthRecorder()

    result = _run(reconcile(signals, [], auth))

    assert result is None
    assert auth.calls == []


# ---------------------------------------------------------------------------
# Pure-narrow smoke: confidence ladder doesn't secretly skip EPR when
# MAC also matches. EPR is the strongest signal and must win short.
# ---------------------------------------------------------------------------

def test_epr_takes_precedence_over_mac_path_when_both_would_match():
    cam = make_camera(
        endpoint_reference="urn:uuid:abc",
        mac_address="aa:bb:cc:dd:ee:01",
        manufacturer="Hikvision",
    )
    signals = make_signals(
        endpoint_reference="urn:uuid:abc",
        mac="aa:bb:cc:dd:ee:01",
        mac_brand="Hikvision",
        scope_hardware="DS-2CD2",  # would give MAC_HIGH via mac path
    )
    r = narrow(signals, [cam])
    assert r.confidence is MatchConfidence.EPR_EXACT
    assert r.evidence == ["endpoint_reference"]
