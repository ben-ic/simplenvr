"""Persistence paths for the HomeKit bridge.

HAP-python owns its own state format (pairing keys, paired-client
public keys, bridge identity). We hand it a file path inside
`DATA_DIR/ecosystem/homekit/` and let it manage the contents. Deleting
`accessory.state` forces a clean-slate re-pair — that's the user-visible
reset path (Settings panel lands in M5).

The HAP setup pincode is a separate concern: HAP-python does NOT persist
it (only crypto keys + paired clients), which would otherwise mean every
sidecar restart rotates the pair code. Commercial HomeKit accessories
print a static pincode on a label; users expect the same code each time
they open Settings. We store it in `pincode.txt` alongside accessory.state
and pass it back into `AccessoryDriver(pincode=...)` at construction.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from pyhap.util import generate_pincode

from ...config import DATA_DIR

logger = logging.getLogger(__name__)

_PINCODE_RE = re.compile(r"^\d{3}-\d{2}-\d{3}$")


def homekit_state_dir() -> Path:
    """Resolve `DATA_DIR/ecosystem/homekit/` and ensure it exists."""
    d = DATA_DIR / "ecosystem" / "homekit"
    d.mkdir(parents=True, exist_ok=True)
    return d


def accessory_state_path() -> Path:
    """Path to HAP-python's bridge state file (pairing keys + clients)."""
    return homekit_state_dir() / "accessory.state"


def pincode_path() -> Path:
    """Path to the persisted HAP setup pincode."""
    return homekit_state_dir() / "pincode.txt"


def load_or_generate_pincode() -> bytes:
    """Return the persistent HAP setup pincode as ASCII bytes.

    First call writes a fresh pincode via `pyhap.util.generate_pincode()`;
    subsequent calls read the same value back. A malformed on-disk
    pincode (hand-edited or truncated) is logged and regenerated rather
    than raising — the bridge must always start.

    File is written 0600: the pincode is only meaningful during the
    pair-setup window, but a local attacker who reads it before the user
    has paired could hijack the pair. After pair succeeds the code is
    inert, but the conservative perms cost nothing.
    """
    path = pincode_path()
    if path.exists():
        try:
            code = path.read_text(encoding="ascii").strip()
            if _PINCODE_RE.fullmatch(code):
                return code.encode("ascii")
            logger.warning(
                "HomeKit pincode.txt malformed (%r); regenerating.", code,
            )
        except OSError as exc:
            logger.warning(
                "HomeKit pincode.txt unreadable (%s); regenerating.", exc,
            )

    pincode = generate_pincode()  # bytes like b"123-45-678"
    try:
        path.write_text(pincode.decode("ascii") + "\n", encoding="ascii")
        os.chmod(path, 0o600)
    except OSError as exc:
        # Non-fatal: bridge can still run with an in-memory pincode,
        # but the user will see a rotated code on next restart.
        logger.error(
            "HomeKit pincode persist failed (%s); code will rotate on "
            "next restart until the write succeeds.", exc,
        )
    return pincode
