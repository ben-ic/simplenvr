import { apiFetch, apiUrl } from "../lib/backend";
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
  return res.json();
}

export async function fetchStorageStats(): Promise<StorageStats> {
  const res = await apiFetch("/api/settings/storage-stats");
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

export async function fetchRecentMotionEvents(
  limit = 20
): Promise<MotionEvent[]> {
  const res = await apiFetch(`/api/motion_events/recent?limit=${limit}`);
  const data = await res.json();
  return data.events;
}

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
