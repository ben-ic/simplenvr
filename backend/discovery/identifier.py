"""
Camera brand identifier — multi-signal fingerprint scoring.

Takes every unauthenticated signal we could gather about a device on the
LAN (reverse DNS hostname, MAC address, ONVIF scopes, HTTP server/title,
RTSP URL path), scores each fingerprint entry in fingerprints.py by how
many independent signals match, and returns the highest-confidence match
along with an optional second-best for tie-breaking.

Design goals:

  1. **Multi-signal, not single-signal.** No single signal is safe alone:
     MAC OUIs can be wrong (OEM rebadges), hostnames can be generic
     ("unknown"), HTTP probes can fail, ONVIF scopes can be empty. But
     the union of signals almost always produces an unambiguous match.

  2. **Declared brands boost, don't filter.** If the user told us during
     onboarding that they own Reolinks, that's a hint — a camera
     matching Reolink gets a confidence boost. But if a DIFFERENT
     camera's signals clearly say it's a Tapo, we identify it as a Tapo
     regardless of the declaration. Users forget what they own.

  3. **Transparent scoring.** The caller can inspect how each signal
     contributed so we can debug mis-identifications. The return value
     is a structured result, not just a brand string.

  4. **No side effects.** Pure function of inputs → scored result. No
     I/O, no caching, no state. The caller (scanner.py) is responsible
     for gathering the inputs.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from .fingerprints import FINGERPRINTS, CameraFingerprint

logger = logging.getLogger(__name__)


# ── Scoring weights ─────────────────────────────────────────────────
#
# Each signal gets a weight reflecting how reliable that signal is for
# brand identification. MAC OUI is high-confidence (IEEE registry,
# brand-owned); hostname patterns are high-confidence when they match
# because they're firmware-baked; HTTP headers are medium (can be
# stripped or proxied); RTSP path patterns are medium (shared across
# OEM rebadges). Declared-brand hint is an additive boost, not a
# multiplier, so a confident non-declared match still wins over a
# weak declared match.

_W_MAC_OUI = 10
_W_HOSTNAME = 10
_W_ONVIF_SCOPE = 8
_W_HTTP_SERVER = 6
_W_HTTP_TITLE = 6
_W_RTSP_PATH = 4
_W_DECLARED_BRAND_BOOST = 5


# ── Input struct ────────────────────────────────────────────────────

@dataclass(frozen=True)
class IdentifySignals:
    """
    All available unauthenticated signals for one candidate device.

    Fields are optional so callers can pass whatever they have; the
    scorer simply skips signals that aren't present. This makes the
    scorer resilient to partial data (e.g. a camera that responds to
    ONVIF but refuses HTTP probes).
    """
    ip: str
    mac_address: str | None = None
    hostname: str | None = None
    onvif_scopes: tuple[str, ...] = ()
    http_server: str | None = None
    http_title: str | None = None
    rtsp_path: str | None = None
    # The brand name already normalized by mac_lookup.lookup_manufacturer
    # (which goes through the IEEE registry and the corporate-name alias
    # table). This is the single authoritative MAC→brand signal — the
    # identifier should NOT re-do alias logic here. If this is "Tapo"
    # and a fingerprint's brand is "TP-Link Tapo", that's a strong
    # match even if a raw substring check would miss it.
    normalized_mac_brand: str | None = None


# ── Output struct ───────────────────────────────────────────────────

@dataclass
class SignalMatch:
    """Which signals contributed to a fingerprint's score and by how much."""
    signal: str                  # "mac_oui", "hostname", "onvif_scope", etc.
    weight: int                  # points contributed
    detail: str = ""             # human-readable explanation


@dataclass
class IdentifyResult:
    """The highest-scoring fingerprint and why it won."""
    brand: str                   # The identified brand (display name)
    device_type: str             # "camera" | "hub" | "hub_camera"
    confidence: int              # Sum of matched signal weights
    matches: list[SignalMatch] = field(default_factory=list)
    # Runner-up for ambiguity detection. If the winner and runner-up
    # are close in score, the caller may want to show "possibly
    # Reolink or Hikvision" rather than committing.
    runner_up_brand: str | None = None
    runner_up_confidence: int = 0
    # Raw fingerprint reference so callers can access notes,
    # default_credentials, rtsp_example_paths, etc.
    fingerprint: CameraFingerprint | None = None


# ── Scoring ─────────────────────────────────────────────────────────

def _normalize(s: str | None) -> str:
    """Lowercase + strip for case-insensitive substring matching."""
    return s.lower().strip() if s else ""


def _score_fingerprint(
    fp: CameraFingerprint,
    signals: IdentifySignals,
    declared_brands: frozenset[str],
) -> tuple[int, list[SignalMatch]]:
    """
    Score a single fingerprint against the available signals.

    Returns (total_score, list_of_matches). A zero score means nothing
    matched and this fingerprint is not a candidate.
    """
    matches: list[SignalMatch] = []
    score = 0

    # 1. MAC OUI — two independent sub-signals that can both contribute:
    #
    #    (a) The MAC's OUI prefix (first 3 bytes) is in the fingerprint's
    #        curated mac_ouis set. Tight match, high confidence.
    #
    #    (b) The normalized brand name from mac_lookup.lookup_manufacturer
    #        (which ran the MAC through the IEEE registry + corporate alias
    #        table) matches this fingerprint's brand. This path catches
    #        OUI prefixes that aren't in the curated fingerprint's mac_ouis
    #        set but ARE in the IEEE registry under a known alias — i.e.,
    #        it gives us IEEE-wide coverage without requiring the
    #        fingerprint data to be perfect.
    #
    # Both can fire for the same device and each contributes independently.
    if signals.mac_address:
        mac_prefix = ":".join(signals.mac_address.lower().split(":")[:3])
        if mac_prefix in fp.mac_ouis:
            score += _W_MAC_OUI
            matches.append(
                SignalMatch(
                    "mac_oui_curated",
                    _W_MAC_OUI,
                    f"MAC OUI {mac_prefix} is in {fp.brand}'s known prefixes",
                )
            )

    if signals.normalized_mac_brand:
        # Case-insensitive: "Tapo" matches "TP-Link Tapo", "Reolink"
        # matches "Reolink", "Hanwha" matches "Hanwha Vision (Wisenet)".
        # This is a substring match on the normalized brand string INSIDE
        # the fingerprint brand, which works cleanly for every case where
        # the mac_lookup alias produces the user-facing short name.
        norm = signals.normalized_mac_brand.lower()
        brand = fp.brand.lower()
        if norm and (norm in brand or brand in norm):
            score += _W_MAC_OUI
            matches.append(
                SignalMatch(
                    "mac_oui_ieee",
                    _W_MAC_OUI,
                    f"IEEE→alias brand '{signals.normalized_mac_brand}' matches '{fp.brand}'",
                )
            )

    # 2. Hostname pattern. The fingerprint defines one or more regex
    #    patterns the DHCP hostname might match. These are firmware-
    #    baked signals and very high confidence when they hit.
    if signals.hostname and fp.hostname_patterns:
        host_lower = signals.hostname.lower()
        for pattern in fp.hostname_patterns:
            try:
                if re.search(pattern, host_lower):
                    score += _W_HOSTNAME
                    matches.append(
                        SignalMatch(
                            "hostname",
                            _W_HOSTNAME,
                            f"Hostname '{signals.hostname}' matches pattern /{pattern}/",
                        )
                    )
                    break
            except re.error:
                logger.warning(
                    "Invalid hostname regex in fingerprint %s: %r", fp.brand, pattern
                )
                continue

    # 3. ONVIF WS-Discovery scope patterns. The scopes field in an
    #    unauthenticated ProbeMatch response often contains brand
    #    hints like "onvif://www.onvif.org/hardware/RLC-410".
    if signals.onvif_scopes and fp.onvif_scope_patterns:
        scopes_joined = " ".join(signals.onvif_scopes).lower()
        for pattern in fp.onvif_scope_patterns:
            try:
                if re.search(pattern, scopes_joined, re.IGNORECASE):
                    score += _W_ONVIF_SCOPE
                    matches.append(
                        SignalMatch(
                            "onvif_scope",
                            _W_ONVIF_SCOPE,
                            f"ONVIF scope matches /{pattern}/",
                        )
                    )
                    break
            except re.error:
                continue

    # 4. HTTP server header.
    if signals.http_server and fp.http_server_substrings:
        server_lower = _normalize(signals.http_server)
        for needle in fp.http_server_substrings:
            if needle.lower() in server_lower:
                score += _W_HTTP_SERVER
                matches.append(
                    SignalMatch(
                        "http_server",
                        _W_HTTP_SERVER,
                        f"HTTP Server header contains '{needle}'",
                    )
                )
                break

    # 5. HTTP title.
    if signals.http_title and fp.http_title_substrings:
        title_lower = _normalize(signals.http_title)
        for needle in fp.http_title_substrings:
            if needle.lower() in title_lower:
                score += _W_HTTP_TITLE
                matches.append(
                    SignalMatch(
                        "http_title",
                        _W_HTTP_TITLE,
                        f"HTTP title contains '{needle}'",
                    )
                )
                break

    # 6. RTSP path pattern.
    if signals.rtsp_path and fp.rtsp_path_patterns:
        path_lower = _normalize(signals.rtsp_path)
        for pattern in fp.rtsp_path_patterns:
            try:
                if re.search(pattern, path_lower):
                    score += _W_RTSP_PATH
                    matches.append(
                        SignalMatch(
                            "rtsp_path",
                            _W_RTSP_PATH,
                            f"RTSP path matches /{pattern}/",
                        )
                    )
                    break
            except re.error:
                continue

    # 7. Declared-brand boost. If the user told us during onboarding
    #    they own this brand, bump the score by a small additive
    #    amount. Never enough to overturn a strong match for a
    #    different brand, but enough to break ties in the declared
    #    brand's favor.
    if score > 0 and declared_brands and fp.brand in declared_brands:
        score += _W_DECLARED_BRAND_BOOST
        matches.append(
            SignalMatch(
                "declared_brand",
                _W_DECLARED_BRAND_BOOST,
                f"User declared {fp.brand} during onboarding",
            )
        )

    return score, matches


def identify(
    signals: IdentifySignals,
    declared_brands: list[str] | None = None,
) -> IdentifyResult | None:
    """
    Score every fingerprint against the signals and return the best match.

    Returns None if no fingerprint scored above zero — meaning the
    device's signals don't match any brand in the database. Caller
    should surface this as "Unknown camera — <ieee_manufacturer>" if
    ieee_manufacturer is available, or plain "Unknown" otherwise.
    """
    declared_set = frozenset(declared_brands or [])

    scored: list[tuple[int, CameraFingerprint, list[SignalMatch]]] = []
    for fp in FINGERPRINTS:
        score, matches = _score_fingerprint(fp, signals, declared_set)
        if score > 0:
            scored.append((score, fp, matches))

    if not scored:
        return None

    # Sort by score descending; tie-break by tier (lower tier wins,
    # since consumer brands are more likely than niche ones on a home LAN).
    scored.sort(key=lambda x: (-x[0], x[1].tier))
    winner_score, winner_fp, winner_matches = scored[0]

    runner_up_brand = None
    runner_up_score = 0
    if len(scored) > 1:
        runner_up_score, runner_up_fp, _ = scored[1]
        runner_up_brand = runner_up_fp.brand

    return IdentifyResult(
        brand=winner_fp.brand,
        device_type=winner_fp.device_type,
        confidence=winner_score,
        matches=winner_matches,
        runner_up_brand=runner_up_brand,
        runner_up_confidence=runner_up_score,
        fingerprint=winner_fp,
    )
