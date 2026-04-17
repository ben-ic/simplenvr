from __future__ import annotations

import ipaddress
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Camera(BaseModel):
    """
    A discovered or manually-added camera.

    Credential handling: `password` is marked `exclude=True` so it never
    appears in API responses (camera list/detail, scan snapshots,
    camera_updated WebSocket events). Internal code reads `.password`
    directly to build the authenticated RTSP URL on demand via
    `backend/rtsp_url.py::with_creds`. The DB column is the single
    source of truth for the secret; the `rtsp_uri` column stores a
    credential-free URL so the secret is never duplicated on disk.

    `exclude=True` only affects Pydantic's serializers (`model_dump`,
    `model_dump_json`). It does NOT affect `__repr__` / `__str__`, so
    any stray `logger.exception(cam)` or f-string containing the model
    would otherwise render the plaintext password. `__repr_args__` is
    overridden below to mask the field at the representation layer
    too — leaks a presence bit (`<set>` vs `None`) because serializers
    already leak that much, but never the value.
    """

    def __repr_args__(self):
        for key, value in super().__repr_args__():
            if key == "password":
                yield key, "<set>" if value else None
            else:
                yield key, value

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
    # Codec tags for the main and sub streams, captured from the ONVIF
    # VideoEncoderConfiguration we picked during interrogation.
    # Normalized upper-case ("H264", "H265", "MJPEG"). None = unknown
    # (pre-migration row, or a camera that exposes no encoder metadata).
    # Read at recorder spawn so the codec-aware branch in
    # recording/codec.py transcodes MJPEG sub-streams to H.264 instead
    # of stream-copying them into a fragmented MP4 (which yields black
    # files). Every other codec stream-copies as before.
    rtsp_codec: str | None = None
    substream_codec: str | None = None
    status: Literal["online", "offline", "needs_auth", "asleep"] = "online"
    username: str | None = None
    password: str | None = Field(default=None, exclude=True)
    name: str | None = None
    first_seen: datetime = Field(default_factory=utcnow)
    last_seen: datetime = Field(default_factory=utcnow)
    # Device type distinguishes direct IP cameras from hub devices
    # (Eufy HomeBase, Reolink Home Hub, Arlo SmartHub) and from the
    # cameras that live behind those hubs.
    #   - "camera"     : a direct IP camera with its own LAN presence
    #                    and RTSP server
    #   - "hub"        : a gateway device that is discovered on the LAN
    #                    but does not itself produce video; its purpose
    #                    is to aggregate cameras on sub-paths like
    #                    rtsp://<hub-ip>:8554/live0
    #   - "hub_camera" : a camera that lives behind a hub. Does not have
    #                    its own LAN address; its rtsp_uri points at the
    #                    hub. parent_hub_id references the hub's id.
    # Hub handling is Phase 2 work — the scanner and UI may treat
    # non-"camera" entries specially but must not crash on them.
    device_type: Literal["camera", "hub", "hub_camera"] = "camera"
    # For hub_camera entries: the id of the hub they live behind.
    # None for camera and hub entries. Used to group hub-cameras
    # together in the UI and to enumerate "my cameras" under a hub.
    parent_hub_id: str | None = None
    # DHCP-registered hostname (from reverse DNS on the camera's IP).
    # Not the same as Camera.name — this is what the device told the
    # router, whereas `name` is what the user called it. Used by the
    # setup card UI to help the user match the card to the physical
    # device on their wall.
    hostname: str | None = None
    # MAC address in lowercase colon-separated form (e.g. ec:71:db:12:34:56).
    # Used for OUI-based manufacturer identification and for the setup
    # card's "technical details" line.
    mac_address: str | None = None
    # Which signal source was used to populate manufacturer + model.
    # "onvif" = authenticated ONVIF GetDeviceInformation after sign-in.
    # "fingerprint" = unauthenticated fingerprint match (reverse DNS,
    #                 MAC OUI, ONVIF scopes, HTTP probe, etc.)
    # "manual" = user-supplied (future).
    # None = nothing has populated these fields yet.
    identification_source: Literal["onvif", "fingerprint", "manual"] | None = None
    # Opaque per-device identity string from ONVIF WS-Discovery
    # ProbeMatch (`EndpointReference/Address`, typically
    # `urn:uuid:<...>`). Stable across DHCP rebinds and reboots on
    # compliant ONVIF cameras. Primary unauthenticated signal for the
    # tiered reconciliation engine — an exact match on a singleton
    # offline candidate reconciles at EPR_EXACT confidence, no auth
    # required. None for RTSP-only discovery (Tapo/Eufy).
    endpoint_reference: str | None = None
    # Additional NIC MACs enumerated via ONVIF GetNetworkInterfaces at
    # authentication time. Dual-NIC cameras (wired + wireless) let a
    # camera rebind on its other interface after a router reboot — the
    # reconciliation engine checks both `mac_address` and `alt_macs`
    # when narrowing a new IP to an offline candidate.
    alt_macs: list[str] = Field(default_factory=list)
    # Recording-reliability circuit breaker state. Populated by the
    # chronic-failure handler in CameraRecorder when split-brain
    # restarts exceed CIRCUIT_BREAKER_THRESHOLD within
    # CIRCUIT_BREAKER_WINDOW_S; cleared by POST
    # /cameras/{id}/retry-main-stream.
    #   NULL    — no preference; follow the global
    #             record_substream_when_available setting.
    #   "main"  — user explicitly picked main. The breaker's UPDATE
    #             guards `WHERE ... OR recording_stream_override = 'main'`
    #             so a chronic-failure flip never clobbers this.
    #   "sub"   — record from the sub-stream regardless of the global
    #             setting. Written by the breaker on auto-fallback, or
    #             (future) by a manual user picker.
    recording_stream_override: Literal["main", "sub"] | None = None
    # Human-readable reason for the most recent auto-fallback, e.g.
    # "split-brain x3 in 10min". Cleared together with
    # recording_stream_override by the retry endpoint. NULL for cameras
    # that have never tripped the breaker.
    fallback_reason: str | None = None
    # Recorder-observed health. Distinct from `status`:
    #   status    = discovery-scan view (can we reach the RTSP port?)
    #   health    = recorder view (is the camera actually sending
    #               packets to the ffmpeg that's writing segments?)
    # A camera can be status=online + health=offline if its RTSP
    # socket accepts connections but the video feed has gone silent
    # (PoE brownout during IR cutoff, upstream buffer freeze, etc.).
    # Populated by CameraRecorder's staleness watchdog and emitted to
    # the frontend as camera_health events so the UI can surface
    # "last live Nm ago" on the live tile without waiting for the
    # next discovery scan.
    #   None     = recorder hasn't reported yet (just spawned, still
    #              in RTSP setup, or no recorder running for this cam)
    #   "ok"      = packets flowing normally
    #   "stalled" = no packets in 15-60s window (brief flap)
    #   "offline" = no packets in 60s+ (likely real outage)
    #   "unsupported_codec" = recorder refused to spawn because the
    #              camera's only available stream is a codec we cannot
    #              stream-copy (MJPEG) and the platform has no hardware
    #              H.264 encoder to transcode with. Only reachable on
    #              non-shipping platforms (bare-metal Linux without a GPU)
    #              since macOS / Windows always expose a hardware encoder.
    health: Literal["ok", "stalled", "offline", "unsupported_codec"] | None = None
    # ISO8601 timestamp of the most recent frame/packet the recorder
    # observed on this camera. Used by the UI to render "last live
    # Nm ago" during outages. Updated by the recorder's staleness
    # watchdog whenever it sees progress; only persisted in memory
    # (not in the sqlite cameras table) because it's meaningless
    # across process restarts.
    last_frame_at: datetime | None = None


class CameraAuthRequest(BaseModel):
    username: str
    password: str
    apply_to_manufacturer: bool = False


class CameraNameRequest(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("name must not be empty")
        return stripped


class ManualCameraRequest(BaseModel):
    """
    Request body for POST /api/cameras/manual — the escape hatch for
    cameras that didn't auto-discover. The user supplies network
    details + credentials and we probe known RTSP URL patterns until
    one yields a valid stream, then save the result as a Camera row
    with identification_source="manual".

    Fields:
      ip          required. IPv4 address string.
      port        optional, default 554.
      path        optional. A specific RTSP path to try first.
                  Leave blank to probe the brand's known path patterns.
      brand       optional. Brand name (e.g. "Reolink", "Dahua") used
                  to look up known RTSP path patterns. Case-insensitive.
      username    required. RTSP basic/digest auth username.
      password    required. RTSP basic/digest auth password.
      name        optional. Friendly name ("Driveway", "Back porch").
    """
    ip: str
    port: int = Field(default=554, ge=1, le=65535)
    path: str | None = None

    @field_validator("path")
    @classmethod
    def _validate_path(cls, v: str | None) -> str | None:
        if v is None:
            return None
        import re
        # Only allow safe RTSP path characters. Reject anything that
        # could inject userinfo (@), query strings (?), or fragments (#)
        # into the assembled rtsp:// URL.
        if not re.match(r"^/[A-Za-z0-9/_.\-]*$", v):
            raise ValueError(
                "RTSP path must start with / and contain only "
                "letters, digits, slashes, underscores, dots, and hyphens"
            )
        return v
    brand: str | None = None
    username: str
    password: str
    name: str | None = None

    @field_validator("ip")
    @classmethod
    def _validate_ip(cls, v: str) -> str:
        # Reject anything that isn't a parseable IP address. Without this,
        # POST /api/cameras/manual with a crafted `ip` could point the
        # scanner's ffprobe at arbitrary hostnames — an SSRF reachability
        # probe from the NVR host's network position. Loopback,
        # link-local, and multicast are additionally rejected: no real
        # camera lives at those addresses and they're the most
        # interesting targets for a LAN peer who can reach the API port.
        try:
            addr = ipaddress.ip_address(v)
        except ValueError as e:
            raise ValueError(f"ip must be a valid IP address: {e}") from e
        if addr.is_loopback or addr.is_link_local or addr.is_multicast:
            raise ValueError("ip must be a routable LAN address")
        return v


class ScanStatus(BaseModel):
    scanning: bool = False
    last_scan: datetime | None = None
    cameras_found: int = 0
    cameras_online: int = 0
    cameras_needs_auth: int = 0


class DiscoveryEvent(BaseModel):
    type: Literal[
        "camera_found",
        "camera_lost",
        "camera_updated",
        # Fired from api/cameras.py when a camera is deleted via
        # DELETE /api/cameras/{id}. Separate from camera_lost
        # (which means "went offline") — camera_deleted means
        # "remove this camera from the UI entirely, it's gone."
        # Was missing from this literal for some time and crashed
        # every delete emit with a Pydantic validation error —
        # caught by tests/test_discovery_event_literal.py.
        "camera_deleted",
        "scan_complete",
        "storage_updated",
        "settings_updated",
        "recording_started",
        "recording_stopped",
        "recordings_deleted",
        # Factory reset completed. Emitted after DB + on-disk recordings
        # are wiped so the UI can force a clean-state reload.
        "database_reset",
        # Recordings/events cleared without touching camera configuration.
        "data_cleared",
        "motion_started",
        "motion_ended",
        # Snapshot is the initial full-state payload a new WS client
        # receives right after it connects so the UI can render
        # immediately without waiting for the next delta event. Not a
        # change notification — a bootstrap marker. Emitted from
        # backend/api/ws.py on connection.
        "snapshot",
        # Detection pipeline v2: fired when the detector writes or
        # updates an object_class / object_confidence on a motion event,
        # so the Inbox can re-render the affected row with the new
        # label without a full refresh.
        "motion_event_updated",
        # High-priority audio classifier (YAMNet: gunshot / glass_break
        # / scream / siren) creates a brand-new motion_events row with
        # source='audio' and no preceding motion_started event. This
        # literal signals the frontend to insert the row rather than
        # look up an existing one. Keep in sync with frontend/src/types.ts.
        "motion_event_created",
        # Recorder health transitions (ok ↔ stalled ↔ offline).
        # Emitted by CameraRecorder's staleness watchdog ONLY on
        # state changes, not on every tick, so the event stream
        # stays quiet for healthy cameras. Payload:
        #   {camera_id, health, last_frame_at}
        "camera_health",
        # Per-camera audio pipeline came online. Emitted by
        # CameraRecorder._try_spawn_audio once the audio ffmpeg and
        # AudioBroadcaster are ready. AudioManager subscribes to this
        # event to wire up a per-camera YAMNet consumer on that
        # broadcaster. Payload: {camera_id}. Without this literal the
        # emit call raises a pydantic ValidationError and the audio
        # subsystem silently fails to hook up per-camera classifiers.
        "audio_available",
        # Counterpart to audio_available: fired when the audio ffmpeg
        # exits (camera lost, recorder stop, mid-stream crash) so
        # AudioManager can unhook its per-camera YAMNet consumer.
        # Emitted from _audio_pipe_reader's finally block.
        "audio_stopped",
    ]
    data: dict
    timestamp: datetime = Field(default_factory=utcnow)


class Settings(BaseModel):
    max_storage_gb: float = Field(default=50.0, ge=1.0, le=10000.0)
    segment_duration_minutes: int = Field(default=1, ge=1, le=60)
    recording_enabled: bool = True
    # Literal pins this to the exact allowed values. Previously a plain
    # str — a crafted value like `1,scale=100:-1,drawtext=text=...` would
    # be interpolated verbatim into FFmpeg's -vf filter graph
    # (build_unified_cmd in recording/codec.py), giving an attacker who
    # could reach POST /api/settings a filter-graph injection. Pydantic
    # now rejects anything outside this set before it reaches the DB.
    recording_fps: Literal["original", "10", "5", "2", "1", "0.5"] = "original"
    # Record from the camera's own low-bitrate sub-stream instead of the main
    # stream when the camera exposes one. Default False ("max quality") records
    # the full-resolution main stream, same as before. Flipping True trades
    # resolution for roughly 8x longer retention on the same disk — typical
    # Reolink sub-streams are ~0.8 Mbps at 640x480 vs ~6 Mbps at 2560x1920.
    # Cameras without a sub-stream (substream_uri is None) silently fall back
    # to the main stream regardless of this setting, so toggling is always
    # safe — the worst case on a sub-streamless camera is "same as today".
    record_substream_when_available: bool = False
    # Opt-in: ffprobe every segment after close and delete corrupt ones.
    # Off by default because it adds one ffprobe invocation per segment.
    validate_segments: bool = False
    # User override for the recordings directory. None means "use the default
    # under DATA_DIR/recordings". A custom path lets users point recording at
    # an external drive without relocating their settings database.
    recordings_path: str | None = None
    # Onboarding state. onboarding_completed flips to True after the user has
    # finished the first-launch flow (brand selection + credential entry).
    # declared_brands is the list of camera brands the user said they owned
    # during onboarding — used as a hint by the fingerprint identifier to
    # boost confidence for matching cameras, but never as a hard filter
    # (users forget what they have, inherit cameras, or add new brands
    # without revisiting this screen).
    onboarding_completed: bool = False
    declared_brands: list[str] = Field(default_factory=list)


class MotionEvent(BaseModel):
    id: str
    camera_id: str
    started_at: datetime
    ended_at: datetime | None = None
    thumbnail_url: str | None = None
    # Classifier fields — must stay in sync with frontend/src/types.ts
    # MotionEvent. object_confidence is kept internal-only (never rendered
    # in the UI per product rule "no scores"), but it's serialized on the
    # wire so the frontend's type stays honest and any future backend
    # refactor that swaps the hand-rolled _row_to_event dict in api/motion.py
    # for model_dump() does not silently drop these fields.
    object_class: Literal["person", "vehicle", "animal"] | None = None
    object_confidence: float | None = None
    summary: str | None = None
    description: str | None = None


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
