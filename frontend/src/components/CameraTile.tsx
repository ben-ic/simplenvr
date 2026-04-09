import { useEffect, useState } from "react";
import { apiUrl } from "../lib/backend";
import type { Camera } from "../types";

// localStorage key tracking which cameras this browser has successfully
// streamed in the past. Drives the "First connection takes a few seconds…"
// honesty copy: show it only for cameras the user has never seen before,
// since telling them that every time would be misleading on subsequent
// launches.
const SEEN_CAMERAS_KEY = "simplenvr.seen-cameras";

function getSeenCameras(): Set<string> {
  try {
    const raw = localStorage.getItem(SEEN_CAMERAS_KEY);
    if (!raw) return new Set();
    const arr = JSON.parse(raw);
    return new Set(Array.isArray(arr) ? arr : []);
  } catch {
    return new Set();
  }
}

function markCameraSeen(id: string) {
  try {
    const seen = getSeenCameras();
    if (seen.has(id)) return;
    seen.add(id);
    localStorage.setItem(SEEN_CAMERAS_KEY, JSON.stringify([...seen]));
  } catch {
    // localStorage unavailable (e.g. private mode) — just skip
  }
}

// How long to wait for the first frame before showing a failure state.
// Cold-start TTFF is typically 3-5s warm / up to 20-30s on first-ever
// launch while discovery + go2rtc registration + RTSP handshake all run.
// 15s is the sweet spot: long enough that warm-starts never trip it,
// short enough that a broken camera surfaces before the user gives up.
const FIRST_FRAME_TIMEOUT_MS = 15_000;

export function CameraTile({
  camera,
  onClick,
  isMotionActive,
}: {
  camera: Camera;
  onClick: () => void;
  isMotionActive: boolean;
}) {
  const [clock, setClock] = useState(formatNow);
  const [streamUrl, setStreamUrl] = useState<string>("");
  const [hasFirstFrame, setHasFirstFrame] = useState(false);
  const [connectionFailed, setConnectionFailed] = useState(false);
  const [retryKey, setRetryKey] = useState(0);
  // isFirstConnect is captured at mount time so the copy doesn't flip
  // mid-connection when we markCameraSeen() after the first frame.
  const [isFirstConnect] = useState(() => !getSeenCameras().has(camera.id));

  useEffect(() => {
    const id = setInterval(() => setClock(formatNow()), 1000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    let cancelled = false;
    setHasFirstFrame(false);
    setConnectionFailed(false);
    apiUrl(`/api/cameras/${camera.id}/stream.mjpeg`).then((url) => {
      if (!cancelled) {
        const sep = url.includes("?") ? "&" : "?";
        setStreamUrl(retryKey === 0 ? url : `${url}${sep}_r=${retryKey}`);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [camera.id, retryKey]);

  useEffect(() => {
    if (hasFirstFrame) return;
    const timer = setTimeout(() => {
      setConnectionFailed(true);
    }, FIRST_FRAME_TIMEOUT_MS);
    return () => clearTimeout(timer);
  }, [hasFirstFrame, retryKey]);

  const displayName =
    camera.name ||
    [camera.manufacturer, camera.model].filter(Boolean).join(" ") ||
    camera.ip;

  const showOverlay = !hasFirstFrame && !connectionFailed;
  const showError = connectionFailed;

  return (
    <div
      onClick={onClick}
      className={`bg-[#0a0a0a] relative aspect-video overflow-hidden cursor-pointer group ${
        isMotionActive
          ? "outline outline-2 outline-red-500 outline-offset-[-2px] animate-pulse"
          : ""
      }`}
    >
      {streamUrl && !connectionFailed && (
        <img
          src={streamUrl}
          alt={displayName}
          className="w-full h-full object-cover"
          onLoad={() => {
            setHasFirstFrame(true);
            markCameraSeen(camera.id);
          }}
          onError={() => setConnectionFailed(true)}
        />
      )}

      {showOverlay && (
        <div className="absolute inset-0 flex flex-col items-center justify-center bg-[#0a0a0a] text-center px-6 pointer-events-none">
          <div className="flex items-center gap-2 mb-3">
            <svg
              className="w-4 h-4 text-[#888] animate-spin"
              fill="none"
              stroke="currentColor"
              strokeWidth={2.5}
              viewBox="0 0 24 24"
            >
              <path d="M21 12a9 9 0 1 1-6.219-8.56" strokeLinecap="round" />
            </svg>
            <span className="text-sm font-medium text-[#ddd]">
              {isFirstConnect ? "Connecting to " : "Reconnecting to "}
              <span className="text-white">{displayName}</span>
              …
            </span>
          </div>
          {isFirstConnect && (
            <p className="text-[11px] text-[#777] max-w-[280px] leading-snug">
              First connection takes a few seconds while we set up the stream.
              This only happens once.
            </p>
          )}
        </div>
      )}

      {showError && (
        <div
          className="absolute inset-0 flex flex-col items-center justify-center bg-[#0a0a0a] text-center px-6"
          onClick={(e) => e.stopPropagation()}
        >
          <svg
            className="w-5 h-5 text-red-500 mb-2"
            fill="none"
            stroke="currentColor"
            strokeWidth={2}
            viewBox="0 0 24 24"
          >
            <circle cx="12" cy="12" r="10" />
            <line x1="12" y1="8" x2="12" y2="12" />
            <line x1="12" y1="16" x2="12.01" y2="16" />
          </svg>
          <span className="text-sm font-medium text-[#ddd] mb-1">
            Can't reach <span className="text-white">{displayName}</span>
          </span>
          <p className="text-[11px] text-[#777] max-w-[280px] leading-snug mb-3">
            Check the camera is powered on and on the same network.
          </p>
          <button
            onClick={() => {
              setConnectionFailed(false);
              setHasFirstFrame(false);
              setRetryKey((k) => k + 1);
            }}
            className="px-3 py-1 text-xs font-semibold text-[#ddd] bg-[#222] border border-[#333] rounded hover:bg-[#2a2a2a] transition-colors"
          >
            Retry
          </button>
        </div>
      )}

      <div className="absolute inset-0 bg-black/0 group-hover:bg-black/30 transition-colors flex items-center justify-center opacity-0 group-hover:opacity-100">
        <div className="bg-black/70 text-white text-xs font-semibold px-3 py-1.5 rounded">
          Browse footage →
        </div>
      </div>

      <div className="absolute top-0 left-0 right-0 px-3 py-2 flex justify-between items-start bg-gradient-to-b from-black/70 to-transparent pointer-events-none">
        <span className="text-xs font-semibold text-white drop-shadow">
          {displayName}
        </span>
        <div className="flex items-center gap-2">
          {isMotionActive && (
            <span className="flex items-center gap-1 text-[10px] font-bold uppercase tracking-wide text-red-500 bg-black/60 px-1.5 py-0.5 rounded">
              MOTION
            </span>
          )}
          <span className="flex items-center gap-1 text-[10px] font-bold uppercase tracking-wide text-red-500">
            <span className="w-1 h-1 rounded-full bg-red-500 animate-pulse" />
            REC
          </span>
        </div>
      </div>

      <div className="absolute bottom-0 left-0 right-0 px-3 py-2 flex justify-between items-end bg-gradient-to-t from-black/70 to-transparent pointer-events-none">
        <span className="text-[11px] text-white/70 font-mono tabular-nums">
          {clock}
        </span>
      </div>
    </div>
  );
}

function formatNow(): string {
  const d = new Date();
  return `${String(d.getHours()).padStart(2, "0")}:${String(
    d.getMinutes(),
  ).padStart(2, "0")}:${String(d.getSeconds()).padStart(2, "0")}`;
}
