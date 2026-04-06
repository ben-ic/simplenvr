"""
ONVIF client wrapper using onvif-zeep-async.

Handles device interrogation: GetDeviceInformation, GetProfiles, GetStreamUri.
Detects auth faults and falls back to scope-based metadata.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from urllib.parse import quote, urlparse

logger = logging.getLogger(__name__)


@dataclass
class CameraInfo:
    ip: str
    xaddr: str
    manufacturer: str | None = None
    model: str | None = None
    firmware: str | None = None
    serial_number: str | None = None
    hardware_id: str | None = None
    resolutions: list[str] = field(default_factory=list)
    rtsp_uri: str | None = None
    needs_auth: bool = False


async def interrogate_camera(
    xaddr: str,
    ip: str,
    scope_metadata: dict[str, str] | None = None,
    username: str | None = None,
    password: str | None = None,
) -> CameraInfo:
    """
    Connect to an ONVIF camera and retrieve its information.

    If auth is required and no credentials are provided, returns partial
    info from WS-Discovery scopes with needs_auth=True.
    """
    info = CameraInfo(ip=ip, xaddr=xaddr)

    # Pre-fill from scope metadata (available even without auth)
    if scope_metadata:
        info.model = scope_metadata.get("hardware")
        info.manufacturer = _guess_manufacturer(scope_metadata, xaddr)
        if not info.manufacturer and scope_metadata.get("name"):
            info.model = info.model or scope_metadata.get("name")

    cam = None
    try:
        from onvif import ONVIFCamera
        from pathlib import Path
        import onvif

        wsdl_dir = str(Path(onvif.__path__[0]) / "wsdl")

        parsed = urlparse(xaddr)
        port = parsed.port or 80
        user = username or "admin"
        passwd = password or ""

        cam = ONVIFCamera(ip, port, user, passwd, wsdl_dir=wsdl_dir)
        await cam.update_xaddrs()

        # Get device information (must create the service explicitly)
        devicemgmt = await cam.create_devicemgmt_service()
        device_info = await devicemgmt.GetDeviceInformation()

        # Some firmwares (notably Reolink) return literal placeholder strings
        # for fields they never filled in. Reject these so the manufacturer
        # falls back to MAC OUI / model name detection.
        info.manufacturer = _clean_field(device_info.Manufacturer, "manufacturer")
        info.model = _clean_field(device_info.Model, "model")
        info.firmware = _clean_field(device_info.FirmwareVersion, "firmware")
        info.serial_number = _clean_field(device_info.SerialNumber, "serial")
        info.hardware_id = _clean_field(device_info.HardwareId, "hardware")
        info.needs_auth = False

        # Get media profiles and stream URI
        try:
            media_service = await cam.create_media_service()
            profiles = await media_service.GetProfiles()

            if profiles:
                # Collect resolutions from all profiles
                for profile in profiles:
                    try:
                        enc = profile.VideoEncoderConfiguration
                        if enc and enc.Resolution:
                            res = f"{enc.Resolution.Width}x{enc.Resolution.Height}"
                            if res not in info.resolutions:
                                info.resolutions.append(res)
                    except Exception:
                        pass

                # Get RTSP URI from the first (usually best) profile
                stream_setup = {
                    "Stream": "RTP-Unicast",
                    "Transport": {"Protocol": "RTSP"},
                }
                uri_response = await media_service.GetStreamUri(
                    {"StreamSetup": stream_setup, "ProfileToken": profiles[0].token}
                )
                rtsp_uri = uri_response.Uri

                # Inject credentials into RTSP URI if needed.
                # URL-encode credentials so special chars like @ : / # don't
                # break the URI parser (e.g. Tapo passwords starting with @).
                if username and password and "@" not in rtsp_uri:
                    parsed_rtsp = urlparse(rtsp_uri)
                    encoded_user = quote(username, safe="")
                    encoded_pass = quote(password, safe="")
                    netloc = f"{encoded_user}:{encoded_pass}@{parsed_rtsp.hostname}"
                    if parsed_rtsp.port:
                        netloc += f":{parsed_rtsp.port}"
                    rtsp_uri = parsed_rtsp._replace(netloc=netloc).geturl()

                info.rtsp_uri = rtsp_uri

        except Exception as e:
            logger.warning("Failed to get media profiles for %s: %s", ip, e)

        logger.info(
            "Interrogated %s: %s %s (%s)",
            ip,
            info.manufacturer,
            info.model,
            info.rtsp_uri or "no stream URI",
        )

    except Exception as e:
        err_str = str(e).lower()
        if "not authorized" in err_str or "sender" in err_str or "authentication" in err_str:
            info.needs_auth = True
            logger.info("Camera %s requires authentication", ip)
        elif "module" in err_str and "onvif" in err_str:
            logger.error("onvif-zeep-async not installed: %s", e)
            info.needs_auth = False
        else:
            logger.warning("Failed to interrogate %s: %s", ip, e)
            info.needs_auth = True  # Assume auth needed if we can't connect

    finally:
        if cam is not None:
            try:
                await cam.close()
            except Exception:
                pass

    return info


_PLACEHOLDER_VALUES = {
    "manufacturer",
    "model",
    "firmware",
    "firmwareversion",
    "serial",
    "serialnumber",
    "hardware",
    "hardwareid",
    "unknown",
    "n/a",
    "none",
    "",
}


def _clean_field(value, field_kind: str) -> str | None:
    """
    Reject ONVIF fields that are obviously placeholders (e.g. Reolink
    firmware returns literal 'Manufacturer' strings).
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s.lower() in _PLACEHOLDER_VALUES:
        return None
    return s


def _guess_manufacturer(scope_metadata: dict[str, str], xaddr: str) -> str | None:
    """Try to guess manufacturer from scope data or xaddr patterns."""
    name = scope_metadata.get("name", "").lower()
    hardware = scope_metadata.get("hardware", "").lower()
    xaddr_lower = xaddr.lower()

    manufacturers = {
        "hikvision": ["hikvision", "hikv", "ds-2"],
        "dahua": ["dahua", "dh-"],
        "reolink": ["reolink", "rlc-", "rlc8", "rlc5"],
        "amcrest": ["amcrest"],
        "axis": ["axis"],
        "hanwha": ["hanwha", "wisenet"],
        "uniview": ["uniview"],
    }

    for mfr, patterns in manufacturers.items():
        for pattern in patterns:
            if pattern in name or pattern in hardware or pattern in xaddr_lower:
                return mfr.title()

    return None
