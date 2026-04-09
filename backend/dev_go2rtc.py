"""
Bare-python dev-mode go2rtc spawner.

In bundled/Tauri mode, the Rust shell spawns go2rtc as a sidecar before
Python and hands Python the admin + RTSP URLs via env vars
(SIMPLENVR_GO2RTC_URL, SIMPLENVR_GO2RTC_RTSP_URL). In bare-python dev
mode (`python -m backend.main` from a shell), there's no Tauri shell,
so nothing starts go2rtc and the backend silently falls back to direct
camera URLs.

This module closes the gap: on bare-python startup (SIMPLENVR_DEV=1 and
SIMPLENVR_GO2RTC_URL unset), the backend finds the bundled go2rtc
binary in the repo, spawns it as a subprocess, waits for the admin API
to answer, and sets the env vars so the rest of the code sees the same
environment as Tauri mode. Cleanup on Python exit is handled by the
atexit hook in main.py plus the existing process_cleanup.kill_orphan_
go2rtc() sweep at startup.

NO-OP when:
  - SIMPLENVR_GO2RTC_URL is already set (Tauri mode or manual override)
  - Not in SIMPLENVR_DEV=1 mode (bundled production run without Tauri
    is unsupported — the binary likely isn't where we expect anyway)

The hardcoded ports (1984 admin, 8554 RTSP) and YAML config match
src-tauri/src/lib.rs:GO2RTC_API_BASE / GO2RTC_RTSP_BASE / write_go2rtc_
config. If you change them here, change them there in lockstep.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import subprocess
from pathlib import Path
from typing import Optional

from . import go2rtc_client
from .config import DATA_DIR
from .process_cleanup import kill_orphan_go2rtc

logger = logging.getLogger(__name__)

# Hardcoded — see module docstring on keeping in lockstep with Rust.
# Port choice rationale: we moved off the default 1984 (commonly
# occupied by other tools, Orwell homage aside) to a higher port
# unlikely to collide with any well-known service on
# Windows/macOS/Linux. 58554 is adjacent to RTSP 8554 (mental
# grouping) and in the unassigned IANA range. If either of these
# changes, update:
#   1. backend/dev_go2rtc.py:_GO2RTC_API_URL / _GO2RTC_RTSP_URL
#   2. backend/dev_go2rtc.py:_GO2RTC_YAML (api.listen / rtsp.listen)
#   3. src-tauri/src/lib.rs:GO2RTC_API_BASE / GO2RTC_RTSP_BASE
#   4. src-tauri/src/lib.rs:write_go2rtc_config (the YAML string)
# The frontend NEVER references these URLs directly — all live
# preview traffic goes through backend/api/streams.py proxies which
# read the URL from SIMPLENVR_GO2RTC_URL.
_GO2RTC_API_URL = "http://127.0.0.1:58581"
_GO2RTC_RTSP_URL = "rtsp://127.0.0.1:58554"

_GO2RTC_YAML = """\
log:
  level: info
api:
  listen: 127.0.0.1:58581
rtsp:
  listen: 127.0.0.1:58554
"""

# Running subprocess handle. Exposed to main.py's atexit hook via the
# shutdown() helper below so the child dies with its parent.
_proc: Optional[subprocess.Popen] = None


def _find_go2rtc_binary() -> Optional[Path]:
    """Locate the go2rtc binary in the repo layout.

    Priority order:
      1. src-tauri/target/debug/go2rtc    — cargo build artifact, native arch
      2. src-tauri/target/release/go2rtc  — release build artifact
      3. src-tauri/binaries/go2rtc-{triple} — externalBin source files

    The cargo build artifacts are preferred because they're guaranteed
    to match the host's native architecture (they were built on this
    machine). The externalBin files in binaries/ are the ones that get
    copied into bundled .app/.exe and cover all target triples.

    Returns None if no executable is found. Caller logs a clear warning
    with recovery instructions.
    """
    repo_root = Path(__file__).resolve().parent.parent
    candidates: list[Path] = [
        repo_root / "src-tauri" / "target" / "debug" / "go2rtc",
        repo_root / "src-tauri" / "target" / "release" / "go2rtc",
    ]

    # Prefer the platform-matched externalBin as a fallback. On ARM64
    # Macs we also accept the x86_64 binary (runs fine under Rosetta)
    # as a last resort, since historically only the x86_64 variant
    # was shipped.
    sys_name = platform.system()
    machine = platform.machine().lower()
    triples: list[str] = []
    if sys_name == "Darwin":
        if machine in ("arm64", "aarch64"):
            triples.extend(["aarch64-apple-darwin", "x86_64-apple-darwin"])
        else:
            triples.append("x86_64-apple-darwin")
    elif sys_name == "Linux":
        triples.append("x86_64-unknown-linux-gnu")
    elif sys_name == "Windows":
        if machine in ("arm64", "aarch64"):
            triples.append("aarch64-pc-windows-msvc.exe")
        else:
            triples.append("x86_64-pc-windows-msvc.exe")

    for triple in triples:
        candidates.append(
            repo_root / "src-tauri" / "binaries" / f"go2rtc-{triple}"
        )

    for path in candidates:
        if path.exists() and os.access(path, os.X_OK):
            return path
    return None


async def ensure_running() -> bool:
    """Spawn and wait for go2rtc if bare-python dev mode needs it.

    Sequence:
      1. No-op if SIMPLENVR_GO2RTC_URL is already set (Tauri mode, or
         a previous call on this process already spawned go2rtc).
      2. No-op if not in SIMPLENVR_DEV=1 mode (no unsupported secret
         spawning in production binaries).
      3. Sweep orphan go2rtc processes from previous crashed sessions.
      4. Locate the binary; bail with a loud warning if not found.
      5. Write the YAML config to DATA_DIR/go2rtc.yaml.
      6. Spawn the subprocess inheriting the current process group so
         Ctrl-C reaches it and stdout/stderr stay with the dev terminal.
      7. Set SIMPLENVR_GO2RTC_URL + SIMPLENVR_GO2RTC_RTSP_URL in
         os.environ so the existing go2rtc_client code picks them up.
      8. Poll /api/streams until it answers (10s budget, matching the
         Tauri-side timeout).

    Returns True if go2rtc is reachable at the hardcoded URLs afterward,
    False on any failure (including already-running Tauri mode case,
    which is technically a success but from this helper's perspective
    a no-op). Failure is non-fatal — the backend continues with direct
    camera URLs exactly as it did before this module existed.
    """
    global _proc

    # Already provisioned by a parent (Tauri shell, or an earlier call
    # on this same process). Treat as success and return without
    # touching anything.
    if os.environ.get("SIMPLENVR_GO2RTC_URL"):
        logger.debug(
            "SIMPLENVR_GO2RTC_URL already set, dev spawner is a no-op"
        )
        return True

    # Only spawn in explicit dev mode. A production bundle without the
    # tether env var is an unsupported config — we don't want to
    # quietly start a binary that may not even be present in the
    # install. Let go2rtc_client.is_enabled() return False and the
    # rest of the backend fall back to direct camera URLs, which is
    # the documented graceful-fallback behavior.
    if os.environ.get("SIMPLENVR_DEV") != "1":
        return False

    binary = _find_go2rtc_binary()
    if binary is None:
        logger.warning(
            "go2rtc binary not found in the repo. Looked in "
            "src-tauri/target/debug, src-tauri/target/release, and "
            "src-tauri/binaries/. Dev-mode HLS live preview will not "
            "work. Either run `cargo tauri build` once to produce a "
            "native binary, or copy one from src-tauri/binaries/ into "
            "src-tauri/target/debug/go2rtc."
        )
        return False

    # Orphan cleanup: a previous bare-python session may have crashed
    # without running its atexit shutdown, leaving go2rtc reparented to
    # init. process_cleanup.kill_orphan_go2rtc() handles that — it
    # excludes our own ppid and our parent's ppid, so this is safe to
    # run even when the Tauri shell is our actual parent (the env var
    # guard above would have short-circuited this path anyway, but
    # belt-and-suspenders).
    try:
        killed = kill_orphan_go2rtc()
        if killed:
            logger.warning(
                "Killed %d orphan go2rtc process(es) from previous "
                "bare-python session(s) before spawning a fresh one.",
                killed,
            )
    except Exception as e:
        logger.debug("orphan go2rtc sweep failed (non-fatal): %s", e)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    config_path = DATA_DIR / "go2rtc.yaml"
    try:
        config_path.write_text(_GO2RTC_YAML)
    except Exception as e:
        logger.error("failed to write go2rtc.yaml: %s", e)
        return False

    logger.info("spawning dev go2rtc: %s -c %s", binary, config_path)
    try:
        # Inherit the parent process group (NO start_new_session) so
        # the dev terminal's Ctrl-C reaches go2rtc via the default
        # signal propagation. The shutdown() hook below is the belt;
        # atexit in main.py is the suspenders.
        _proc = subprocess.Popen(
            [str(binary), "-c", str(config_path)],
            stdin=subprocess.DEVNULL,
            # go2rtc logs to stderr; surface it to the dev terminal so
            # Ben can see startup errors without hunting in a separate
            # log file. stdout is almost always empty.
            stdout=subprocess.DEVNULL,
            stderr=None,  # inherit parent stderr
        )
    except Exception as e:
        logger.error("go2rtc subprocess spawn failed: %s", e)
        return False

    # Set env vars BEFORE the readiness wait so wait_for_ready picks
    # up api_base() from the same env var the rest of the code reads.
    os.environ["SIMPLENVR_GO2RTC_URL"] = _GO2RTC_API_URL
    os.environ["SIMPLENVR_GO2RTC_RTSP_URL"] = _GO2RTC_RTSP_URL

    # Wait for the admin API. 10s budget mirrors the Tauri side
    # (GO2RTC_HEALTH_ATTEMPTS * GO2RTC_HEALTH_INTERVAL in lib.rs).
    ready = await go2rtc_client.wait_for_ready(timeout_s=10.0)
    if not ready:
        logger.error(
            "dev go2rtc spawned (pid=%d) but /api/streams did not "
            "answer within 10s. Killing the half-started child and "
            "falling back to direct camera URLs.",
            _proc.pid,
        )
        shutdown()
        # Revert env vars so downstream code correctly reports that
        # go2rtc is unavailable instead of quietly retrying against
        # a dead admin API.
        os.environ.pop("SIMPLENVR_GO2RTC_URL", None)
        os.environ.pop("SIMPLENVR_GO2RTC_RTSP_URL", None)
        return False

    logger.info("dev go2rtc ready (pid=%d)", _proc.pid)
    return True


def shutdown() -> None:
    """Terminate the dev go2rtc subprocess if it's running.

    Idempotent — safe to call multiple times. Called from the main.py
    atexit handler and from the FastAPI lifespan shutdown path.
    """
    global _proc
    if _proc is None:
        return
    try:
        if _proc.poll() is None:
            pid = _proc.pid
            logger.info("stopping dev go2rtc (pid=%d)", pid)
            _proc.terminate()
            try:
                _proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                logger.warning(
                    "dev go2rtc (pid=%d) did not exit in 3s, killing",
                    pid,
                )
                try:
                    _proc.kill()
                    _proc.wait(timeout=1.0)
                except Exception:
                    pass
    except Exception as e:
        logger.warning("error stopping dev go2rtc: %s", e)
    _proc = None
