import { invoke } from "@tauri-apps/api/core";

let cachedBase: string | null = null;

async function resolveBase(): Promise<string> {
  if (cachedBase !== null) return cachedBase;

  // Tauri v2 sets window.__TAURI_INTERNALS__ in both dev and prod webviews.
  // Plain browser (no Tauri) leaves it undefined.
  if (typeof window !== "undefined" && (window as any).__TAURI_INTERNALS__) {
    const port: number = await invoke("get_backend_port");
    cachedBase = `http://127.0.0.1:${port}`;
  } else {
    cachedBase = ""; // dev fallback — Vite proxy handles routing
  }
  return cachedBase;
}

export async function apiUrl(path: string): Promise<string> {
  const base = await resolveBase();
  return `${base}${path}`;
}

export async function apiFetch(
  path: string,
  init?: RequestInit
): Promise<Response> {
  return fetch(await apiUrl(path), init);
}

export async function wsUrl(path: string): Promise<string> {
  const base = await resolveBase();
  if (base === "") {
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    return `${protocol}//${location.host}${path}`;
  }
  const url = new URL(base);
  return `ws://127.0.0.1:${url.port}${path}`;
}
