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
  status: "online" | "offline" | "needs_auth";
  username: string | null;
  password: string | null;
  name: string | null;
  first_seen: string;
  last_seen: string;
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
    | "scan_complete"
    | "storage_updated"
    | "settings_updated"
    | "recording_started"
    | "recording_stopped"
    | "motion_started"
    | "motion_ended";
  data: Record<string, unknown>;
  timestamp: string;
}

export interface Settings {
  max_storage_gb: number;
  segment_duration_minutes: number;
  recording_enabled: boolean;
  recording_fps: string;
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
}

export type AppScreen =
  | "onboarding"
  | "scan"
  | "discovery"
  | "dashboard"
  | "playback";
