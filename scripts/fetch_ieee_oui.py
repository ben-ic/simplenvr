#!/usr/bin/env python3
"""
Download the IEEE MAC OUI registry and generate a Python data module.

IEEE publishes the authoritative manufacturer → MAC prefix database
at https://standards-oui.ieee.org/. Three files cover the three
allocation sizes:

  oui.csv    — 24-bit OUI blocks (standard, ~16M addresses each)
  mam.csv    — 28-bit MA-M blocks (medium, ~1M addresses each)
  oui36.csv  — 36-bit MA-S blocks (small, ~4K addresses each)

Each file is public-domain tabular data published by IEEE as part of
their role as a standards registration authority. Redistribution and
inclusion in commercial software is unrestricted.

This script:
  1. Downloads all three CSV files from IEEE
  2. Parses each into (prefix, organization) pairs
  3. Writes a generated Python module (backend/discovery/_ieee_oui.py)
     containing three dicts: OUI_24, OUI_28, OUI_36
  4. The generated module is imported lazily by mac_lookup.py at
     runtime to expand coverage beyond our hand-maintained brand list

Run once periodically (e.g. monthly) to refresh the data.
Commit the generated _ieee_oui.py file to the repo — it's checked in,
not gitignored, so end users get the data without re-running this.
"""

from __future__ import annotations

import csv
import io
import logging
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT = REPO_ROOT / "backend" / "discovery" / "_ieee_oui.py"

SOURCES = [
    ("OUI_24", "https://standards-oui.ieee.org/oui/oui.csv", 6),
    ("OUI_28", "https://standards-oui.ieee.org/oui28/mam.csv", 7),
    ("OUI_36", "https://standards-oui.ieee.org/oui36/oui36.csv", 9),
]

USER_AGENT = "SimpleNVR-OUIFetcher/0.1"

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("fetch_ieee_oui")


def http_get(url: str, timeout: float = 60.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def parse_csv(raw: bytes, hex_digits: int) -> dict[str, str]:
    """
    Parse an IEEE CSV. Columns are:
      Registry, Assignment, Organization Name, Organization Address

    Assignment is the prefix as a hex string with no separators.
    We normalize to a colon-separated lowercase form matching what
    our mac_lookup.py expects (e.g. "ec:71:db" for 6-hex prefixes).
    """
    text = raw.decode("utf-8", errors="replace")
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if not header or len(header) < 3:
        log.warning("Unexpected CSV header: %r", header)
        return {}

    result: dict[str, str] = {}
    for row in reader:
        if len(row) < 3:
            continue
        assignment = row[1].strip().upper()
        org = row[2].strip()
        if len(assignment) != hex_digits:
            continue
        # Normalize to colon-separated lowercase: "EC71DB" → "ec:71:db"
        # For 7-digit MA-M prefixes: "EC71DB0" → "ec:71:db:0" (keep the
        # extra nibble as a trailing single hex char — lookup code
        # handles variable-length matching)
        prefix = ":".join(
            assignment[i : i + 2] for i in range(0, len(assignment) - 1, 2)
        ).lower()
        if len(assignment) % 2 == 1:
            # Odd number of hex chars → append the last char as a
            # 1-char segment. 28-bit and 36-bit prefixes have odd
            # lengths (7 and 9). Lookup code treats these as partial
            # byte matches via longest-prefix.
            prefix = prefix + ":" + assignment[-1].lower()
        result[prefix] = org

    return result


def write_module(data: dict[str, dict[str, str]]) -> None:
    """
    Write the parsed prefix→organization data as a Python module.

    Format is three top-level dicts (OUI_24, OUI_28, OUI_36), each
    mapping prefix string to IEEE-registered organization name. The
    consumer (mac_lookup.py) imports these lazily and does
    longest-prefix matching by trying 36-bit first, then 28-bit,
    then 24-bit.

    The file starts with a generated-file banner and total counts
    so it's obvious to human readers that it's machine-produced.
    """
    total = sum(len(d) for d in data.values())
    with OUTPUT.open("w", encoding="utf-8") as f:
        f.write('"""\n')
        f.write("Generated IEEE OUI database.\n\n")
        f.write("DO NOT EDIT BY HAND. Regenerate with:\n")
        f.write("    python scripts/fetch_ieee_oui.py\n\n")
        f.write("Source: https://standards-oui.ieee.org/\n")
        f.write("License: public domain (IEEE standards registry data)\n\n")
        f.write(f"Total prefixes: {total}\n")
        for name, d in data.items():
            f.write(f"  {name}: {len(d)}\n")
        f.write('"""\n\n')
        f.write("from __future__ import annotations\n\n")
        for name, d in data.items():
            f.write(f"{name}: dict[str, str] = {{\n")
            for prefix in sorted(d.keys()):
                # Order matters: backslashes FIRST, then quotes,
                # otherwise the backslashes we add for quote-escaping
                # get double-escaped. Also use repr() as a safer
                # alternative to hand-rolled escaping — it handles
                # embedded quotes, unicode, and control characters
                # correctly and produces a valid Python literal.
                f.write(f'    "{prefix}": {d[prefix]!r},\n')
            f.write("}\n\n")

    log.info("Wrote %s (%d total prefixes)", OUTPUT, total)


def main() -> int:
    data: dict[str, dict[str, str]] = {}
    for name, url, hex_digits in SOURCES:
        log.info("Downloading %s from %s", name, url)
        try:
            raw = http_get(url)
        except Exception as e:
            log.error("  failed: %s", e)
            return 1
        log.info("  %d bytes", len(raw))
        parsed = parse_csv(raw, hex_digits)
        log.info("  %d entries parsed", len(parsed))
        data[name] = parsed

    write_module(data)

    # Sanity-check: spot-check a few well-known OUIs
    oui_24 = data["OUI_24"]
    checks = {
        "ec:71:db": "Reolink / Baichuan (expected)",
        "10:2c:b1": "Eufy / Anker (expected)",
        "b0:19:21": "Tapo / TP-Link (expected)",
        "00:40:8c": "Axis Communications (expected)",
    }
    print("\nSpot checks against known brands:")
    for prefix, note in checks.items():
        org = oui_24.get(prefix, "(not found)")
        print(f"  {prefix}  {note}")
        print(f"           → {org}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
