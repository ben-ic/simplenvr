"""
Single source of truth for RTSP URL credential handling.

Storage rule: the `cameras.rtsp_uri` and `cameras.substream_uri` columns
store a credential-free URL (e.g. `rtsp://10.0.0.13:554/stream1`). Username
and password live only in `cameras.username` and `cameras.password`. At
every use site that actually connects to the camera (recorder, go2rtc
registration, verification probes) the authenticated URL is rebuilt on
demand via `with_creds()` or `authed_uri()`.

This avoids duplicating the secret across two columns — which was both a
security smell (two places to encrypt, two places that leaked creds in the
go2rtc config file) and a correctness smell (nothing enforced that the URL
column and the password column stayed in sync on rotation).
"""

from __future__ import annotations

from urllib.parse import quote, urlparse, urlunparse


def strip_creds(uri: str | None) -> str | None:
    """Return `uri` with any embedded userinfo removed. None/empty pass through."""
    if not uri:
        return uri
    parsed = urlparse(uri)
    if "@" not in (parsed.netloc or ""):
        return uri
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunparse(parsed._replace(netloc=host))


def with_creds(
    uri: str | None, username: str | None, password: str | None
) -> str | None:
    """
    Return `uri` with `username:password` URL-encoded into the authority.

    - Pass-through if `uri` is None/empty, if either credential is missing,
      or if `uri` already carries userinfo (caller is responsible in that
      edge case — this should not happen with storage discipline).
    """
    if not uri or not username or not password:
        return uri
    parsed = urlparse(uri)
    if "@" in (parsed.netloc or ""):
        return uri
    host = parsed.hostname or ""
    netloc = f"{quote(username, safe='')}:{quote(password, safe='')}@{host}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


def authed_uri(camera) -> str | None:
    """Convenience: build the authenticated RTSP URL for a Camera model."""
    return with_creds(camera.rtsp_uri, camera.username, camera.password)


def authed_substream_uri(camera) -> str | None:
    """Authenticated RTSP URL for the camera's sub-stream, or None if the
    camera doesn't expose one. Same credential handling as `authed_uri`."""
    return with_creds(camera.substream_uri, camera.username, camera.password)
