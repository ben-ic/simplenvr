"""
SimpleNVR sidecar logging configuration.

Two modes:

  * **Dev mode** (`SIMPLENVR_DEV=1`): stdout only, INFO level. `cargo tauri
    dev` pipes the sidecar's stdout/stderr to the dev terminal, and Ben-the-
    dev wants every `logger.info(...)` call visible. No file rotation —
    live observability wins.

  * **Production** (bundled Tauri / no dev env var): a rotating file
    handler in `platformdirs.user_log_dir("SimpleNVR", "SimpleNVR")` plus
    a stderr handler so crash output still reaches the parent Tauri
    process. 10 MB per file × 5 backups caps worst-case disk at ~50 MB.

This module is idempotent: if the root logger already has handlers
(e.g. uvicorn reload, test harness), we bump their level but do not
stack additional handlers.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import platformdirs


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_DATEFMT = "%H:%M:%S"

# 10 MB × 5 backups = ~50 MB cap on sidecar log disk usage.
_MAX_BYTES = 10 * 1024 * 1024
_BACKUP_COUNT = 5


def _silence_noisy_loggers() -> None:
    """Silence third-party HTTP client chatter.

    httpx + httpcore log every request at INFO, which drowns the log at
    ~20+ lines/sec once the HLS proxy is running. Raising them to WARNING
    leaves real errors (connection refused, timeouts) visible.
    """
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def configure_logging() -> None:
    """Configure the root logger for the sidecar process.

    Safe to call multiple times — subsequent calls only adjust levels,
    they do not re-add handlers.
    """
    dev_mode = os.environ.get("SIMPLENVR_DEV") == "1"
    root = logging.getLogger()

    if root.handlers:
        # Already configured — just normalize levels and return.
        target_level = logging.INFO if dev_mode else logging.WARNING
        for h in root.handlers:
            # The errors.log handler is pinned at ERROR regardless of
            # dev/prod re-entry — lowering it would dump INFO chatter
            # into the errors-only file and defeat the whole point.
            if getattr(h, "_simplenvr_errors_only", False):
                continue
            h.setLevel(target_level)
        root.setLevel(target_level)
        _silence_noisy_loggers()
        return

    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)

    if dev_mode:
        # Stdout-only, INFO level. `cargo tauri dev` surfaces this in the
        # terminal; no on-disk log to rotate.
        stream = logging.StreamHandler(stream=sys.stderr)
        stream.setFormatter(formatter)
        stream.setLevel(logging.INFO)
        root.addHandler(stream)
        root.setLevel(logging.INFO)
        _silence_noisy_loggers()
        return

    # Production: rotating file + stderr.
    log_dir = Path(platformdirs.user_log_dir("SimpleNVR", "SimpleNVR"))
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler: logging.Handler = RotatingFileHandler(
            log_dir / "sidecar.log",
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.INFO)
        root.addHandler(file_handler)
    except OSError as exc:
        # Disk-full / perms / read-only FS — don't take down the sidecar
        # over log setup. Fall back to stderr only; the bootstrap error
        # prints to stderr so it'll still show up in Tauri's sidecar
        # capture if anyone is looking.
        print(
            f"warning: could not open log file at {log_dir}/sidecar.log: {exc}",
            file=sys.stderr,
        )

    # Additive error-only sink. sidecar.log keeps everything at INFO+
    # for full forensic context; errors.log is a grep-free view of
    # just the things that went wrong, so on-call (or a bug reporter)
    # has a single file to tail.
    try:
        err_handler: logging.Handler = RotatingFileHandler(
            log_dir / "errors.log",
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        err_handler.setFormatter(formatter)
        err_handler.setLevel(logging.ERROR)
        # Tag so the re-entrant normalize-levels loop above skips us.
        err_handler._simplenvr_errors_only = True  # type: ignore[attr-defined]
        root.addHandler(err_handler)
    except OSError as exc:
        print(
            f"warning: could not open log file at {log_dir}/errors.log: {exc}",
            file=sys.stderr,
        )

    # Keep a stderr handler so the parent Tauri process's sidecar capture
    # still sees crash-adjacent output (uncaught exceptions, late warnings).
    stream = logging.StreamHandler(stream=sys.stderr)
    stream.setFormatter(formatter)
    stream.setLevel(logging.WARNING)
    root.addHandler(stream)

    root.setLevel(logging.INFO)
    _silence_noisy_loggers()
