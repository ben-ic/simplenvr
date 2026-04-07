from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Camera(BaseModel):
    id: str
    ip: str
    xaddr: str
    manufacturer: str | None = None
    model: str | None = None
    firmware: str | None = None
    serial_number: str | None = None
    hardware_id: str | None = None
    resolutions: list[str] = Field(default_factory=list)
    rtsp_uri: str | None = None
    substream_uri: str | None = None
    status: Literal["online", "offline", "needs_auth"] = "online"
    username: str | None = None
    password: str | None = None
    name: str | None = None
    first_seen: datetime = Field(default_factory=utcnow)
    last_seen: datetime = Field(default_factory=utcnow)


class CameraAuthRequest(BaseModel):
    username: str
    password: str
    apply_to_manufacturer: bool = False


class CameraNameRequest(BaseModel):
    name: str


class ScanStatus(BaseModel):
    scanning: bool = False
    last_scan: datetime | None = None
    cameras_found: int = 0
    cameras_online: int = 0
    cameras_needs_auth: int = 0


class DiscoveryEvent(BaseModel):
    type: Literal[
        "snapshot",
        "camera_found",
        "camera_lost",
        "camera_updated",
        "scan_complete",
        "storage_updated",
        "settings_updated",
        "recording_started",
        "recording_stopped",
        "motion_started",
        "motion_ended",
    ]
    data: dict
    timestamp: datetime = Field(default_factory=utcnow)


class Settings(BaseModel):
    max_storage_gb: float = 10.0
    segment_duration_minutes: int = 1
    recording_enabled: bool = True
    recording_fps: str = "original"  # "original" | "10" | "5" | "2" | "1" | "0.5"
    # Opt-in: ffprobe every segment after close and delete corrupt ones.
    # Off by default because it adds one ffprobe invocation per segment.
    validate_segments: bool = False


class MotionEvent(BaseModel):
    id: str
    camera_id: str
    started_at: datetime
    ended_at: datetime | None = None
    thumbnail_url: str | None = None


class StorageStats(BaseModel):
    """Retention-aware storage stats for the dashboard.

    Reflects the circular-buffer model: oldest segments are overwritten when
    the configured budget fills, so the relevant "time" is budget / bitrate,
    not free-disk / bitrate.
    """

    storage_budget_gb: float
    current_usage_gb: float
    bitrate_gb_per_day: float | None = None
    retention_days: float | None = None
    free_disk_gb: float
    ready: bool = False


class StorageStatus(BaseModel):
    used_bytes: int = 0
    limit_bytes: int = 0
    free_bytes: int = 0
    total_bitrate_bps: int = 0  # sum across all recording cameras
    seconds_remaining: int = 0
    cameras_recording: int = 0
    per_camera_bytes: dict[str, int] = Field(default_factory=dict)
