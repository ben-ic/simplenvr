import type {
  Camera,
  Recording,
  ScanStatus,
  Settings,
  StorageStatus,
} from "../types";

const BASE = "";

export async function fetchCameras(): Promise<Camera[]> {
  const res = await fetch(`${BASE}/api/cameras`);
  return res.json();
}

export async function fetchScanStatus(): Promise<ScanStatus> {
  const res = await fetch(`${BASE}/api/scan/status`);
  return res.json();
}

export async function triggerScan(): Promise<void> {
  await fetch(`${BASE}/api/scan`, { method: "POST" });
}

export async function submitAuth(
  cameraId: string,
  username: string,
  password: string,
  applyToManufacturer: boolean
): Promise<Camera> {
  const res = await fetch(`${BASE}/api/cameras/${cameraId}/auth`, {
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

export async function updateCameraName(
  cameraId: string,
  name: string
): Promise<Camera> {
  const res = await fetch(`${BASE}/api/cameras/${cameraId}/name`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  return res.json();
}

export async function fetchSettings(): Promise<Settings> {
  const res = await fetch(`${BASE}/api/settings`);
  return res.json();
}

export async function updateSettings(settings: Settings): Promise<Settings> {
  const res = await fetch(`${BASE}/api/settings`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(settings),
  });
  return res.json();
}

export async function fetchStorage(): Promise<StorageStatus> {
  const res = await fetch(`${BASE}/api/storage`);
  return res.json();
}

export async function fetchRecordingDates(
  cameraId?: string
): Promise<string[]> {
  const url = cameraId
    ? `${BASE}/api/recordings/dates?camera_id=${cameraId}`
    : `${BASE}/api/recordings/dates`;
  const res = await fetch(url);
  const data = await res.json();
  return data.dates;
}

export async function fetchRecordings(
  cameraId: string,
  date: string
): Promise<Recording[]> {
  const res = await fetch(
    `${BASE}/api/recordings?camera_id=${cameraId}&date=${date}`
  );
  const data = await res.json();
  return data.recordings;
}

export function recordingFileUrl(recordingId: string): string {
  return `${BASE}/api/recordings/${recordingId}/file`;
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

export async function fetchTimeline(
  cameraId: string,
  date: string
): Promise<Timeline> {
  const res = await fetch(
    `${BASE}/api/recordings/timeline?camera_id=${cameraId}&date=${date}`
  );
  return res.json();
}
