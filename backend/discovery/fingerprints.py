"""
Camera fingerprint database for SimpleNVR unauthenticated discovery.

Purpose
-------
When SimpleNVR scans a LAN for IP cameras, it has only network-level signals
to work with: reverse DNS hostname, MAC OUI, ONVIF WS-Discovery scopes,
HTTP server header, HTML <title> of the (unauthenticated) login page,
RTSP URL path, and mDNS service advertisements.

This module maps those signals back to a brand (and where possible a model
family) so the setup UI can show "Reolink RLC-410 at 10.0.0.13" instead of
"Unknown camera at 10.0.0.13".

Scoring intent
--------------
The discovery layer should score each candidate fingerprint by how many
independent signals match. A MAC OUI hit alone is weak (OEM rebadging is
rampant in the camera industry — Lorex/Amcrest/Annke all share silicon
with Dahua or Hikvision). A MAC OUI hit + an ONVIF scope hit + an HTTP
title hit is strong. The caller should sum match weights and pick the
highest, with ties broken by tier (consumer brands are statistically more
likely on a home LAN than Bosch).

Device types
------------
Most entries are device_type="camera" (a direct IP camera that speaks
RTSP/ONVIF on its own LAN address). Some entries are device_type="hub":
gateway boxes (Eufy HomeBase, Reolink Home Hub, Arlo SmartHub) that have
their own LAN presence and expose cameras-behind-them via RTSP paths on
the hub's address. The discovery layer should treat hubs differently:
adopting a hub means walking its child-camera list, not adopting the hub
itself as a stream.

Caveats
-------
- OUI lists are NOT exhaustive. IEEE assigns new blocks regularly and
  brands buy them opportunistically. Treat the OUI sets as "known good"
  not "complete".
- Several brands (Lorex, Annke, Amcrest, Ezviz) are OEM rebadges of
  Dahua/Hikvision and will share fingerprints with their parent.
- Cloud-only brands (Ring, Nest, Arlo, stock Wyze) have supports_local_rtsp=False.
  SimpleNVR cannot ingest them; the UI should show a friendly "not supported" hint.
- Default credentials are PUBLIC information published in user manuals and
  on ipvm.com. They are included here so the setup wizard can pre-fill the
  "try default password" field.
- logo_source_url points at a clean vector source (Wikimedia Commons SVG
  preferred). The frontend build pipeline fetches these separately into
  bundled assets; they are NOT runtime-loaded.
"""

from dataclasses import dataclass, field
from typing import Union


# ─── Signal tier ─────────────────────────────────────────────────────
#
# Every signal in a fingerprint is either ANCHORED or SUPPORTING.
#
#   Anchored  — the signal is brand-bearing on its own. Examples:
#               a hostname containing the brand literal (^axis-),
#               an ONVIF scope of the form name/<Brand>, an HTTP
#               server header containing the brand name verbatim,
#               or an RTSP path that's brand-specific (/axis-media/).
#               IEEE MAC OUI matches are implicitly anchored because
#               IEEE is an authoritative external registry.
#
#   Supporting — the signal is consistent with the brand but also
#               consistent with many other devices. Examples: a
#               generic embedded webserver (Boa, lighttpd), the
#               generic ONVIF Profile S scope, or a model-only
#               hostname pattern (^C\d{3}, ^Camera\d*$). These
#               cannot win on their own because they would award
#               phantom points to any cheap unauthenticated cam.
#
# The scorer in identifier.py applies a hard rule:
#   "A fingerprint scores 0 unless at least one anchored signal
#    matches. Supporting signals only contribute their points after
#    that anchor gate has been satisfied."
#
# This makes it structurally impossible for a camera to be mis-
# identified from generic signals alone, regardless of what someone
# adds to the fingerprint database in the future. The 2026-04-08
# audit removed 9 generic strings that had been hand-added to
# brand-specific fingerprints by an earlier research-agent run; this
# tier system prevents that class of bug from regressing.
#
# For ergonomics, fingerprint definitions can write a bare string
# instead of FingerprintSignal — bare strings default to anchored,
# matching how every existing fingerprint signal behaves today after
# the 2026-04-08 cleanup. Only the handful of generic-shape patterns
# need an explicit Supporting() wrapper.

@dataclass(frozen=True)
class FingerprintSignal:
    """A single signal pattern with its anchor tier."""
    pattern: str
    anchored: bool = True


def Anchored(pattern: str) -> FingerprintSignal:
    """Mark a signal as brand-bearing — sufficient evidence on its own."""
    return FingerprintSignal(pattern=pattern, anchored=True)


def Supporting(pattern: str) -> FingerprintSignal:
    """Mark a signal as consistent-but-not-exclusive — only counts when
    an anchored signal in the same fingerprint also matches."""
    return FingerprintSignal(pattern=pattern, anchored=False)


# Either a bare string (defaults to Anchored) or an explicit
# FingerprintSignal — both are accepted in fingerprint definitions.
SignalEntry = Union[str, FingerprintSignal]


@dataclass(frozen=True)
class CameraFingerprint:
    brand: str                                          # Display name shown to user
    tier: int                                           # 1 = consumer, 2 = prosumer, 3 = niche
    device_type: str = "camera"                         # "camera" or "hub"
    hostname_patterns: tuple[SignalEntry, ...] = ()     # regex patterns
    hostname_examples: tuple[str, ...] = ()
    onvif_scope_patterns: tuple[SignalEntry, ...] = ()  # regex fragments matched against Scopes field
    http_server_substrings: tuple[SignalEntry, ...] = () # case-insensitive substrings of Server: header
    http_title_substrings: tuple[SignalEntry, ...] = ()  # case-insensitive substrings of <title>
    rtsp_path_patterns: tuple[SignalEntry, ...] = ()    # regex of URL path after host:port
    rtsp_example_paths: tuple[str, ...] = ()
    mdns_service_types: tuple[str, ...] = ()
    default_credentials: tuple[tuple[str, str], ...] = ()
    supports_local_rtsp: bool = True
    notes: str = ""
    logo_source_url: str = ""                           # URL to vector logo (SVG preferred)
    sources: tuple[str, ...] = ()

FINGERPRINTS: list[CameraFingerprint] = [

    # ---------------------------------------------------------------------
    # TIER 1 — HOME CONSUMER
    # ---------------------------------------------------------------------

    CameraFingerprint(
        brand="Reolink",
        tier=1,
        # Reolink's parent company is Baichuan Digital Technology; their cameras
        # have historically registered DHCP hostnames containing "baichuan",
        # "Camera", or the model number itself (e.g. "RLC-410").
        hostname_patterns=(
            r"(?i)^baichuan",
            # `^Camera\d*$` is shape-only — many generic OEM cams ship
            # with the literal hostname "Camera" or "Camera1". Marked
            # Supporting so it can boost a real Reolink match (via
            # MAC OUI / ONVIF name / RLC- hostname / Reolink hostname)
            # but cannot win on its own.
            Supporting(r"(?i)^Camera\d*$"),
            r"(?i)^RLC-",
            r"(?i)^E1-",
            r"(?i)^Reolink",
        ),
        hostname_examples=("baichuan", "Camera1", "RLC-410-5MP"),
        onvif_scope_patterns=(
            r"onvif://www\.onvif\.org/name/Reolink",
            r"onvif://www\.onvif\.org/hardware/RLC",
        ),
        # Only brand-specific server strings. "webserver" is generic
        # (cheap OEM cams use it verbatim) — removed to avoid false
        # positives where any camera with a stubby Server header got
        # +6 Reolink points.
        http_server_substrings=("Reolink",),
        http_title_substrings=("Reolink",),
        rtsp_path_patterns=(
            r"^/h26[45]Preview_\d+_(main|sub)$",
            r"^/Preview_\d+_(main|sub)$",
        ),
        rtsp_example_paths=(
            "/h264Preview_01_main",
            "/h264Preview_01_sub",
            "/h265Preview_01_main",
        ),
        default_credentials=(("admin", ""),),  # Reolink ships with blank password; user sets on first login
        notes="Reolink supports ONVIF and RTSP out of the box on wired models. "
              "Battery models (Argus, Go) gate RTSP behind their app and may not "
              "expose it at all. The PoE/wired RLC line is the sweet spot.",
        logo_source_url="https://commons.wikimedia.org/wiki/File:Reolink_logo.svg",
        sources=(
            "https://support.reolink.com/hc/en-us/articles/360007010473",
            "https://oui.ieee.org/",
        ),
    ),

    CameraFingerprint(
        brand="Eufy",
        tier=1,
        # Eufy product codes start with T8xxx; the camera SKU is often visible
        # on the device label and in DHCP. eufyCam 2C is T8113, 2 is T8114,
        # eufyCam 3 is T8160, indoor cam 2K is T8410, etc.
        hostname_patterns=(
            r"(?i)^T8\d{3}",
            r"(?i)^eufy",
        ),
        hostname_examples=("T8410", "T8113", "eufyCam"),
        # Eufy generally does NOT broadcast ONVIF; they speak their own P2P protocol
        # over the HomeBase. RTSP is opt-in per camera.
        rtsp_path_patterns=(
            # `/live0`, `/live1`, etc. is the Eufy HomeBase convention
            # but it's also used by dozens of generic Chinese OEM cams.
            # Supporting so it boosts a real Eufy match (MAC OUI / Eufy
            # hostname / T8\d{3} model code) but can't win on its own.
            Supporting(r"^/live\d*$"),
        ),
        rtsp_example_paths=("/live0",),
        default_credentials=(),  # Eufy uses cloud-account auth, not per-camera passwords
        notes="Eufy cameras require a HomeBase hub to enable RTSP. RTSP must be "
              "manually enabled per-camera in the Eufy Security app under "
              "Device Settings -> Network -> RTSP Stream. The stream URL is then "
              "rtsp://<homebase-ip>:554/live0 served from the HomeBase, NOT the "
              "camera itself. Battery cameras have wake-on-motion behavior that "
              "breaks 24/7 recording even with RTSP enabled.",
        logo_source_url="https://commons.wikimedia.org/wiki/File:Eufy_logo.svg",
        sources=(
            "https://support.eufylife.com/s/article/How-to-Use-RTSP-on-Eufy-Cameras",
        ),
    ),

    CameraFingerprint(
        brand="TP-Link Tapo",
        tier=1,
        hostname_patterns=(
            r"(?i)^Tapo",
            # `^C\d{3}` is shape-only — matches Tapo C100/C200/C310 but
            # also matches any "C100" / "C200" device that happens to
            # use that DHCP hostname (cheap OEM cams, USB devices, etc.)
            # Supporting so it boosts a real Tapo match (MAC OUI /
            # name/Tapo scope / Tapo hostname / Tapo title) but can't
            # win on its own.
            Supporting(r"(?i)^C\d{3}"),   # C100, C200, C310, C320WS etc.
        ),
        hostname_examples=("Tapo_Cam_1A2B", "C200"),
        onvif_scope_patterns=(
            r"onvif://www\.onvif\.org/name/Tapo",
            r"onvif://www\.onvif\.org/name/TP-LINK",
        ),
        http_title_substrings=("Tapo",),
        rtsp_path_patterns=(
            # `/stream1` and `/stream2` are Tapo's convention but also
            # used by other OEMs (Anker, generic chipsets). Supporting
            # so a real Tapo match wins via brand-bearing signals.
            Supporting(r"^/stream[12]$"),
        ),
        rtsp_example_paths=("/stream1", "/stream2"),
        default_credentials=(),  # Tapo requires user to set RTSP creds in app on first run
        notes="Tapo C-series cameras require enabling 'Camera Account' in the Tapo "
              "app (Advanced Settings -> Camera Account) before RTSP works. The "
              "username and password are user-chosen, not factory defaults.",
        logo_source_url="https://commons.wikimedia.org/wiki/File:Tapo_logo.svg",
        sources=(
            "https://www.tp-link.com/us/support/faq/2680/",
        ),
    ),

    CameraFingerprint(
        brand="TP-Link Kasa",
        tier=1,
        hostname_patterns=(
            r"(?i)^KC\d{3}",   # KC100, KC120, KC200, KC400
            r"(?i)^Kasa",
        ),
        hostname_examples=("KC120", "Kasa_Cam_4F2A"),
        supports_local_rtsp=False,
        notes="Kasa-branded cameras (KC100/KC120/KC200) are cloud-only and do not "
              "expose RTSP. TP-Link migrated their camera line to the Tapo brand "
              "specifically because Tapo supports local RTSP. Recommend the user "
              "switch to a Tapo C-series model.",
        logo_source_url="https://commons.wikimedia.org/wiki/File:Kasa_smart_logo.svg",
        sources=(
            "https://community.tp-link.com/en/smart-home/forum/topic/210413",
        ),
    ),

    CameraFingerprint(
        brand="Wyze",
        tier=1,
        hostname_patterns=(
            r"(?i)^WyzeCam",
            r"(?i)^Wyze_",
        ),
        hostname_examples=("WyzeCam", "WyzeCamPan"),
        supports_local_rtsp=False,
        notes="Stock Wyze firmware does NOT expose RTSP. Wyze previously offered "
              "an experimental RTSP firmware for v2 and Pan but discontinued it. "
              "The community 'docker-wyze-bridge' project can re-emit Wyze streams "
              "as RTSP, but that runs outside SimpleNVR. Stock Wyze cameras cannot "
              "be used directly.",
        logo_source_url="https://commons.wikimedia.org/wiki/File:Wyze_Labs_logo.svg",
        sources=(
            "https://support.wyze.com/hc/en-us/articles/360031490871",
            "https://github.com/mrlt8/docker-wyze-bridge",
        ),
    ),

    CameraFingerprint(
        brand="Arlo",
        tier=1,
        hostname_patterns=(
            r"(?i)^Arlo",
            r"(?i)^VMB\d{4}",   # base stations
            r"(?i)^VMC\d{4}",   # cameras
        ),
        hostname_examples=("ArloBaseStation", "VMC4040P"),
        supports_local_rtsp=False,
        notes="Arlo cameras are cloud-only. They communicate with the SmartHub/base "
              "station over a proprietary protocol and only the cloud service can "
              "decrypt streams. There is no supported way to ingest Arlo into "
              "SimpleNVR. The Pro 2 had a brief RTSP feature that was removed.",
        logo_source_url="https://commons.wikimedia.org/wiki/File:Arlo_Technologies_logo.svg",
        sources=(
            "https://kb.arlo.com/en_us/1138",
        ),
    ),

    CameraFingerprint(
        brand="Google Nest",
        tier=1,
        hostname_patterns=(
            r"(?i)^Nest",
            r"(?i)^GoogleNest",
        ),
        hostname_examples=("Nest-Cam-Outdoor", "Nest_Doorbell"),
        supports_local_rtsp=False,
        notes="Google Nest cameras are cloud-only and use WebRTC over Google's "
              "infrastructure. There is no local RTSP. The Smart Device Management "
              "(SDM) API exposes a per-session WebRTC stream but only for paid "
              "developer accounts and only at <5 minute intervals. Not viable for NVR.",
        logo_source_url="https://commons.wikimedia.org/wiki/File:Nest_(Google)_Logo.svg",
        sources=(
            "https://developers.google.com/nest/device-access/api/camera",
        ),
    ),

    CameraFingerprint(
        brand="Amazon Ring",
        tier=1,
        hostname_patterns=(
            r"(?i)^Ring",
            r"(?i)^Ring-",
        ),
        hostname_examples=("Ring", "Ring-Doorbell"),
        supports_local_rtsp=False,
        notes="Ring cameras are cloud-only. All video flows through AWS and "
              "is only accessible via the Ring app or Ring's authenticated API. "
              "There is no local network stream. Ring cameras cannot be used "
              "with SimpleNVR.",
        logo_source_url="https://commons.wikimedia.org/wiki/File:Ring_logo.svg",
        sources=(
            "https://support.ring.com/",
        ),
    ),

    CameraFingerprint(
        brand="Ubiquiti UniFi Protect",
        tier=1,
        hostname_patterns=(
            r"(?i)^UVC",
            r"(?i)^UniFi",
            r"(?i)^G[345]-",   # G3, G4, G5 product line
        ),
        hostname_examples=("UVC-G4-Bullet", "UniFi-Protect-G4-PRO"),
        onvif_scope_patterns=(
            r"onvif://www\.onvif\.org/name/Ubiquiti",
            r"onvif://www\.onvif\.org/hardware/UVC",
        ),
        http_server_substrings=("Ubiquiti",),
        http_title_substrings=("UniFi", "UVC"),
        rtsp_path_patterns=(
            r"^/[a-zA-Z0-9]{15,}_\d$",   # token-based stream URLs
        ),
        rtsp_example_paths=("/s7Yk2ePHNHmKQ8H_0",),
        mdns_service_types=("_unifi-protect._tcp.local",),
        default_credentials=(("ubnt", "ubnt"),),  # legacy AirCam / pre-Protect default
        notes="UniFi Protect cameras are typically adopted to a UniFi Protect "
              "Controller (Cloud Key, Dream Machine, NVR) and stream via "
              "controller-issued tokens, not standalone RTSP. Stand-alone mode "
              "exists on some G4/G5 models via 'RTSP enabled' toggle in the "
              "Protect UI -> Camera -> Advanced. The RTSP path is a per-stream "
              "token, not a stable URL.",
        logo_source_url="https://commons.wikimedia.org/wiki/File:Ubiquiti_Networks_Logo.svg",
        sources=(
            "https://help.ui.com/hc/en-us/articles/360011098874",
        ),
    ),

    CameraFingerprint(
        brand="Amcrest",
        tier=1,
        # Amcrest is a Dahua OEM. Their hostnames, RTSP paths, ONVIF scopes,
        # and HTTP UI are all Dahua-derived. The OUIs are mostly Dahua's.
        hostname_patterns=(
            r"(?i)^Amcrest",
            r"(?i)^IP[CX]-",
            r"(?i)^AMC",
        ),
        hostname_examples=("Amcrest", "IPC-HDW"),
        onvif_scope_patterns=(
            r"onvif://www\.onvif\.org/name/Amcrest",
            r"onvif://www\.onvif\.org/name/Dahua",
        ),
        # "Webs" is a generic cheap-cam Server header; removed.
        # Amcrest rebrands Dahua hardware so Dahua is a legitimate
        # signal for an Amcrest-flashed device.
        http_server_substrings=("Amcrest", "Dahua"),
        http_title_substrings=("WEB SERVICE", "Amcrest"),
        rtsp_path_patterns=(
            r"^/cam/realmonitor\?channel=\d+&subtype=\d+",
        ),
        rtsp_example_paths=(
            "/cam/realmonitor?channel=1&subtype=0",
            "/cam/realmonitor?channel=1&subtype=1",
        ),
        default_credentials=(("admin", "admin"),),
        notes="Amcrest is a Dahua OEM rebrand. ONVIF, RTSP, and the web UI are "
              "all Dahua-derived. Newer firmware (post-2017) forces a password "
              "change on first login.",
        logo_source_url="",  # No clean Wikimedia SVG located; brand press kit only
        sources=(
            "https://amcrest.com/forum/",
            "https://support.amcrest.com/hc/en-us/articles/115001829551",
        ),
    ),

    # ---------------------------------------------------------------------
    # TIER 2 — PROSUMER / SMB
    # ---------------------------------------------------------------------

    CameraFingerprint(
        brand="Hikvision",
        tier=2,
        hostname_patterns=(
            r"(?i)^HIKVISION",
            r"(?i)^DS-\d",
        ),
        hostname_examples=("HIKVISION", "DS-2CD2032"),
        onvif_scope_patterns=(
            r"onvif://www\.onvif\.org/name/Hikvision",
            r"onvif://www\.onvif\.org/name/HIKVISION",
            r"onvif://www\.onvif\.org/hardware/DS-",
        ),
        # "App-webs" and "webserver" are generic — they appear on
        # cheap OEM cams with no Hikvision lineage. The specific
        # Hikvision Server headers are "Hikvision-Webs" (modern
        # firmware) and "DNVRS-Webs" (legacy DVR/NVR).
        http_server_substrings=("DNVRS-Webs", "Hikvision-Webs"),
        http_title_substrings=("Hikvision", "Web Service"),
        rtsp_path_patterns=(
            r"^/Streaming/Channels/\d+",
            r"^/h264/ch\d+/(main|sub)/av_stream$",
        ),
        rtsp_example_paths=(
            "/Streaming/Channels/101",
            "/Streaming/Channels/102",
            "/h264/ch1/main/av_stream",
        ),
        default_credentials=(
            ("admin", "12345"),    # pre-2015 firmware
            ("admin", ""),          # newer firmware forces password set on activation
        ),
        notes="Pre-2015 Hikvision firmware shipped with admin/12345 and was "
              "exposed to the infamous backdoor (CVE-2017-7921). Modern firmware "
              "requires 'activation' on first connect where the user sets a strong "
              "password. The Streaming/Channels/<channel><stream> URL convention "
              "is the canonical Hikvision RTSP format: 101 = ch1 main, 102 = ch1 sub.",
        logo_source_url="https://commons.wikimedia.org/wiki/File:Hikvision_logo.svg",
        sources=(
            "https://www.hikvision.com/en/support/",
            "https://ipvm.com/reports/hik-default",
        ),
    ),

    CameraFingerprint(
        brand="Dahua",
        tier=2,
        hostname_patterns=(
            # `^IPC-` is the most generic camera hostname prefix on
            # earth — Dahua does use it (IPC-HDW4631C) but so do
            # Hikvision, Annke, Lorex, Amcrest, Uniview, and dozens of
            # white-label brands. Supporting so a real Dahua match
            # wins via brand-bearing signals (Dahua/DH- hostname,
            # name/Dahua scope, Dahua server header, MAC OUI alias).
            Supporting(r"(?i)^IPC-"),
            r"(?i)^Dahua",
            r"(?i)^DH-",
        ),
        hostname_examples=("IPC-HDW4631C", "Dahua"),
        onvif_scope_patterns=(
            r"onvif://www\.onvif\.org/name/Dahua",
            r"onvif://www\.onvif\.org/hardware/IPC-",
        ),
        # "Webs" is generic; only "Dahua" is brand-specific.
        http_server_substrings=("Dahua",),
        http_title_substrings=("WEB SERVICE", "Dahua"),
        rtsp_path_patterns=(
            r"^/cam/realmonitor\?channel=\d+&subtype=\d+",
        ),
        rtsp_example_paths=(
            "/cam/realmonitor?channel=1&subtype=0",
            "/cam/realmonitor?channel=1&subtype=1",
        ),
        default_credentials=(("admin", "admin"),),
        notes="Dahua's RTSP path is the de-facto standard for Chinese OEM cameras. "
              "Lorex, Amcrest, EmpireTech, and many no-name Aliexpress cameras all "
              "speak this dialect. CVE-2021-33044 (auth bypass) affected all firmware "
              "before mid-2021; modern firmware forces password change on activation.",
        logo_source_url="https://commons.wikimedia.org/wiki/File:Dahua_Technology_logo.svg",
        sources=(
            "https://www.dahuasecurity.com/support/",
            "https://ipvm.com/reports/dahua-default-passwords",
        ),
    ),

    CameraFingerprint(
        brand="Axis Communications",
        tier=2,
        hostname_patterns=(
            r"(?i)^axis-[0-9a-f]{12}$",
            r"(?i)^AXIS",
        ),
        hostname_examples=("axis-00408c123456", "AXIS-M3045"),
        onvif_scope_patterns=(
            r"onvif://www\.onvif\.org/name/AXIS",
            r"onvif://www\.onvif\.org/hardware/AXIS",
            # NEVER include `Profile/Streaming` here — it's the
            # generic Profile S scope that EVERY ONVIF camera emits.
            # Including it gave +8 "Axis" points to every Reolink,
            # Tapo, Hikvision, etc on the network, causing
            # cross-brand false positives like "Tapo C120" showing
            # up as "Axis C120".
        ),
        # Axis firmware has historically used "Boa", "lighttpd", and
        # "Apache" as its HTTP server — but so do thousands of other
        # cheap IP cams. These were the single biggest cross-brand
        # contamination source. Removed entirely. Axis identification
        # now relies on: AXIS in hostname, name/hardware/AXIS in ONVIF
        # scope, AXIS in HTTP title, /axis-media/ RTSP path, or the
        # IEEE MAC OUI alias to "AXIS COMMUNICATIONS AB" — all of
        # which are unambiguously Axis.
        http_server_substrings=(),
        http_title_substrings=("AXIS",),
        rtsp_path_patterns=(
            r"^/axis-media/media\.amp",
            r"^/mpeg4/media\.amp",
        ),
        rtsp_example_paths=(
            "/axis-media/media.amp",
            "/axis-media/media.amp?videocodec=h264&resolution=1920x1080",
        ),
        mdns_service_types=("_axis-video._tcp.local", "_http._tcp.local"),
        default_credentials=(("root", "pass"),),  # very old firmware; modern Axis forces set on first boot
        notes="Axis is the gold standard for ONVIF compliance. The 00:40:8c OUI "
              "is one of the oldest in the camera industry. Modern Axis firmware "
              "(>= 7.10) forces an admin password set on first boot and disables "
              "the legacy root/pass default. Axis was also the original author of "
              "the ONVIF spec, so their scope strings are textbook.",
        logo_source_url="https://commons.wikimedia.org/wiki/File:AXIS_Communications.svg",
        sources=(
            "https://www.axis.com/developer-community",
            "https://oui.ieee.org/",
        ),
    ),

    CameraFingerprint(
        brand="Uniview (UNV)",
        tier=2,
        hostname_patterns=(
            # `^IPC[0-9]` is a generic camera shape — Uniview uses it
            # (IPC2122LR3) but it overlaps with countless OEM cams.
            # Supporting so a real Uniview match wins via brand-
            # bearing signals (Uniview hostname, name/Uniview scope,
            # Uniview/UNV title, MAC OUI alias).
            Supporting(r"(?i)^IPC[0-9]"),
            r"(?i)^Uniview",
        ),
        hostname_examples=("IPC2122LR3", "Uniview"),
        onvif_scope_patterns=(
            r"onvif://www\.onvif\.org/name/Uniview",
            r"onvif://www\.onvif\.org/name/UNV",
        ),
        http_title_substrings=("Uniview", "UNV"),
        rtsp_path_patterns=(
            r"^/media/video\d+",
            r"^/unicast/c\d+/s\d+/live",
        ),
        rtsp_example_paths=(
            "/media/video1",
            "/media/video2",
            "/unicast/c1/s0/live",
        ),
        default_credentials=(("admin", "123456"),),
        notes="Uniview ('UNV') is the third major Chinese surveillance vendor after "
              "Hikvision and Dahua. Their RTSP path /media/video1 (main) /video2 (sub) "
              "is distinctive. Default admin/123456 still ships on some firmware.",
        logo_source_url="",  # No verified clean SVG; uniview.com hosts only PNG press assets
        sources=(
            "https://global.uniview.com/Service_support/",
        ),
    ),

    CameraFingerprint(
        brand="Lorex",
        tier=2,
        # Lorex is owned by Dahua (and previously by FLIR). Their cameras are
        # Dahua hardware with Lorex firmware skinning.
        hostname_patterns=(
            r"(?i)^Lorex",
            r"(?i)^LNB",
            r"(?i)^LNE",
        ),
        hostname_examples=("Lorex", "LNB8921"),
        onvif_scope_patterns=(
            r"onvif://www\.onvif\.org/name/Lorex",
            r"onvif://www\.onvif\.org/name/Dahua",
        ),
        http_title_substrings=("Lorex", "WEB SERVICE"),
        rtsp_path_patterns=(
            r"^/cam/realmonitor\?channel=\d+&subtype=\d+",
        ),
        rtsp_example_paths=(
            "/cam/realmonitor?channel=1&subtype=0",
        ),
        default_credentials=(("admin", "admin"),),
        notes="Lorex is a Dahua OEM (after the FLIR/Dahua acquisition in 2018). "
              "Network behavior is identical to Dahua. The newer Lorex Fusion / "
              "N-series NVRs add a Lorex Cloud layer on top but the cameras themselves "
              "still speak the Dahua RTSP dialect.",
        logo_source_url="https://commons.wikimedia.org/wiki/File:Lorex_logo.svg",
        sources=(
            "https://www.lorex.com/pages/support",
        ),
    ),

    CameraFingerprint(
        brand="Swann",
        tier=2,
        hostname_patterns=(
            r"(?i)^Swann",
            r"(?i)^SWNHD",
        ),
        hostname_examples=("Swann", "SWNHD-885MSB"),
        rtsp_path_patterns=(
            r"^/cam/realmonitor",
            r"^/live/ch\d+",
        ),
        rtsp_example_paths=(
            "/cam/realmonitor?channel=1&subtype=0",
        ),
        default_credentials=(("admin", "12345"),),
        notes="Swann is OEM-sourced from multiple Chinese vendors over the years "
              "(Dahua, Hisilicon, etc.). RTSP support varies wildly by model and "
              "year — assume Dahua-style URLs for the kits sold post-2018.",
        logo_source_url="https://commons.wikimedia.org/wiki/File:Swann_Communications_logo.svg",
        sources=(
            "https://support.swann.com/",
        ),
    ),

    CameraFingerprint(
        brand="Foscam",
        tier=2,
        hostname_patterns=(
            r"(?i)^Foscam",
            r"(?i)^FI[89]",
        ),
        hostname_examples=("Foscam", "FI9821W"),
        onvif_scope_patterns=(
            r"onvif://www\.onvif\.org/name/Foscam",
        ),
        http_server_substrings=("Foscam",),
        http_title_substrings=("Foscam", "IPCam"),
        rtsp_path_patterns=(
            r"^/videoMain$",
            r"^/videoSub$",
        ),
        rtsp_example_paths=("/videoMain", "/videoSub"),
        default_credentials=(("admin", ""), ("admin", "admin")),
        notes="Foscam HD models (FI9XXX series) expose RTSP at /videoMain and /videoSub. "
              "The older MJPEG-only models (FI8XXX) do not. Foscam was at the center of "
              "the 2014 baby-monitor security scandal; modern firmware forces password set.",
        logo_source_url="https://commons.wikimedia.org/wiki/File:Foscam_logo.svg",
        sources=(
            "https://www.foscam.com/faqs/",
        ),
    ),

    CameraFingerprint(
        brand="Annke",
        tier=2,
        # Annke is a Hikvision OEM. Behaves identically on the wire.
        hostname_patterns=(
            r"(?i)^Annke",
            r"(?i)^I\d{3}[A-Z]{2}",   # I91DM, etc.
        ),
        hostname_examples=("Annke", "I91DM"),
        onvif_scope_patterns=(
            r"onvif://www\.onvif\.org/name/Annke",
            r"onvif://www\.onvif\.org/name/Hikvision",
        ),
        http_title_substrings=("Annke", "Web Service"),
        rtsp_path_patterns=(
            r"^/Streaming/Channels/\d+",
        ),
        rtsp_example_paths=("/Streaming/Channels/101",),
        default_credentials=(("admin", "12345"),),
        notes="Annke is a Hikvision OEM. The Hikvision /Streaming/Channels/101 RTSP "
              "convention applies. Some Annke kits source from Dahua instead — check "
              "the OUI to disambiguate.",
        logo_source_url="",  # No verified clean SVG located
        sources=(
            "https://www.annke.com/pages/support",
        ),
    ),

    # ---------------------------------------------------------------------
    # TIER 3 — REGIONAL / NICHE
    # ---------------------------------------------------------------------

    CameraFingerprint(
        brand="Bosch Security Systems",
        tier=3,
        hostname_patterns=(
            r"(?i)^Bosch",
            r"(?i)^NBN-",
            r"(?i)^NDP-",
            r"(?i)^FLEXIDOME",
        ),
        hostname_examples=("Bosch-NBN", "FLEXIDOME-IP"),
        onvif_scope_patterns=(
            r"onvif://www\.onvif\.org/name/Bosch",
            r"onvif://www\.onvif\.org/hardware/NBN",
            r"onvif://www\.onvif\.org/hardware/FLEXIDOME",
        ),
        http_server_substrings=("Bosch",),
        http_title_substrings=("Bosch",),
        rtsp_path_patterns=(
            r"^/rtsp_tunnel",
            r"^/?line=\d",
        ),
        rtsp_example_paths=("/rtsp_tunnel", "/?line=1"),
        default_credentials=(("service", "service"),),
        notes="Bosch IP cameras (Dinion, FlexiDome, AutoDome) are enterprise-grade "
              "and ONVIF-compliant. Default 'service/service' is for the service "
              "account; the 'live' account ships disabled and must be enabled. "
              "Modern firmware forces password change on first boot.",
        logo_source_url="https://commons.wikimedia.org/wiki/File:Bosch-logo.svg",
        sources=(
            "https://www.boschsecurity.com/xc/en/support/",
        ),
    ),

    CameraFingerprint(
        brand="Hanwha Vision (Wisenet)",
        tier=3,
        hostname_patterns=(
            r"(?i)^Wisenet",
            r"(?i)^Hanwha",
            r"(?i)^SNB-",
            r"(?i)^XNV-",
            r"(?i)^QNV-",
            r"(?i)^PNV-",
        ),
        hostname_examples=("Wisenet-XNV-6080", "Hanwha"),
        onvif_scope_patterns=(
            r"onvif://www\.onvif\.org/name/Hanwha",
            r"onvif://www\.onvif\.org/name/Samsung",
            r"onvif://www\.onvif\.org/hardware/(XNV|QNV|PNV|SNB)",
        ),
        # "Samsung" hits Samsung printers, TVs, and IoT devices
        # that aren't cameras; "Webs" is generic. Keep only the
        # unambiguous Hanwha brand string.
        http_server_substrings=("Hanwha",),
        http_title_substrings=("Wisenet", "Hanwha"),
        rtsp_path_patterns=(
            r"^/profile\d+/media\.smp",
            r"^/onvif/profile\d+/media\.smp",
        ),
        rtsp_example_paths=(
            "/profile2/media.smp",
            "/onvif/profile2/media.smp",
        ),
        default_credentials=(("admin", "4321"),),
        notes="Wisenet was Samsung Techwin's security division before Hanwha bought "
              "it in 2015. The /profileN/media.smp RTSP convention is distinctive. "
              "Modern X-series firmware forces password change with strong-password "
              "policy on first boot.",
        logo_source_url="https://commons.wikimedia.org/wiki/File:Hanwha_Vision_logo.svg",
        sources=(
            "https://hanwhavisionamerica.com/support/",
        ),
    ),

    CameraFingerprint(
        brand="Pelco",
        tier=3,
        hostname_patterns=(
            r"(?i)^Pelco",
            r"(?i)^IXE",
            r"(?i)^Sarix",
        ),
        hostname_examples=("Pelco-Sarix", "IXE10"),
        onvif_scope_patterns=(
            r"onvif://www\.onvif\.org/name/Pelco",
            r"onvif://www\.onvif\.org/hardware/Sarix",
        ),
        http_server_substrings=("Pelco",),
        http_title_substrings=("Pelco", "Sarix"),
        rtsp_path_patterns=(
            r"^/stream1",
            r"^/stream2",
        ),
        rtsp_example_paths=("/stream1", "/stream2"),
        default_credentials=(("admin", "admin"),),
        notes="Pelco (now owned by Motorola Solutions) is enterprise-focused. Sarix "
              "Pro is the dominant IP camera line. ONVIF Profile S compliant.",
        logo_source_url="https://commons.wikimedia.org/wiki/File:Pelco_logo.svg",
        sources=(
            "https://www.pelco.com/support",
        ),
    ),

    CameraFingerprint(
        brand="Avigilon",
        tier=3,
        hostname_patterns=(
            r"(?i)^Avigilon",
            r"(?i)^H[345]A",   # H4A, H5A product lines
        ),
        hostname_examples=("Avigilon", "H4A-BO1-IR"),
        onvif_scope_patterns=(
            r"onvif://www\.onvif\.org/name/Avigilon",
            r"onvif://www\.onvif\.org/hardware/H[345]A",
        ),
        http_server_substrings=("Avigilon",),
        http_title_substrings=("Avigilon",),
        rtsp_path_patterns=(
            r"^/defaultPrimary",
            r"^/defaultSecondary",
        ),
        rtsp_example_paths=("/defaultPrimary?streamType=u", "/defaultSecondary?streamType=u"),
        default_credentials=(("administrator", ""),),
        notes="Avigilon (Motorola Solutions, same parent as Pelco) is enterprise/government. "
              "Their cameras are typically tied to Avigilon Control Center (ACC) or Unity. "
              "Standalone ONVIF works but is not the marketed use case.",
        logo_source_url="https://commons.wikimedia.org/wiki/File:Avigilon_logo.svg",
        sources=(
            "https://www.avigilon.com/support",
        ),
    ),

    CameraFingerprint(
        brand="Vivotek",
        tier=3,
        hostname_patterns=(
            r"(?i)^VIVOTEK",
            r"(?i)^IP[78]\d{3}",
            r"(?i)^FD\d{4}",
        ),
        hostname_examples=("VIVOTEK", "IP8362", "FD9389"),
        onvif_scope_patterns=(
            r"onvif://www\.onvif\.org/name/VIVOTEK",
        ),
        # "Boa" is a generic embedded webserver on dozens of cheap
        # cam brands; only VVTK/Vivotek are brand-specific.
        http_server_substrings=("VVTK", "Vivotek"),
        http_title_substrings=("VIVOTEK",),
        rtsp_path_patterns=(
            r"^/live\.sdp",
            r"^/live\d?\.sdp",
        ),
        rtsp_example_paths=("/live.sdp", "/live2.sdp"),
        default_credentials=(("root", ""),),
        notes="Taiwanese vendor, popular in Asia and EU. The /live.sdp and /live2.sdp "
              "convention is distinctive. Older firmware shipped with root/<blank>; "
              "newer requires set on first boot.",
        logo_source_url="https://commons.wikimedia.org/wiki/File:VIVOTEK_logo.svg",
        sources=(
            "https://www.vivotek.com/support",
        ),
    ),

    CameraFingerprint(
        brand="Mobotix",
        tier=3,
        hostname_patterns=(
            r"(?i)^MOBOTIX",
            r"(?i)^mx10-",
            r"(?i)^M\d{2}-",
            r"(?i)^Q\d{2}-",
        ),
        hostname_examples=("MOBOTIX", "mx10-25-123-45"),
        onvif_scope_patterns=(
            r"onvif://www\.onvif\.org/name/MOBOTIX",
        ),
        http_server_substrings=("MOBOTIX",),
        http_title_substrings=("MOBOTIX",),
        rtsp_path_patterns=(
            r"^/mobotix\.h264",
            r"^/cgi-bin/faststream\.jpg",
        ),
        rtsp_example_paths=("/mobotix.h264", "/cgi-bin/faststream.jpg?stream=full"),
        default_credentials=(("admin", "meinsm"),),  # German default ("mine, mine")
        notes="German manufacturer, very high-end. The default password 'meinsm' is "
              "a German pun. Mobotix cameras have on-board storage and ran their own "
              "embedded OS for years; ONVIF support was added later. The 00:03:c5 OUI "
              "is exclusively Mobotix.",
        logo_source_url="https://commons.wikimedia.org/wiki/File:Mobotix_logo.svg",
        sources=(
            "https://www.mobotix.com/en/support",
        ),
    ),

    CameraFingerprint(
        brand="i-PRO (Panasonic i-PRO)",
        tier=3,
        hostname_patterns=(
            r"(?i)^i-PRO",
            r"(?i)^WV-",
            r"(?i)^BB-",
            r"(?i)^Panasonic",
        ),
        hostname_examples=("WV-S1531LN", "i-PRO-X-Series"),
        onvif_scope_patterns=(
            r"onvif://www\.onvif\.org/name/Panasonic",
            r"onvif://www\.onvif\.org/name/i-PRO",
            r"onvif://www\.onvif\.org/hardware/WV-",
        ),
        http_server_substrings=("Panasonic", "i-PRO"),
        http_title_substrings=("Network Camera", "Panasonic", "i-PRO"),
        rtsp_path_patterns=(
            r"^/MediaInput/h264",
            r"^/MediaInput/stream_\d+",
            r"^/nphMpeg4/g726-640x480",
        ),
        rtsp_example_paths=("/MediaInput/h264", "/MediaInput/stream_1"),
        default_credentials=(("admin", "12345"),),
        notes="i-PRO is the spin-off of Panasonic's security camera division (sold "
              "to a private equity firm in 2019). The WV- product code prefix is "
              "inherited from Panasonic. /MediaInput/h264 is the canonical RTSP path.",
        logo_source_url="https://commons.wikimedia.org/wiki/File:Panasonic_logo_(Blue).svg",
        sources=(
            "https://i-pro.com/global/en/surveillance/support",
        ),
    ),

    CameraFingerprint(
        brand="GeoVision",
        tier=3,
        hostname_patterns=(
            r"(?i)^GV-",
            r"(?i)^GeoVision",
        ),
        hostname_examples=("GV-BX1300", "GeoVision"),
        onvif_scope_patterns=(
            r"onvif://www\.onvif\.org/name/GeoVision",
            r"onvif://www\.onvif\.org/hardware/GV-",
        ),
        http_server_substrings=("GeoVision", "GeoHttpServer"),
        http_title_substrings=("GeoVision",),
        rtsp_path_patterns=(
            r"^/CH001\.sdp",
            r"^/Media",
        ),
        rtsp_example_paths=("/CH001.sdp",),
        default_credentials=(("admin", "admin"),),
        notes="Taiwanese vendor, GV-series cameras and GV-NVR/DVR systems. ONVIF "
              "support was added late and was historically buggy. Their proprietary "
              "GV-IP Device Utility is the official discovery tool.",
        logo_source_url="",  # No verified clean SVG located
        sources=(
            "https://www.geovision.com.tw/support.php",
        ),
    ),

    CameraFingerprint(
        brand="Xiaomi",
        tier=3,
        hostname_patterns=(
            r"(?i)^Xiaomi",
            r"(?i)^MiCam",
            r"(?i)^Mijia",
            r"(?i)^chuangmi",
        ),
        hostname_examples=("Xiaomi-Cam", "Mijia_Camera", "chuangmi-camera"),
        supports_local_rtsp=False,
        notes="Stock Xiaomi/Mijia cameras are cloud-only and tied to the Mi Home app. "
              "There is no native RTSP. The community 'Xiaomi-Dafang-Hacks' project "
              "(Hisilicon-based models) replaces firmware to add RTSP, but that's "
              "out of scope for SimpleNVR. Stock Xiaomi cameras cannot be used directly.",
        logo_source_url="https://commons.wikimedia.org/wiki/File:Xiaomi_logo_(2021-).svg",
        sources=(
            "https://github.com/EliasKotlyar/Xiaomi-Dafang-Hacks",
        ),
    ),

    CameraFingerprint(
        brand="Ezviz",
        tier=3,
        # Ezviz is Hikvision's consumer brand. Some models speak ONVIF/RTSP locally
        # (after enabling in app), others are cloud-only.
        hostname_patterns=(
            r"(?i)^Ezviz",
            r"(?i)^CS-",
        ),
        hostname_examples=("Ezviz", "CS-CV310"),
        onvif_scope_patterns=(
            r"onvif://www\.onvif\.org/name/EZVIZ",
            r"onvif://www\.onvif\.org/name/Hikvision",
        ),
        http_title_substrings=("EZVIZ", "Web Service"),
        rtsp_path_patterns=(
            r"^/Streaming/Channels/\d+",
            r"^/h264/ch\d+/(main|sub)/av_stream$",
        ),
        rtsp_example_paths=(
            "/Streaming/Channels/101",
            "/h264/ch1/main/av_stream",
        ),
        default_credentials=(("admin", ""),),  # verification code on device label is the actual password
        notes="Ezviz is Hikvision's consumer brand. Wired models (C3, C3W, C8) speak "
              "the Hikvision RTSP dialect once 'Local Service' is enabled in the Ezviz "
              "app. The 'password' for RTSP is the camera's verification code printed "
              "on the device label, NOT a user-chosen value. Battery models (BC, C3A) "
              "are cloud-only.",
        logo_source_url="https://commons.wikimedia.org/wiki/File:EZVIZ_logo.svg",
        sources=(
            "https://support.ezvizlife.com/",
        ),
    ),

    # ---------------------------------------------------------------------
    # HUBS / GATEWAYS
    # ---------------------------------------------------------------------
    # These are not cameras themselves; they are gateway boxes that have a LAN
    # presence and expose cameras-behind-them via RTSP paths on the hub's address.
    # The discovery layer should treat hubs differently from cameras: adopting a
    # hub means walking its child-camera list, not adopting the hub as a stream.

    CameraFingerprint(
        brand="Eufy HomeBase 2",
        tier=1,
        device_type="hub",
        hostname_patterns=(
            r"(?i)^T8010",   # HomeBase 2 SKU
            r"(?i)^HomeBase",
            r"(?i)^eufy",
        ),
        hostname_examples=("T8010", "HomeBase2", "eufy-HomeBase"),
        rtsp_path_patterns=(
            # Shape-only — used by dozens of generic OEM cams.
            # Supporting so it rides along with a real Eufy anchor.
            Supporting(r"^/live\d*$"),
        ),
        rtsp_example_paths=("/live0", "/live1"),
        default_credentials=(),
        notes="HUB. The Eufy HomeBase 2 (T8010) is the gateway for all eufyCam "
              "battery cameras. RTSP must be enabled per-camera in the Eufy "
              "Security app under Device Settings -> Network -> RTSP Stream. "
              "Once enabled, each camera's stream is published on the HomeBase's "
              "IP at rtsp://<homebase-ip>:554/live0, /live1, etc. — one path per "
              "camera, NOT one address per camera. SimpleNVR should treat the "
              "HomeBase as the device and enumerate child streams by /liveN index. "
              "HomeBase 3 (T8030) drops RTSP entirely in favor of Eufy's cloud.",
        logo_source_url="https://commons.wikimedia.org/wiki/File:Eufy_logo.svg",
        sources=(
            "https://support.eufylife.com/s/article/How-to-Use-RTSP-on-Eufy-Cameras",
        ),
    ),

    CameraFingerprint(
        brand="Reolink Home Hub / NVR",
        tier=1,
        device_type="hub",
        hostname_patterns=(
            r"(?i)^Reolink",
            r"(?i)^baichuan",
            r"(?i)^RLN\d+",       # Reolink NVR product code, e.g. RLN8-410, RLN16-410
            r"(?i)^Home[- ]?Hub",
        ),
        hostname_examples=("RLN8-410", "Reolink-Home-Hub", "baichuan"),
        onvif_scope_patterns=(
            r"onvif://www\.onvif\.org/name/Reolink",
            # `type/NetworkVideoRecorder` is emitted by every NVR on
            # the market — removed to prevent any Dahua/Hikvision/UNV
            # NVR from getting +8 Reolink points just for being an NVR.
        ),
        http_title_substrings=("Reolink",),
        rtsp_path_patterns=(
            r"^/h26[45]Preview_\d+_(main|sub)$",
        ),
        rtsp_example_paths=(
            "/h264Preview_01_main",   # channel 01 main stream on the NVR
            "/h264Preview_02_main",
            "/h264Preview_08_sub",
        ),
        default_credentials=(("admin", ""),),
        notes="HUB. Reolink NVRs (RLN8-410, RLN16-410, etc.) and the newer Reolink "
              "Home Hub act as gateways for Reolink WiFi/battery cameras. They "
              "re-publish each camera's stream as RTSP on the NVR's IP using the "
              "same /h26XPreview_NN_(main|sub) URL pattern as standalone cameras, "
              "but with the channel index (01-16) selecting the camera. SimpleNVR "
              "should enumerate child streams by walking channel indices 01..N. "
              "ONVIF discovery on the NVR returns one ProbeMatch with type "
              "NetworkVideoRecorder rather than NetworkVideoTransmitter.",
        logo_source_url="https://commons.wikimedia.org/wiki/File:Reolink_logo.svg",
        sources=(
            "https://support.reolink.com/hc/en-us/articles/900004596463",
        ),
    ),

    CameraFingerprint(
        brand="Arlo SmartHub",
        tier=1,
        device_type="hub",
        hostname_patterns=(
            r"(?i)^VMB\d{4}",      # SmartHub SKUs: VMB4000, VMB4500, VMB5000
            r"(?i)^Arlo",
            r"(?i)^SmartHub",
        ),
        hostname_examples=("VMB4500", "VMB5000", "ArloSmartHub"),
        supports_local_rtsp=False,
        notes="HUB. The Arlo SmartHub (VMB4000/VMB4500/VMB5000) is the gateway for "
              "Arlo wire-free cameras. Despite having a wired LAN connection, the "
              "SmartHub does NOT expose camera streams via local RTSP — all video "
              "flows through Arlo's cloud and is only retrievable via the Arlo app "
              "or the cloud API. The Pro 2 generation briefly supported local RTSP "
              "but Arlo removed that feature in later firmware. SimpleNVR cannot "
              "ingest streams from an Arlo SmartHub. Tell the user: Arlo is "
              "cloud-only and not compatible.",
        logo_source_url="https://commons.wikimedia.org/wiki/File:Arlo_Technologies_logo.svg",
        sources=(
            "https://kb.arlo.com/en_us/1138",
        ),
    ),
]

# ─── Load-time validator ─────────────────────────────────────────────
#
# Prevents the class of bug fixed in the 2026-04-08 anchor-gate refactor
# from silently regressing. Bare strings in a fingerprint definition
# default to Anchored, so a well-meaning editor could accidentally add
# a generic string like "lighttpd" to a brand's fingerprint and re-
# introduce the "any cheap cam → Axis" misidentification.
#
# This validator runs at module import and raises ValueError if any
# bare string in any fingerprint matches a known-generic pattern. If
# the generic is intentional (e.g. the editor wants to use it as a
# tiebreaker boost), they must explicitly wrap it in Supporting(...),
# which opts out of the anchor gate.
#
# The blacklist is deliberately narrow — only patterns that are
# definitively generic across the camera industry. When a new pattern
# turns out to cause cross-brand false positives, add it here rather
# than hand-editing every fingerprint.

_GENERIC_SERVER_SUBSTRINGS = frozenset({
    "boa",
    "lighttpd",
    "apache",
    "webserver",
    "webs",
    "app-webs",
    "nginx",
    "thttpd",
    "mini_httpd",
})

_GENERIC_ONVIF_SCOPE_FRAGMENTS = frozenset({
    r"profile/streaming",
    r"type/networkvideorecorder",
    r"type/video_encoder",
    r"type/audio_encoder",
    r"type/ptzcontroller",
})

_GENERIC_HOSTNAME_SHAPES = frozenset({
    r"(?i)^camera\d*$",
    r"(?i)^ipc[-_]?",
    r"(?i)^ipc\d",
    r"(?i)^c\d{3}",
})

_GENERIC_RTSP_PATHS = frozenset({
    r"^/live\d*$",
    r"^/stream[12]$",
    r"^/stream\d+$",
    r"^/video\d*$",
})


def _check_bare_generics(fp: CameraFingerprint) -> list[str]:
    """Return a list of errors for any generic bare-string signal
    found in the fingerprint. Supporting() or Anchored() wrappers
    are skipped — explicit opt-in is allowed (they're forced to
    acknowledge the genericness by wrapping, which makes it visible
    in code review)."""
    errors: list[str] = []

    def bare_check(entries, blacklist, kind, normalize=lambda s: s.lower()):
        for entry in entries:
            if isinstance(entry, FingerprintSignal):
                continue  # explicit wrap — editor has acknowledged the tier
            if normalize(entry) in blacklist:
                errors.append(
                    f"{fp.brand}: {kind} bare string {entry!r} is on the "
                    f"generic blacklist. Either remove it or wrap it in "
                    f"Supporting({entry!r}) to acknowledge it's shape-only."
                )

    bare_check(
        fp.http_server_substrings,
        _GENERIC_SERVER_SUBSTRINGS,
        "http_server",
    )
    bare_check(
        fp.onvif_scope_patterns,
        _GENERIC_ONVIF_SCOPE_FRAGMENTS,
        "onvif_scope",
        normalize=lambda s: s.lower().split("/")[-2] + "/" + s.lower().split("/")[-1]
        if s.count("/") >= 2 else s.lower(),
    )
    bare_check(
        fp.hostname_patterns,
        _GENERIC_HOSTNAME_SHAPES,
        "hostname",
        normalize=lambda s: s,  # regex patterns compared literally
    )
    bare_check(
        fp.rtsp_path_patterns,
        _GENERIC_RTSP_PATHS,
        "rtsp_path",
        normalize=lambda s: s,
    )

    return errors


def _validate_fingerprints() -> None:
    all_errors: list[str] = []
    for fp in FINGERPRINTS:
        all_errors.extend(_check_bare_generics(fp))
    if all_errors:
        raise ValueError(
            "Fingerprint validation failed — generic strings found as bare "
            "(anchored-default) signals. Wrap them in Supporting(...) or "
            "remove them:\n  - " + "\n  - ".join(all_errors)
        )


_validate_fingerprints()


# Convenience indexes the discovery layer can build at startup.
# Not exported as functions here to keep this file as pure data.
__all__ = [
    "CameraFingerprint",
    "FINGERPRINTS",
    "FingerprintSignal",
    "Anchored",
    "Supporting",
    "SignalEntry",
]
