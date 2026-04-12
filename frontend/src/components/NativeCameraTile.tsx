import { useEffect, useRef, useState } from "react";
import { setMuted as nativeSetMuted } from "tauri-plugin-rtsp-mosaic-api";
import type { Camera } from "../types";
import { cameraDisplayName } from "../lib/format";

// Import registers the <rtsp-tile> custom element globally.
import "tauri-plugin-rtsp-mosaic-api";

// ---------------------------------------------------------------------------
// NativeCameraTile — thin React wrapper around <rtsp-tile>.
//
// The custom element handles the full native tile lifecycle: create on
// connectedCallback, position via ResizeObserver, destroy on disconnect.
// This wrapper bridges React props → DOM attributes and provides the
// interactive overlay layer (click-to-focus, browse footage on hover,
// mute toggle) that lives in the webview DOM.
//
// Z-ordering: the native NSView renders ABOVE the webview. Mouse events
// pass through via hitTest override, so invisible DOM click handlers
// still fire. Visible overlays (name, REC, motion, timestamp) are
// rendered by the plugin's native CATextLayer system via setOverlay().
// ---------------------------------------------------------------------------

// Per-camera mute preference in localStorage. Mirrors the old CameraTile
// contract so existing prefs carry over seamlessly.
const MUTE_KEY_PREFIX = "simplenvr.mute.";

function getCameraMutePref(cameraId: string): boolean {
  try {
    const raw = localStorage.getItem(MUTE_KEY_PREFIX + cameraId);
    if (raw === null) return true; // default muted
    return raw === "true";
  } catch {
    return true;
  }
}

function setCameraMutePref(cameraId: string, muted: boolean) {
  try {
    localStorage.setItem(MUTE_KEY_PREFIX + cameraId, muted ? "true" : "false");
  } catch {
    // localStorage unavailable
  }
}

// Declare the custom element for TypeScript / JSX.
declare global {
  namespace JSX {
    interface IntrinsicElements {
      "rtsp-tile": React.DetailedHTMLProps<
        React.HTMLAttributes<HTMLElement> & {
          src?: string;
          muted?: boolean | "";
          name?: string;
          status?: string;
          timestamp?: string;
          motion?: boolean | "";
        },
        HTMLElement
      >;
    }
  }
}

export function NativeCameraTile({
  camera,
  isMotionActive,
  preferSubstream,
  onClick,
  onBrowseFootage,
}: {
  camera: Camera;
  isMotionActive: boolean;
  preferSubstream: boolean;
  onClick?: () => void;
  onBrowseFootage?: () => void;
}) {
  const ref = useRef<HTMLElement>(null);
  const displayName = cameraDisplayName(camera);
  const [muted, setMuted] = useState(() => getCameraMutePref(camera.id));

  // Build the go2rtc loopback RTSP URL. No credentials needed — go2rtc
  // is on localhost and the camera's authed URI was registered via the
  // admin API by the Python sidecar at startup.
  const streamId =
    preferSubstream && camera.substream_uri ? `${camera.id}_sub` : camera.id;
  const src = `rtsp://127.0.0.1:58554/${streamId}`;

  // Sync the muted attribute imperatively. React's boolean attribute
  // handling for custom elements is inconsistent — setting muted={true}
  // may serialize to "true" instead of the presence/absence toggle the
  // custom element expects.
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (muted) {
      el.setAttribute("muted", "");
    } else {
      el.removeAttribute("muted");
    }
    // Also sync via the IPC API for any existing native tile.
    const tileId = (el as any).tileId;
    if (tileId) {
      nativeSetMuted(tileId, muted).catch(() => {});
    }
  }, [muted]);

  // Sync overlay-affecting attributes imperatively.
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (isMotionActive) {
      el.setAttribute("motion", "");
    } else {
      el.removeAttribute("motion");
    }
  }, [isMotionActive]);

  const toggleMute = (e: React.MouseEvent) => {
    e.stopPropagation();
    const next = !muted;
    setMuted(next);
    setCameraMutePref(camera.id, next);
  };

  return (
    <div className="relative w-full h-full group bg-black overflow-hidden">
      <rtsp-tile
        ref={ref}
        src={src}
        name={displayName}
        status="recording"
        timestamp="live"
        className="block w-full h-full"
      />

      {/* Click catcher — transparent, covers the whole tile. The native
          NSView passes mouse events through, so clicks reach this div.
          Single click → focus (enlarge). */}
      <div
        className="absolute inset-0 cursor-pointer"
        onClick={onClick}
      />

      {/* Hover overlay — "Browse footage" button. Invisible by default,
          fades in on hover. The native tile is above the webview, so the
          hover darkening effect is NOT visible — but the button IS
          clickable thanks to mouse passthrough. We keep the hover
          opacity transition so it works correctly if the user is on a
          platform where native tiles aren't rendering (fallback). */}
      {onBrowseFootage && (
        <div className="absolute inset-0 flex items-center justify-center opacity-0 group-hover:opacity-100 pointer-events-none transition-opacity">
          <button
            onClick={(e) => {
              e.stopPropagation();
              onBrowseFootage();
            }}
            className="pointer-events-auto bg-black/70 text-white text-xs font-semibold px-3 py-1.5 rounded hover:bg-black/90 transition-colors"
          >
            Browse footage &rarr;
          </button>
        </div>
      )}

      {/* Mute toggle — bottom-right corner. Small enough to not
          obstruct the video, but visible and clickable. */}
      <button
        onClick={toggleMute}
        title={muted ? "Unmute audio" : "Mute audio"}
        aria-label={muted ? "Unmute audio" : "Mute audio"}
        className="absolute bottom-2 right-2 text-white/70 hover:text-white transition-colors p-1 rounded hover:bg-black/40 pointer-events-auto"
      >
        {muted ? (
          <svg
            className="w-4 h-4"
            fill="none"
            stroke="currentColor"
            strokeWidth={2}
            viewBox="0 0 24 24"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M11 5L6 9H2v6h4l5 4V5z"
            />
            <line x1="23" y1="9" x2="17" y2="15" strokeLinecap="round" />
            <line x1="17" y1="9" x2="23" y2="15" strokeLinecap="round" />
          </svg>
        ) : (
          <svg
            className="w-4 h-4"
            fill="none"
            stroke="currentColor"
            strokeWidth={2}
            viewBox="0 0 24 24"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M11 5L6 9H2v6h4l5 4V5z"
            />
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M15.54 8.46a5 5 0 0 1 0 7.07"
            />
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M19.07 4.93a10 10 0 0 1 0 14.14"
            />
          </svg>
        )}
      </button>
    </div>
  );
}
