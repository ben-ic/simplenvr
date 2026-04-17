"""Tiered camera reconciliation for DHCP lease changes.

Given an endpoint discovered at a new IP, decide whether it's actually
an existing camera that rebound to a new address. The load-bearing
rule (see `plans/mac-fallback-ip-change-plan.md` §Architecture):

    Auth is a verifier, not a matcher.

The scanner narrows offline candidates using unauthenticated signals
only (EndpointReference, MAC / alt_macs, ONVIF scopes, RTSP banner,
MAC OUI vs stored manufacturer). A single prime suspect at moderate
confidence triggers ONE authenticated GetDeviceInformation call to
verify HardwareId + SerialNumber. Credentials are never iterated
across candidates — a camera whose identity can't be narrowed by
unauthenticated signals is treated as new, not brute-force-identified
by trying every stored credential against it.

This module is pure except for the caller-supplied `interrogate_fn`.
All I/O (DB reads, ARP lookups, ONVIF service calls) happens in the
scanner; reconcile() just sequences the decisions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Awaitable, Callable

if TYPE_CHECKING:
    from ..models import Camera
    from .onvif_client import CameraInfo


class MatchConfidence(Enum):
    # Exact EndpointReference match on a singleton offline candidate
    # whose stored manufacturer is OUI-compatible. Strongest unauth
    # signal we have; reconcile without any auth call.
    EPR_EXACT = "epr_exact"
    # Primary-MAC match on a singleton candidate with >=2 corroborating
    # unauth signals (scope hardware/name, RTSP banner, MAC OUI brand).
    # Enough evidence to reconcile without verification.
    MAC_HIGH = "mac_high"
    # Primary-MAC match with exactly 1 corroboration, OR any alt_mac
    # match. Needs a single verify-auth GetDeviceInformation call
    # against the stored credentials to confirm HardwareId + Serial.
    MAC_MODERATE = "mac_moderate"
    # No signal, multiple tied matches (ambiguous — refuse to guess),
    # or MAC-alone with zero corroboration. Treat the endpoint as a
    # new camera. Never auth against anything.
    NONE = "none"


@dataclass
class NarrowSignals:
    """Unauthenticated signals harvested at the new IP.

    Built by the scanner from the freshly discovered endpoint:
    `xaddr` / `endpoint_reference` from WS-Discovery ProbeMatch;
    `mac` from the ARP table at `new_ip`; `mac_brand` from
    `lookup_manufacturer_by_mac(mac)`; `scope_hardware` /
    `scope_name` from `parse_scopes(ep.scopes)`; `rtsp_server_banner`
    from an optional RTSP OPTIONS probe (may be None).

    `xaddr=None` signals the RTSP-only discovery branch (Tapo/Eufy):
    in that branch EPR and verify-auth are unavailable, so only
    MAC_HIGH can succeed. MAC_MODERATE will fall through to NONE for
    lack of an xaddr to run GetDeviceInformation against.
    """
    new_ip: str
    xaddr: str | None
    endpoint_reference: str | None
    mac: str | None
    mac_brand: str | None
    scope_hardware: str | None
    scope_name: str | None
    rtsp_server_banner: str | None


@dataclass
class NarrowResult:
    candidate: "Camera | None" = None
    confidence: MatchConfidence = MatchConfidence.NONE
    evidence: list[str] = field(default_factory=list)


def _brands_compatible(sig_brand: str | None, stored: str | None) -> bool:
    """Brand-compatibility check with a permissive 'unknown' rule.

    OUI lookup returning None and a camera row with no stored
    manufacturer are both common (budget cameras, manual adds) and
    shouldn't block a reconcile. Only a *known* disagreement blocks.
    """
    if sig_brand is None or stored is None:
        return True
    return sig_brand.lower() == stored.lower()


def _collect_corroborations(
    candidate: "Camera", signals: NarrowSignals
) -> list[str]:
    """Count unauthenticated signals that corroborate a MAC match.

    Each entry in the returned list is a short evidence tag that ends
    up in the `Reconciled` log line — so a human reading the log can
    see *why* the scanner decided this was the same camera.
    """
    out: list[str] = []
    if signals.scope_hardware and candidate.model == signals.scope_hardware:
        out.append("scope_hardware")
    if signals.scope_name and candidate.name == signals.scope_name:
        out.append("scope_name")
    if (
        signals.rtsp_server_banner
        and getattr(candidate, "rtsp_server_banner", None)
        and signals.rtsp_server_banner == getattr(candidate, "rtsp_server_banner", None)
    ):
        out.append("rtsp_banner")
    if (
        signals.mac_brand
        and candidate.manufacturer
        and _brands_compatible(signals.mac_brand, candidate.manufacturer)
    ):
        out.append("mac_brand")
    return out


def narrow(
    signals: NarrowSignals,
    offline_candidates: list["Camera"],
) -> NarrowResult:
    """Pure decision function: who (if anyone) is this endpoint?

    Runs the EPR path first (strongest signal), then the MAC / alt_mac
    path. Any ambiguity (>1 matching candidate) short-circuits to
    `NONE` rather than guessing — a stranded offline row is strictly
    less harmful than a silent mis-reconcile that orphans recordings
    under the wrong camera id.
    """
    # --- EPR path ---
    if signals.endpoint_reference:
        hits = [
            c for c in offline_candidates
            if c.endpoint_reference == signals.endpoint_reference
            and _brands_compatible(signals.mac_brand, c.manufacturer)
        ]
        if len(hits) == 1:
            return NarrowResult(
                hits[0], MatchConfidence.EPR_EXACT, ["endpoint_reference"],
            )
        if len(hits) > 1:
            return NarrowResult()  # ambiguous; refuse

    # --- MAC / alt_mac path ---
    if signals.mac:
        hits = [
            c for c in offline_candidates
            if c.mac_address == signals.mac
            or signals.mac in (c.alt_macs or [])
        ]
        if len(hits) > 1:
            return NarrowResult()  # duplicate MAC; refuse
        if len(hits) == 1:
            candidate = hits[0]
            is_alt = candidate.mac_address != signals.mac
            corroborations = _collect_corroborations(candidate, signals)
            evidence = [("alt_mac" if is_alt else "mac")] + corroborations
            # Primary-MAC + >=2 corroborations: enough without auth.
            if len(corroborations) >= 2 and not is_alt:
                return NarrowResult(
                    candidate, MatchConfidence.MAC_HIGH, evidence,
                )
            # Primary-MAC + 1 corroboration, or any alt_mac match: moderate.
            if len(corroborations) >= 1 or is_alt:
                return NarrowResult(
                    candidate, MatchConfidence.MAC_MODERATE, evidence,
                )
            # MAC alone with zero corroboration is not enough — too many
            # devices randomize MACs these days (phones, IoT) to trust a
            # bare MAC match.

    return NarrowResult()


async def reconcile(
    signals: NarrowSignals,
    offline_candidates: list["Camera"],
    interrogate_fn: Callable[
        [str, str, str, str], Awaitable["CameraInfo | None"]
    ],
) -> tuple["Camera", MatchConfidence, list[str]] | None:
    """Return (candidate, confidence, evidence) on a confirmed match.

    At MAC_MODERATE the function makes at most one authenticated
    GetDeviceInformation call — against the singleton prime suspect,
    using that candidate's stored credentials. Auth is never iterated
    across candidates and never attempted speculatively outside this
    branch.

    Returns None when:
      - narrow() produced no unambiguous candidate
      - MAC_MODERATE path is unavailable (no stored creds, no
        stored hardware_id, or no xaddr on this discovery branch)
      - the verify-auth call failed (creds bad, device unreachable)
      - HardwareId / SerialNumber didn't match (hardware replaced
        at the same network position — treat as a new camera)
    """
    r = narrow(signals, offline_candidates)
    if r.candidate is None:
        return None

    if r.confidence in (MatchConfidence.EPR_EXACT, MatchConfidence.MAC_HIGH):
        return (r.candidate, r.confidence, r.evidence)

    if r.confidence == MatchConfidence.MAC_MODERATE:
        c = r.candidate
        # Verify-auth requires all four: stored creds + stored
        # hardware_id + an xaddr at the new IP. Missing any one means
        # we can't prove identity, so we downgrade silently to "new
        # camera". Speculative auth against a candidate we can't
        # verify against would violate the §Architecture rule.
        if not (c.username and c.password and c.hardware_id and signals.xaddr):
            return None

        info = await interrogate_fn(
            signals.new_ip, signals.xaddr, c.username, c.password,
        )
        if info is None:
            return None  # auth failed or device unreachable
        if (
            info.hardware_id == c.hardware_id
            and info.serial_number == c.serial_number
        ):
            return (c, r.confidence, r.evidence + ["verify_auth"])
        # Matched IP + MAC but different hardware — factory swap,
        # clone device, or a different camera at the same bay. Treat
        # as new. The old offline row stays stranded; the user can
        # delete it through Camera Setup.
        return None

    return None
