"""
Resolve FFmpeg and ffprobe binary paths at runtime.

In dev: SIMPLENVR_FFMPEG_BIN / SIMPLENVR_FFPROBE_BIN env vars are typically
unset, so we fall back to "ffmpeg" / "ffprobe" from PATH.

In prod: the Tauri Rust shell resolves the bundled binary paths and sets
both env vars before spawning the Python sidecar.

Results are cached on first call since env vars don't change after process
start.
"""

from __future__ import annotations

import os
from functools import lru_cache


@lru_cache(maxsize=1)
def get_ffmpeg() -> str:
    return os.environ.get("SIMPLENVR_FFMPEG_BIN", "ffmpeg")


@lru_cache(maxsize=1)
def get_ffprobe() -> str:
    return os.environ.get("SIMPLENVR_FFPROBE_BIN", "ffprobe")
