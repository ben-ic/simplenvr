import { apiFetch } from "../lib/backend";
import type {
  Camera,
  MotionEvent,
  ScanStatus,
  Settings,
  StorageStats,
  StorageStatus,
} from "../types";

export async function fetchCameras(): Promise<Camera[]> {
  const res = await apiFetch("/api/cameras");
  return res.json();
}

export async function fetchScanStatus(): Promise<ScanStatus> {
  const res = await apiFetch("/api/scan/status");
  return res.json();
}

export async function triggerScan(): Promise<void> {
  await apiFetch("/api/scan", { method: "POST" });
}

export async function submitAuth(
  cameraId: string,
  username: string,
  password: string,
  applyToManufacturer: boolean
): Promise<Camera> {
  const res = await apiFetch(`/api/cameras/${cameraId}/auth`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      username,
      password,
      apply_to_manufacturer: applyToManufacturer,
    }),
  });
  return res.json();
}

export async function logoutCamera(cameraId: string): Promise<Camera> {
  const res = await apiFetch(`/api/cameras/${cameraId}/auth`, {
    method: "DELETE",
  });
  return res.json();
}

// Clear the recording circuit breaker's auto-fallback for this camera.
// Used by the "Retry main stream" affordance shown when a camera is in
// the chronic_recording_failure state. Safe to call unconditionally —
// if the underlying problem hasn't been fixed, the breaker will just
// trip again and fall back to sub on its own.
export async function retryMainStream(cameraId: string): Promise<Camera> {
  const res = await apiFetch(`/api/cameras/${cameraId}/retry-main-stream`, {
    method: "POST",
  });
  if (!res.ok) {
    let message = `Could not retry main stream (HTTP ${res.status})`;
    try {
      const body = await res.json();
      if (body?.detail) message = String(body.detail);
    } catch {
      /* fall through */
    }
    throw new Error(message);
  }
  return res.json();
}

export interface ManualCameraRequest {
  ip: string;
  port?: number;
  path?: string;
  brand?: string;
  username: string;
  password: string;
  name?: string;
}

export async function addCameraManually(
  req: ManualCameraRequest
): Promise<Camera> {
  const res = await apiFetch("/api/cameras/manual", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });
  if (!res.ok) {
    // FastAPI 400 responses carry `{detail: "message"}`. Surface the
    // inline so the modal can show it directly without the user
    // needing to open a console.
    let message = `Could not add camera (HTTP ${res.status})`;
    try {
      const body = await res.json();
      if (body?.detail) message = String(body.detail);
    } catch {
      // fall through to default message
    }
    throw new Error(message);
  }
  return res.json();
}

export async function updateCameraName(
  cameraId: string,
  name: string
): Promise<Camera> {
  const res = await apiFetch(`/api/cameras/${cameraId}/name`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  return res.json();
}

export async function deleteCamera(cameraId: string): Promise<void> {
  const res = await apiFetch(`/api/cameras/${cameraId}`, {
    method: "DELETE",
  });
  if (!res.ok) {
    let message = `Could not delete camera (HTTP ${res.status})`;
    try {
      const body = await res.json();
      if (body?.detail) message = String(body.detail);
    } catch {
      // ignore
    }
    throw new Error(message);
  }
}

export async function fetchSettings(): Promise<Settings> {
  const res = await apiFetch("/api/settings");
  if (!res.ok) {
    throw new Error(`Settings not available (HTTP ${res.status})`);
  }
  return res.json();
}

export async function updateSettings(settings: Settings): Promise<Settings> {
  const res = await apiFetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(settings),
  });
  if (!res.ok) {
    // FastAPI returns {detail: "..."} for HTTPException; 422 returns an
    // array-of-errors structure. Flatten both to a single message.
    let message = `Settings update failed (HTTP ${res.status})`;
    try {
      const data = await res.json();
      if (typeof data.detail === "string") message = data.detail;
      else if (Array.isArray(data.detail) && data.detail[0]?.msg)
        message = data.detail[0].msg;
    } catch {
      /* ignore parse errors, use default message */
    }
    throw new Error(message);
  }
  return res.json();
}

export async function fetchStorage(): Promise<StorageStatus> {
  const res = await apiFetch("/api/storage");
  if (!res.ok) throw new Error(`storage ${res.status}`);
  return res.json();
}

export async function fetchStorageStats(): Promise<StorageStats> {
  const res = await apiFetch("/api/settings/storage-stats");
  if (!res.ok) throw new Error(`storage-stats ${res.status}`);
  return res.json();
}

export async function fetchDiskFree(
  path?: string | null,
): Promise<{ free_gb: number; total_gb: number }> {
  const q = path ? `?path=${encodeURIComponent(path)}` : "";
  const res = await apiFetch(`/api/settings/disk-free${q}`);
  if (!res.ok) throw new Error(`disk-free ${res.status}`);
  return res.json();
}

export async function fetchCurrentRecordingsDir(): Promise<string> {
  const res = await apiFetch("/api/settings/recordings-dir");
  if (!res.ok) throw new Error(`recordings-dir ${res.status}`);
  const body = await res.json();
  return String(body.path ?? "");
}

export async function resetDatabase(): Promise<{ message: string }> {
  const res = await apiFetch("/api/settings/reset", {
    method: "POST",
  });
  if (!res.ok) {
    let message = `Database reset failed (HTTP ${res.status})`;
    try {
      const data = await res.json();
      if (data?.detail) message = data.detail;
    } catch {
      /* ignore parse errors, use default message */
    }
    throw new Error(message);
  }
  return res.json();
}

export async function clearData(): Promise<{ message: string }> {
  const res = await apiFetch("/api/settings/clear-data", {
    method: "POST",
  });
  if (!res.ok) {
    let message = `Clear data failed (HTTP ${res.status})`;
    try {
      const data = await res.json();
      if (data?.detail) message = data.detail;
    } catch {
      /* ignore parse errors, use default message */
    }
    throw new Error(message);
  }
  return res.json();
}

export async function fetchRecordingDates(
  cameraId?: string
): Promise<string[]> {
  const path = cameraId
    ? `/api/recordings/dates?camera_id=${cameraId}`
    : `/api/recordings/dates`;
  const res = await apiFetch(path);
  const data = await res.json();
  return data.dates;
}

export interface TimelineSegment {
  id: string;
  started_at: string;
  second_of_day: number;
  duration_s: number;
  file_bytes: number;
  in_progress: boolean;
}

export interface Timeline {
  date: string;
  camera_id: string;
  segments: TimelineSegment[];
  total_duration_s: number;
}

export interface MotionEventFilters {
  limit?: number;
  all?: boolean;
  object_class?: string | null;
  camera_id?: string | null;
  started_after?: string | null;
  ended_before?: string | null;
}

export async function fetchRecentMotionEvents(
  limitOrFilters: number | MotionEventFilters = 20
): Promise<MotionEvent[]> {
  const filters: MotionEventFilters =
    typeof limitOrFilters === "number"
      ? { limit: limitOrFilters }
      : limitOrFilters;
  const params = new URLSearchParams();
  if (filters.limit != null) params.set("limit", String(filters.limit));
  if (filters.all) params.set("all", "true");
  if (filters.object_class) params.set("object_class", filters.object_class);
  if (filters.camera_id) params.set("camera_id", filters.camera_id);
  if (filters.started_after) params.set("started_after", filters.started_after);
  if (filters.ended_before) params.set("ended_before", filters.ended_before);
  const res = await apiFetch(`/api/motion_events/recent?${params}`);
  const data = await res.json();
  return data.events;
}

export async function searchMotionEvents(
  query: string,
  limit = 50
): Promise<MotionEvent[]> {
  const res = await apiFetch(
    `/api/motion_events/search?q=${encodeURIComponent(query)}&limit=${limit}`
  );
  if (!res.ok) return [];
  const data = await res.json();
  return data.events;
}

export interface Episode {
  id: string; // primary event id (for clip playback)
  camera_id: string;
  started_at: string;
  ended_at: string | null;
  object_class: "person" | "vehicle" | "animal" | null;
  labels: string[]; // all distinct labels in the episode (e.g. ["person", "vehicle"])
  thumbnail_url: string | null;
  description: string | null; // Moondream VLM one-liner (null = not available)
  event_count: number;
  duration_s: number;
  event_ids: string[];
}

export async function fetchRecentEpisodes(
  limit = 50
): Promise<Episode[]> {
  const res = await apiFetch(`/api/episodes/recent?limit=${limit}`);
  if (!res.ok) return [];
  const data = await res.json();
  return data.episodes ?? [];
}

// ── Story digest ──────────────────────────────────────────────────────

export interface StoryLine {
  text: string;
  event_count: number;
  started_at: string;
  event_ids: string[];
}

export interface StoryCameraSummary {
  camera_id: string;
  camera_name: string;
  is_quiet: boolean;
  total_events: number;
  lines: StoryLine[];
}

export interface StoryDigest {
  period_label: string;
  total_events: number;
  is_quiet: boolean;
  overall_summary: string;
  cameras: StoryCameraSummary[];
}

export async function fetchStoryToday(): Promise<StoryDigest | null> {
  const res = await apiFetch("/api/story/today");
  if (!res.ok) return null;
  return res.json();
}

// ── Today view ───────────────────────────────────────────────────────

export interface TodayCameraSummary {
  camera_id: string;
  camera_name: string;
  person_count: number;
  vehicle_count: number;
  animal_count: number;
  total: number;
}

/** Episode-shaped card row from /api/today.
 *  See backend _finalize_episode for the full shape. */
export interface TodayEpisode {
  id: string;
  camera_id: string;
  started_at: string;
  ended_at: string | null;
  object_class: string | null;
  labels: string[];
  thumbnail_url: string | null;
  description: string | null;
  event_count: number;
  duration_s: number;
  event_ids: string[];
}

export interface TodayData {
  notable: TodayEpisode[];
  cameras: TodayCameraSummary[];
}

export async function fetchToday(
  classes: string[] = ["person"],
): Promise<TodayData | null> {
  const qs = classes.length > 0
    ? `?classes=${encodeURIComponent(classes.join(","))}`
    : "?classes=";
  const res = await apiFetch(`/api/today${qs}`);
  if (!res.ok) return null;
  return res.json();
}

// ── Motion timeline ───────────────────────────────────────────────────

export interface MotionTimelineEntry {
  second_of_day: number;
  duration_s: number;
  event_id: string;
  thumbnail_url: string | null;
}

export async function fetchMotionTimeline(
  cameraId: string,
  date: string
): Promise<MotionTimelineEntry[]> {
  const res = await apiFetch(
    `/api/motion_events/timeline?camera_id=${cameraId}&date=${date}`
  );
  const data = await res.json();
  // Backend returns a bare JSON array (backend/api/motion.py
  // motion_timeline). Tolerate the legacy `{events: [...]}` shape too
  // in case the endpoint ever changes shape.
  if (Array.isArray(data)) return data;
  return Array.isArray(data?.events) ? data.events : [];
}

export async function fetchTimeline(
  cameraId: string,
  date: string
): Promise<Timeline> {
  const res = await apiFetch(
    `/api/recordings/timeline?camera_id=${cameraId}&date=${date}`
  );
  return res.json();
}
