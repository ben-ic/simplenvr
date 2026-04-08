import { useEffect, useState } from "react";
import { apiUrl } from "../lib/backend";
import { useStorage } from "../hooks/useStorage";
import type { Camera } from "../types";
import { MotionPanel } from "./MotionPanel";
import { SettingsModal } from "./SettingsModal";
import { StorageBanner } from "./StorageBanner";

export function Dashboard({
  cameras,
  onPlayback,
  onManageCameras,
  activeMotion,
}: {
  cameras: Camera[];
  onPlayback: (cameraId?: string, startedAt?: string) => void;
  onManageCameras: () => void;
  activeMotion: Map<string, string>;
}) {
  const online = cameras.filter((c) => c.status === "online" && c.rtsp_uri);
  const storage = useStorage();
  const [showSettings, setShowSettings] = useState(false);
  const [showMotion, setShowMotion] = useState(false);

  return (
    <div className="flex-1 flex flex-col">
      {/* Topbar */}
      <div className="flex items-center justify-between px-5 h-12 bg-[#1a1a1a] border-b border-[#333] shrink-0">
        <div className="flex items-center gap-3">
          <span className="text-[#ddd] font-bold text-[15px]">SimpleNVR</span>
          {storage && (
            <span className="flex items-center gap-1.5 text-xs text-red-500 font-medium">
              <span className="w-1.5 h-1.5 rounded-full bg-red-500 animate-pulse" />
              Recording {storage.cameras_recording} camera
              {storage.cameras_recording !== 1 ? "s" : ""}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={onManageCameras}
            className="px-3 py-1.5 text-[#888] hover:text-[#ddd] text-xs transition-colors"
          >
            Cameras
          </button>
          <button
            onClick={() => setShowMotion(true)}
            className="px-3 py-1.5 text-[#888] hover:text-[#ddd] text-xs transition-colors flex items-center gap-1.5 relative"
          >
            <svg className="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 24 24">
              <path d="M12 22c1.1 0 2-.9 2-2h-4c0 1.1.9 2 2 2zm6-6v-5c0-3.07-1.63-5.64-4.5-6.32V4c0-.83-.67-1.5-1.5-1.5s-1.5.67-1.5 1.5v.68C7.64 5.36 6 7.92 6 11v5l-2 2v1h16v-1l-2-2z" />
            </svg>
            Motion
            {activeMotion.size > 0 && (
              <span className="absolute -top-0.5 -right-0.5 w-2 h-2 rounded-full bg-red-500 animate-pulse" />
            )}
          </button>
          <button
            onClick={() => onPlayback()}
            className="px-3 py-1.5 bg-[#222] border border-[#333] text-[#ddd] text-xs font-semibold rounded hover:bg-[#2a2a2a] transition-colors flex items-center gap-1.5"
          >
            <svg className="w-3 h-3" fill="currentColor" viewBox="0 0 24 24">
              <path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10 10-4.5 10-10S17.5 2 12 2zm0 18c-4.4 0-8-3.6-8-8s3.6-8 8-8 8 3.6 8 8-3.6 8-8 8zm.5-13H11v6l5.2 3.2.8-1.3-4.5-2.7V7z" />
            </svg>
            Recordings
          </button>
          <button
            onClick={() => setShowSettings(true)}
            className="p-1.5 text-[#888] hover:text-[#ddd] transition-colors"
            title="Settings"
            aria-label="Settings"
          >
            <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
              <circle cx="12" cy="12" r="3" />
              <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z" />
            </svg>
          </button>
        </div>
      </div>

      {/* Camera grid */}
      {online.length === 0 ? (
        <div className="flex-1 flex items-center justify-center text-[#555] text-sm">
          No connected cameras to display
        </div>
      ) : (
        <div
          className="flex-1 grid gap-[1px] bg-black p-[1px]"
          style={{
            gridTemplateColumns: `repeat(${
              online.length === 1 ? 1 : 2
            }, 1fr)`,
          }}
        >
          {online.map((cam) => (
            <CameraTile
              key={cam.id}
              camera={cam}
              isMotionActive={activeMotion.has(cam.id)}
              onClick={() => onPlayback(cam.id)}
            />
          ))}
        </div>
      )}

      {/* Storage banner */}
      <StorageBanner storage={storage} />

      {/* Settings modal */}
      {showSettings && <SettingsModal onClose={() => setShowSettings(false)} />}

      {/* Motion panel */}
      <MotionPanel
        open={showMotion}
        onClose={() => setShowMotion(false)}
        cameras={cameras}
        activeMotion={activeMotion}
        onJumpToEvent={(camId, startedAt) => {
          setShowMotion(false);
          onPlayback(camId, startedAt);
        }}
      />
    </div>
  );
}

// localStorage key tracking which cameras this browser has successfully
// streamed in the past. We use this to show "First connection takes a
// few seconds…" only for truly new cameras, and a shorter "Reconnecting…"
// message for cameras the user has seen before. The reason to separate
// these two cases is honesty: telling a user "first connection is slow"
// every time they open the app would be misleading after the first run.
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
// Cold-start TTFF for a camera is typically 3-5s (warm DB) or up to
// 20-30s on first-ever launch while discovery + go2rtc registration +
// RTSP handshake all run. 15s is the sweet spot: long enough that
// warm-starts never trip it, short enough that a genuinely broken
// camera surfaces its state before the user gives up and quits.
const FIRST_FRAME_TIMEOUT_MS = 15_000;

function CameraTile({
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

  // Live clock
  useEffect(() => {
    const id = setInterval(() => setClock(formatNow()), 1000);
    return () => clearInterval(id);
  }, []);

  // Resolve MJPEG stream URL (async to support Tauri vs dev modes).
  // retryKey is in the deps so pressing "Retry" re-resolves and
  // re-renders the <img> with a cache-busting URL.
  useEffect(() => {
    let cancelled = false;
    setHasFirstFrame(false);
    setConnectionFailed(false);
    apiUrl(`/api/cameras/${camera.id}/stream.mjpeg`).then((url) => {
      if (!cancelled) {
        // Append retry counter so browser doesn't reuse a stale connection
        const sep = url.includes("?") ? "&" : "?";
        setStreamUrl(retryKey === 0 ? url : `${url}${sep}_r=${retryKey}`);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [camera.id, retryKey]);

  // Failure timeout: if no frame arrives within FIRST_FRAME_TIMEOUT_MS,
  // transition to the "Unable to connect" state. Cleared as soon as
  // the first frame arrives.
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

      {/* Connecting overlay — shown until the first frame arrives.
          First-time connections get an explanatory note so the user
          understands why there's a wait and that it only happens once. */}
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

      {/* Error state — shown if no frame has arrived in 15s. Includes
          a retry button that forces the stream URL to re-resolve with
          a cache-busting query param. */}
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

      {/* Hover hint */}
      <div className="absolute inset-0 bg-black/0 group-hover:bg-black/30 transition-colors flex items-center justify-center opacity-0 group-hover:opacity-100">
        <div className="bg-black/70 text-white text-xs font-semibold px-3 py-1.5 rounded">
          View recordings →
        </div>
      </div>

      {/* Top overlay */}
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

      {/* Bottom overlay */}
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
    d.getMinutes()
  ).padStart(2, "0")}:${String(d.getSeconds()).padStart(2, "0")}`;
}
