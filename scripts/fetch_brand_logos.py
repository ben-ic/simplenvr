#!/usr/bin/env python3
"""
Download brand logos from Wikimedia Commons into the frontend assets
directory so the onboarding + discovery UI can render them.

Reads the logo_source_url field from every entry in
backend/discovery/fingerprints.py. Each URL is a Wikimedia Commons
File: page (e.g. https://commons.wikimedia.org/wiki/File:Reolink_logo.svg).
This script queries the Wikimedia API to get the direct image URL
for each file and downloads the SVG to
frontend/src/assets/brand-logos/<slug>.svg.

Why not hard-code direct file URLs? Because Wikimedia's direct URLs
include MD5 hash subdirectories that change when the file is
re-uploaded (e.g. /8/80/Reolink_logo.svg). The File: page URL is
stable; the underlying direct URL is not. The API is the right
indirection.

The script is deliberately non-fatal for individual missing files —
any brand whose logo can't be fetched is skipped and reported,
and the UI gracefully falls back to a text-only tile for that brand.
This matters because the research agent may have populated a few
URLs that don't actually exist on Commons.

Usage: scripts/fetch_brand_logos.py
"""

from __future__ import annotations

import json
import logging
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from backend.discovery.fingerprints import FINGERPRINTS  # noqa: E402

OUT_DIR = REPO_ROOT / "frontend" / "src" / "assets" / "brand-logos"
WIKIMEDIA_API = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = "SimpleNVR-LogoFetcher/0.1 (https://github.com/ben-ic/simplenvr)"

logging.basicConfig(
    level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr
)
log = logging.getLogger("fetch_brand_logos")


def slugify(brand: str) -> str:
    """Convert a brand name to a URL-safe filename slug."""
    s = brand.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s


def extract_filename(page_url: str) -> str | None:
    """Parse 'File:Something.svg' out of a Commons page URL."""
    # Match both /wiki/File:Foo.svg and /wiki/Special:FilePath/Foo.svg
    m = re.search(r"/(?:wiki|w)/(?:File|Special:FilePath)[:/]([^?#]+)", page_url)
    if m:
        return urllib.parse.unquote(m.group(1))
    # Some URLs are already Special:Redirect/file/Foo.svg
    m = re.search(r"Special:Redirect/file/([^?#]+)", page_url)
    if m:
        return urllib.parse.unquote(m.group(1))
    return None


def http_get(url: str, timeout: float = 10.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def resolve_file_url(filename: str) -> str | None:
    """
    Query the Wikimedia API for the direct file URL.

    Uses action=query&titles=File:<name>&prop=imageinfo&iiprop=url,
    which returns a JSON object with the current file URL under
    query.pages.<pageid>.imageinfo[0].url. The key is "missing" when
    the file doesn't exist on Commons.
    """
    params = {
        "action": "query",
        "format": "json",
        "titles": f"File:{filename}",
        "prop": "imageinfo",
        "iiprop": "url",
        "redirects": "1",
    }
    url = f"{WIKIMEDIA_API}?{urllib.parse.urlencode(params)}"
    try:
        data = json.loads(http_get(url))
    except Exception as e:
        log.warning("  api query failed for %s: %s", filename, e)
        return None
    pages = data.get("query", {}).get("pages", {})
    for page in pages.values():
        if "missing" in page:
            return None
        infos = page.get("imageinfo") or []
        if infos and "url" in infos[0]:
            return infos[0]["url"]
    return None


def fetch_logo(brand: str, page_url: str) -> tuple[str, bytes] | None:
    filename = extract_filename(page_url)
    if not filename:
        log.warning("%s: could not parse filename from %s", brand, page_url)
        return None
    direct = resolve_file_url(filename)
    if not direct:
        log.warning("%s: file not found on Commons (%s)", brand, filename)
        return None
    try:
        data = http_get(direct)
    except urllib.error.HTTPError as e:
        log.warning("%s: HTTP %s fetching %s", brand, e.code, direct)
        return None
    except Exception as e:
        log.warning("%s: fetch failed: %s", brand, e)
        return None
    # Sanity check: ensure it's actually an SVG. Strip a UTF-8 BOM if
    # present (some SVGs on Commons are served with BOM) before the
    # startswith check, otherwise we'd false-reject legitimate files.
    stripped = data.lstrip().lstrip(b"\xef\xbb\xbf").lstrip()
    if not stripped.startswith(b"<?xml") and not stripped.startswith(b"<svg"):
        log.warning(
            "%s: downloaded file does not look like an SVG (first bytes: %r)",
            brand,
            data[:40],
        )
        return None
    return (filename, data)


# ── Simple Icons fallback ──────────────────────────────────────────────
# simpleicons.org hosts thousands of brand SVGs under CC0. Much better
# coverage for tech brands than Wikimedia Commons, and the licensing is
# strictly more permissive (no attribution requirement at all). Used as
# a fallback when the Wikimedia URL is missing or doesn't resolve.
SIMPLEICONS_BASE = "https://cdn.simpleicons.org"

# Map our internal brand names to the simpleicons slug where it differs
# from the obvious lowercased form. Many brands use a direct slug match
# (e.g. "reolink" → https://cdn.simpleicons.org/reolink), which we try
# first. This table handles the renames.
SIMPLEICONS_SLUGS: dict[str, str] = {
    "Reolink": "reolink",
    "Eufy": "eufy",
    "TP-Link Tapo": "tp-link",
    "TP-Link Kasa": "tp-link",
    "Wyze": "wyze",
    "Arlo": "arlo",
    "Google Nest": "googlenest",
    "Amazon Ring": "ring",
    "Ubiquiti UniFi Protect": "ubiquiti",
    "Hikvision": "hikvision",
    "Dahua": "dahuatechnology",
    "Axis Communications": "axiscommunications",
    "Lorex": "lorex",
    "Swann": "swann",
    "Foscam": "foscam",
    "Bosch Security Systems": "bosch",
    "Hanwha Vision (Wisenet)": "hanwha",
    "Avigilon": "avigilon",
    "Vivotek": "vivotek",
    "Mobotix": "mobotix",
    "Xiaomi": "xiaomi",
    "Ezviz": "ezviz",
    "Eufy HomeBase 2": "eufy",
    "Reolink Home Hub / NVR": "reolink",
    "Arlo SmartHub": "arlo",
}


def fetch_from_simpleicons(brand: str) -> tuple[str, bytes] | None:
    """Try simpleicons.org as a fallback logo source."""
    slug = SIMPLEICONS_SLUGS.get(brand)
    if not slug:
        # Derive a fallback slug from the brand name
        slug = slugify(brand).replace("-", "")
    url = f"{SIMPLEICONS_BASE}/{slug}"
    try:
        data = http_get(url, timeout=8.0)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        log.warning("  simpleicons HTTP %s for %s", e.code, slug)
        return None
    except Exception as e:
        log.warning("  simpleicons fetch failed for %s: %s", slug, e)
        return None
    stripped = data.lstrip().lstrip(b"\xef\xbb\xbf").lstrip()
    if not stripped.startswith(b"<?xml") and not stripped.startswith(b"<svg"):
        return None
    return (f"simpleicons:{slug}", data)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    log.info("Writing logos into %s", OUT_DIR)

    manifest: dict[str, str] = {}
    success: list[str] = []
    failure: list[str] = []

    # Dedup the per-brand write loop so hub entries that share a logo
    # with their corresponding camera brand (e.g. "Eufy HomeBase 2"
    # using the same Eufy logo) don't trigger duplicate downloads.
    seen_slugs: set[str] = set()

    for fp in FINGERPRINTS:
        log.info("%s: resolving logo", fp.brand)

        # 1. Try the Wikimedia URL the fingerprint database specified
        result = None
        if fp.logo_source_url:
            result = fetch_logo(fp.brand, fp.logo_source_url)

        # 2. Fall back to simpleicons.org (CC0, much better tech-brand
        #    coverage than Wikimedia)
        if result is None:
            log.info("  trying simpleicons.org fallback")
            result = fetch_from_simpleicons(fp.brand)

        if result is None:
            log.warning("  no logo source worked, will use text tile")
            failure.append(fp.brand)
            continue

        _source, data = result
        slug = slugify(fp.brand)
        if slug in seen_slugs:
            # Already wrote this exact filename — still record the
            # brand → filename mapping for the manifest.
            manifest[fp.brand] = f"{slug}.svg"
            success.append(fp.brand)
            log.info("  -> reused %s.svg", slug)
            continue
        out_path = OUT_DIR / f"{slug}.svg"
        out_path.write_bytes(data)
        manifest[fp.brand] = f"{slug}.svg"
        seen_slugs.add(slug)
        success.append(fp.brand)
        log.info("  -> wrote %s (%d bytes)", out_path.name, len(data))

    # Write a manifest mapping brand name -> logo filename so the
    # frontend can import it without hardcoding the slug-to-brand
    # mapping in JS.
    manifest_path = OUT_DIR / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    log.info("Wrote manifest %s", manifest_path.name)

    print()
    print(f"Success ({len(success)}): {', '.join(success)}")
    print(f"Failure ({len(failure)}): {', '.join(failure)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
