import type { Camera } from "../types";

// Shared UI formatters. Extracted from Home.tsx + HistoryPanel.tsx where
// they were duplicated verbatim. Keep this module presentation-only —
// anything that touches the DOM, the network, or React state belongs
// somewhere else.

export function formatDuration(s: number): string {
  if (s < 60) return `0:${String(s).padStart(2, "0")}`;
  const m = Math.floor(s / 60);
  const sec = s % 60;
  if (m < 60) return `${m}:${String(sec).padStart(2, "0")}`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

// Best-effort display label for a camera. Prefers the user-supplied name,
// falls back to manufacturer/IP, hostname, and finally bare IP. Kept here
// so every screen labels a given camera identically.
export function cameraDisplayName(cam: Camera | undefined): string {
  if (!cam) return "Camera";
  if (cam.name) return cam.name;
  if (cam.manufacturer) return `${cam.manufacturer} (${cam.ip})`;
  if (cam.hostname) return cam.hostname;
  return cam.ip;
}

export function cameraNameFor(cameras: Camera[], cameraId: string): string {
  return cameraDisplayName(cameras.find((c) => c.id === cameraId));
}
