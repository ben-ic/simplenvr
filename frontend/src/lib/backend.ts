import { invoke } from "@tauri-apps/api/core";

export function isTauri(): boolean {
  return typeof window !== "undefined" && !!(window as any).__TAURI_INTERNALS__;
}

let cachedBase: string | null = null;

async function resolveBase(): Promise<string> {
  if (cachedBase !== null) return cachedBase;

  // Tauri v2 sets window.__TAURI_INTERNALS__ in both dev and prod webviews.
  // Plain browser (no Tauri) leaves it undefined.
  if (typeof window !== "undefined" && (window as any).__TAURI_INTERNALS__) {
    const port: number = await invoke("get_backend_port");
    cachedBase = `http://127.0.0.1:${port}`;
    installTightCSP(port);
  } else {
    cachedBase = ""; // dev fallback — Vite proxy handles routing
  }
  return cachedBase;
}

// Defense-in-depth CSP narrowing. The compile-time CSP in tauri.conf.json
// allows connect/img/media to http://127.0.0.1:* because the backend port
// is chosen dynamically at startup and isn't known at build time. Once
// we've resolved that port via Tauri's IPC (which does NOT use HTTP and
// therefore isn't affected by CSP), we inject a <meta> policy that locks
// network access to the exact backend port.
//
// CSP composes with AND semantics — adding a policy can only *remove*
// permissions, never grant them. After this meta element lands, the
// webview can still reach the backend on its chosen port but can no
// longer fetch from any other localhost service (notably go2rtc's
// admin API on 58581, which accepts unauthenticated PUT /api/streams
// and would let a compromised webview redirect camera feeds).
function installTightCSP(port: number): void {
  if (typeof document === "undefined") return;
  if (document.head.querySelector('meta[data-simplenvr-csp="runtime"]')) return;
  const exact = `http://127.0.0.1:${port}`;
  const exactWs = `ws://127.0.0.1:${port}`;
  const meta = document.createElement("meta");
  meta.httpEquiv = "Content-Security-Policy";
  meta.setAttribute("data-simplenvr-csp", "runtime");
  meta.content =
    `default-src 'self'; ` +
    `connect-src 'self' ${exact} ${exactWs}; ` +
    `img-src 'self' data: blob: ${exact}; ` +
    `media-src 'self' blob: ${exact}; ` +
    `script-src 'self'; ` +
    `style-src 'self' 'unsafe-inline'`;
  document.head.appendChild(meta);
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

// ─── URL builders for backend-served media ──────────────────────────
//
// Build URLs to backend resources in one place so path-injection or
// scheme-injection bugs can't creep in from a stray string-concat at
// a call site. All helpers gate on a `/api/` prefix and urlencode any
// interpolated IDs — a future backend change that starts returning
// paths outside /api/, or IDs containing slashes/query chars, won't
// silently produce attacker-influenced URLs.

// Ensure a backend-relative path is rooted at /api/ before we attach
// it to an <img> or <video> src. Returns null for invalid input so
// callers can render a placeholder instead.
function safeBackendPath(relPath: string | null | undefined): string | null {
  if (!relPath) return null;
  if (!relPath.startsWith("/api/")) return null;
  return relPath;
}

// Build an absolute URL for a backend thumbnail. `base` is the resolved
// backend base URL (from useBackendBaseUrl in HistoryPanel) or null if
// it hasn't been fetched yet — in both uncertain cases we return null
// so the caller renders the placeholder glyph.
export function thumbnailUrl(
  relPath: string | null | undefined,
  base: string | null,
): string | null {
  const safe = safeBackendPath(relPath);
  if (safe === null || base === null) return null;
  // Trim any accidental trailing slash on base so we never produce //.
  const trimmed = base.endsWith("/") ? base.slice(0, -1) : base;
  return `${trimmed}${safe}`;
}

// Build the URL for a recording segment mp4. The segment id is freshly
// encoded so a future schema change that widens segment IDs past the
// current UUID shape cannot inject path segments or query strings.
export async function recordingFileUrl(segmentId: string): Promise<string> {
  return apiUrl(`/api/recordings/${encodeURIComponent(segmentId)}/file`);
}
