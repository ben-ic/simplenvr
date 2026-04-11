export interface Camera {
  id: string;
  ip: string;
  xaddr: string;
  manufacturer: string | null;
  model: string | null;
  firmware: string | null;
  serial_number: string | null;
  hardware_id: string | null;
  resolutions: string[];
  rtsp_uri: string | null;
  // Substream URL (lower-res, lower-bitrate companion to rtsp_uri).
  // When present, CameraTile prefers it for live view because it
  // lives on a separate RTSP endpoint the camera exposes alongside
  // the mainstream, dodging single-client mainstream limits on
  // cameras that enforce them (Tapo and others) and freeing up
  // bandwidth headroom on busy LANs. Recording stays on the main
  // stream unless the user opts in via record_substream_when_available.
  substream_uri: string | null;
  status: "online" | "offline" | "needs_auth" | "asleep";
  username: string | null;
  password: string | null;
  name: string | null;
  first_seen: string;
  last_seen: string;
  // Device type distinguishes direct IP cameras from hubs and from
  // cameras behind hubs. Hub handling is Phase 2 work — the UI must
  // not crash on non-"camera" values but may render a placeholder.
  device_type: "camera" | "hub" | "hub_camera";
  // For "hub_camera" entries: the id of the hub they live behind.
  parent_hub_id: string | null;
  // DHCP-registered hostname from reverse DNS on the camera IP.
  // Not the same as `name` — this is what the device told the router,
  // whereas `name` is what the user called it.
  hostname: string | null;
  // MAC address in lowercase colon-separated form (e.g. ec:71:db:12:34:56).
  mac_address: string | null;
  // Which signal source populated manufacturer/model:
  //   "onvif"       = authenticated ONVIF GetDeviceInformation (definitive)
  //   "fingerprint" = unauthenticated multi-signal scoring (heuristic)
  //   "manual"      = user-entered (future)
  //   null          = nothing populated these fields yet
  identification_source: "onvif" | "fingerprint" | "manual" | null;
  // Recorder-observed health. Distinct from `status` (discovery scan
  // view) — this is the live view from the ffmpeg that's writing
  // segments. A camera can be status=online + health=offline if its
  // RTSP port still accepts connections but packets have stopped
  // flowing (PoE brownout, upstream freeze, etc.). Drives the
  // corner badge on CameraTile during packet outages.
  //   null      = recorder hasn't reported yet
  //   "ok"      = packets flowing
  //   "stalled" = 15-60s silence (brief flap)
  //   "offline" = 60s+ silence (likely real outage)
  health: "ok" | "stalled" | "offline" | null;
  // ISO8601 timestamp of the most recent frame the recorder saw.
  // Used for "last live Nm ago" labels. Not persisted across
  // backend restarts — it's a live runtime field only.
  last_frame_at: string | null;
}

export interface ScanStatus {
  scanning: boolean;
  last_scan: string | null;
  cameras_found: number;
  cameras_online: number;
  cameras_needs_auth: number;
}

export interface DiscoveryEvent {
  type:
    | "snapshot"
    | "camera_found"
    | "camera_lost"
    | "camera_updated"
    | "camera_deleted"
    | "scan_complete"
    | "storage_updated"
    | "settings_updated"
    | "recording_started"
    | "recording_stopped"
    | "motion_started"
    | "motion_ended"
    | "tracked_event_closed"
    | "motion_event_updated"
    | "recordings_deleted"
    | "camera_health"
    // Per-camera audio pipeline came online / went offline. Must stay in
    // sync with backend/models.py DiscoveryEvent literal.
    | "audio_available"
    | "audio_stopped";
  data: Record<string, unknown>;
  timestamp: string;
}

export interface Settings {
  max_storage_gb: number;
  segment_duration_minutes: number;
  recording_enabled: boolean;
  recording_fps: string;
  record_substream_when_available: boolean;
  recordings_path: string | null;
  onboarding_completed: boolean;
  declared_brands: string[];
}

export interface StorageStatus {
  used_bytes: number;
  limit_bytes: number;
  free_bytes: number;
  total_bitrate_bps: number;
  seconds_remaining: number;
  cameras_recording: number;
  per_camera_bytes: Record<string, number>;
}

export interface StorageStats {
  storage_budget_gb: number;
  current_usage_gb: number;
  bitrate_gb_per_day: number | null;
  retention_days: number | null;
  free_disk_gb: number;
  ready: boolean;
}

export interface Recording {
  id: string;
  camera_id: string;
  started_at: string;
  ended_at: string | null;
  file_path: string;
  file_bytes: number;
  duration_s: number | null;
  bitrate_bps: number | null;
  in_progress: number;
}

export interface MotionEvent {
  id: string;
  camera_id: string;
  started_at: string;
  ended_at: string | null;
  thumbnail_url: string | null;
  // Classifier verdict (Phase 2). Null = silent fallback — the row
  // should render as "Motion at <camera>". When present, the Inbox
  // renders the label-first sentence ("Person at <camera>" etc.).
  // object_confidence is intentionally never rendered to the user:
  // it exists only so the backend can pick the highest-confidence
  // track on multi-track scenes. Product invariant: no scores in UI.
  object_class: "person" | "vehicle" | "animal" | null;
  object_confidence: number | null;
  summary: string | null;     // brief one-liner for Inbox row
  description: string | null; // detailed CSV for search + templates
}

export type AppScreen =
  | "discovery"
  | "setup"
  | "home"
  | "playback";

// InboxEvent is the unit shown in the Inbox view. For v2, events are
// generated by a templated event-template library on the backend from
// detected primitives (person / vehicle / animal / box) + zones +
// duration rules. The frontend renders them as a flat list grouped by
// time (Today / Yesterday / This week).
//
// Mocked for the first implementation pass — wire to a real
// /api/events endpoint once the backend template library exists.
export interface InboxEvent {
  id: string;
  // Event type corresponds to a backend template name; the frontend
  // only needs it to pick an icon and an urgency treatment.
  kind:
    | "package_delivered"
    | "person_at_zone"
    | "vehicle_entered"
    | "recurring_pattern"
    | "unusual_night_hours"
    | "overnight_summary"
    | "person_at_door";
  // The templated sentence. Generated server-side from primitives.
  // Example: "Package delivered to front porch"
  title: string;
  // Supporting details. Example: "Front door · delivery van at curb"
  subtitle: string;
  // ISO timestamp of the event start.
  started_at: string;
  // Duration in seconds. Used for the thumb's bottom-right badge.
  duration_s: number;
  // Camera id — for "tap to watch" we know which camera to play from.
  camera_id: string;
  // Has the user archived this? Archived events are hidden from the
  // default "Today" view but visible under "Archive" (future).
  archived: boolean;
  // Is this event flagged as urgent / unusual? Drives the fresh amber
  // highlight and OS notification behavior.
  urgent: boolean;
  // Has the user seen (opened) this event yet? Drives the "new" badge.
  unread: boolean;
  // Brief VLM summary for display in Inbox row and ClipStage.
  summary: string | null;
  // Detailed CSV for search queries.
  description: string | null;
}
