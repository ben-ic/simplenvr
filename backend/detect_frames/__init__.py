"""Detect-role ffmpeg subsystem: per-camera frame source for the detector.

Parallel to `backend.recording.camera_recorder`, but decoded (RGB24 rawvideo),
downscaled to 640 wide, and rate-limited to a small fps (default 2). Each
camera has one `DetectFfmpegSource` feeding one `NewestFrameSlot` that the
detect task consumes newest-frame-only.
"""

from .ffmpeg_source import DetectFfmpegSource
from .shm_ring import NewestFrameSlot

__all__ = ["DetectFfmpegSource", "NewestFrameSlot"]
