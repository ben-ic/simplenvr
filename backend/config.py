"""
SimpleNVR runtime configuration.

Data directories are resolved at import time based on the environment:
  1. SIMPLENVR_DATA_DIR env var (explicit override, set by the Tauri Rust
     shell in production bundles)
  2. SIMPLENVR_DEV=1 env var → ./data/ relative to the project root (local
     development)
  3. platformdirs.user_data_dir("SimpleNVR", "SimpleNVR") (default for bundled
     apps — resolves to ~/Library/Application Support/SimpleNVR on macOS,
     %APPDATA%/SimpleNVR on Windows, ~/.local/share/SimpleNVR on Linux)

The HTTP server starting port is exposed here as DEFAULT_START_PORT; the
actual runtime port is selected at launch time by port_finder.pick_port(),
so importing this module does not bind any sockets.
"""

from __future__ import annotations

import os
from pathlib import Path

import platformdirs

from .port_finder import DEFAULT_START_PORT as _DEFAULT_START_PORT


def resolve_data_dir() -> Path:
    """
    Determine where SimpleNVR stores its database, recordings, and motion
    thumbnails. See module docstring for precedence rules.
    """
    custom = os.environ.get("SIMPLENVR_DATA_DIR")
    if custom:
        return Path(custom)

    if os.environ.get("SIMPLENVR_DEV") == "1":
        return Path(__file__).resolve().parent.parent / "data"

    return Path(platformdirs.user_data_dir("SimpleNVR", "SimpleNVR"))


DATA_DIR = resolve_data_dir()
DB_PATH = DATA_DIR / "simplenvr.db"
RECORDINGS_DIR = DATA_DIR / "recordings"
MOTION_THUMBNAILS_DIR = DATA_DIR / "motion_thumbnails"

# Motion detection
MOTION_DEBOUNCE_SECONDS = 5.0
MOTION_SCENE_THRESHOLD = 0.04

# Discovery
SCAN_INTERVAL = 30.0  # seconds between discovery scans
PROBE_TIMEOUT = 5.0   # seconds to wait for WS-Discovery responses

# HTTP server — actual port is chosen at launch by port_finder.pick_port()
DEFAULT_START_PORT = _DEFAULT_START_PORT

# Recording settings
JANITOR_INTERVAL = 60.0  # seconds — periodic janitor safety net
FFMPEG_RESTART_BACKOFF = [5, 15, 30, 60]  # seconds between restart attempts
BITRATE_ROLLING_WINDOW = 5  # segments per camera for bitrate average
