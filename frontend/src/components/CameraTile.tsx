import { useEffect, useRef, useState } from "react";
import type { Camera } from "../types";

// localStorage keys
const SEEN_CAMERAS_KEY = "simplenvr.seen-cameras";
const MUTE_KEY_PREFIX = "simplenvr.mute."; // per-camera: simplenvr.mute.{id}

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

function getCameraMutePref(cameraId: string): boolean {
  try {
    const raw = localStorage.getItem(MUTE_KEY_PREFIX + cameraId);
    // Default to true (muted) — matches browser autoplay policy,
    // which requires muted video for unattended playback. The user
    // opts IN to audio per-camera via the unmute button.
    if (raw === null) return true;
    return raw === "true";
  } catch {
    return true;
  }
}

function setCameraMutePref(cameraId: string, muted: boolean) {
  try {
    localStorage.setItem(MUTE_KEY_PREFIX + cameraId, muted ? "true" : "false");
  } catch {
    // ignore
  }
}

// How long to wait for the custom element to produce its first
// internal <video> before showing a failure state. The element
// handles its own internal retries, so this is mostly about telling
// the user something is wrong when nothing is happening at all.
const FIRST_FRAME_TIMEOUT_MS = 15_000;

// Load go2rtc's video-stream.js web component exactly once per app
// lifetime. Subsequent CameraTile mounts reuse the already-registered
// custom element. The ES module registers `<video-stream>` globally
// via customElements.define(), so we just wait for whenDefined() to
// resolve to know the element is ready.
//
// Why a script tag instead of dynamic import(): Vite's module
// resolver tries to statically analyze import() calls and gets
// confused by the dynamic URL. A plain <script type="module"> tag
// bypasses Vite entirely — the browser's native ES module loader
// fetches and evaluates the script.
let videoStreamReady: Promise<void> | null = null;
function loadVideoStreamScript(baseUrl: string): Promise<void> {
  if (videoStreamReady) return videoStreamReady;
  videoStreamReady = new Promise((resolve, reject) => {
    const existing = document.getElementById("go2rtc-video-stream-script");
    if (existing) {
      customElements
        .whenDefined("video-stream")
        .then(() => resolve())
        .catch(reject);
      return;
    }
    const script = document.createElement("script");
    script.id = "go2rtc-video-stream-script";
    script.type = "module";
    script.src = `${baseUrl}/video-stream.js`;
    script.onload = () => {
      customElements
        .whenDefined("video-stream")
        .then(() => resolve())
        .catch(reject);
    };
    script.onerror = () =>
      reject(new Error(`failed to load ${script.src}`));
    document.head.appendChild(script);
  });
  return videoStreamReady;
}

// The custom element from go2rtc's video-stream.js. We reach in via
// known properties (video, src, mode, background) which are part of
// the VideoRTC public interface defined in go2rtc's video-rtc.js.
type VideoStreamElement = HTMLElement & {
  video: HTMLVideoElement | null;
  src: string;
  mode: string;
  background: boolean;
};

export function CameraTile({
  camera,
  onClick,
  isMotionActive,
  go2rtcBaseUrl,
}: {
  camera: Camera;
  onClick: () => void;
  isMotionActive: boolean;
  // Plumbed through from the WS snapshot. Null until the snapshot
  // arrives or when go2rtc is not running; null = render the
  // "connecting" overlay without any network requests.
  go2rtcBaseUrl: string | null;
}) {
  const [clock, setClock] = useState(formatNow);
  const [hasFirstFrame, setHasFirstFrame] = useState(false);
  const [connectionFailed, setConnectionFailed] = useState(false);
  const [retryKey, setRetryKey] = useState(0);
  const [isFirstConnect] = useState(() => !getSeenCameras().has(camera.id));
  // Per-camera audio mute state. Default is muted so browser
  // autoplay works without a user gesture. User toggles per tile
  // via the speaker icon in the bottom-right; persisted in
  // localStorage so the setting survives reloads.
  const [muted, setMuted] = useState(() => getCameraMutePref(camera.id));

  const containerRef = useRef<HTMLDivElement | null>(null);
  const elementRef = useRef<VideoStreamElement | null>(null);

  useEffect(() => {
    const id = setInterval(() => setClock(formatNow()), 1000);
    return () => clearInterval(id);
  }, []);

  // Instantiate the <video-stream> custom element once the
  // video-stream.js script has loaded and the go2rtcBaseUrl is known.
  // We manage the element imperatively (document.createElement +
  // appendChild) because JSX doesn't recognize custom element type
  // signatures without a separate .d.ts declaration and we want to
  // set the internal video.controls / video.muted after mount.
  useEffect(() => {
    if (!go2rtcBaseUrl) return;
    const container = containerRef.current;
    if (!container) return;

    let cancelled = false;
    let element: VideoStreamElement | null = null;

    loadVideoStreamScript(go2rtcBaseUrl)
      .then(() => {
        if (cancelled) return;
        // Create the custom element and attach it. VideoRTC's
        // connectedCallback creates the internal <video> element on
        // append, so by the time appendChild returns, element.video
        // should be populated.
        element = document.createElement(
          "video-stream",
        ) as VideoStreamElement;
        // background=true tells the element NOT to pause the stream
        // when the tab is hidden — we want recorders to keep pulling
        // fresh frames so a switch-back-to-tab doesn't show stale
        // video.
        element.background = true;
        // Mode preference order: WebRTC first (sub-second latency,
        // the ideal for live NVR viewing), then MSE over WebSocket
        // (also low latency, handles codec weirdness better than
        // HLS), then HLS and MJPEG as last-resort fallbacks.
        element.mode = "webrtc,mse,hls,mjpeg";
        element.src = `${go2rtcBaseUrl}/api/ws?src=${encodeURIComponent(
          camera.id,
        )}`;
        element.style.display = "block";
        element.style.position = "absolute";
        element.style.inset = "0";
        element.style.width = "100%";
        element.style.height = "100%";
        container.appendChild(element);
        elementRef.current = element;

        // Access the internal <video> element after mount:
        //   - Disable native controls (we want a clean tile with
        //     our own overlays; the play button was from the
        //     default controls=true)
        //   - Set muted to the user's per-camera preference
        //     (default: muted, so autoplay works)
        //   - Ensure autoplay + playsInline + object-cover styling
        const video = element.video;
        if (video) {
          video.controls = false;
          video.muted = muted;
          video.autoplay = true;
          video.playsInline = true;
          video.style.objectFit = "cover";
          video.style.width = "100%";
          video.style.height = "100%";
          // onCanPlay fires when the browser can begin playback —
          // equivalent to "first frame is ready to render."
          const onCanPlay = () => {
            setHasFirstFrame(true);
            markCameraSeen(camera.id);
          };
          video.addEventListener("canplay", onCanPlay);
          video.addEventListener("playing", onCanPlay);
          // Record the listener for cleanup.
          (element as unknown as { _canPlayCleanup?: () => void })._canPlayCleanup =
            () => {
              video.removeEventListener("canplay", onCanPlay);
              video.removeEventListener("playing", onCanPlay);
            };
        }
      })
      .catch((e) => {
        if (cancelled) return;
        // eslint-disable-next-line no-console
        console.error("failed to load video-stream.js:", e);
        setConnectionFailed(true);
      });

    return () => {
      cancelled = true;
      if (element) {
        try {
          const cleanup = (element as unknown as { _canPlayCleanup?: () => void })
            ._canPlayCleanup;
          if (cleanup) cleanup();
          element.remove();
        } catch {
          /* best-effort */
        }
      }
      elementRef.current = null;
    };
    // muted intentionally NOT in deps — we mutate video.muted
    // directly in the toggle handler below instead of recreating the
    // element every time the user clicks the mute button.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [camera.id, retryKey, go2rtcBaseUrl]);

  // Failure-timeout watchdog: if the element hasn't fired canplay /
  // playing within FIRST_FRAME_TIMEOUT_MS, show the manual retry
  // button. 15s covers slow camera handshakes without letting a
  // truly-dead stream spin forever.
  useEffect(() => {
    if (hasFirstFrame) return;
    if (!go2rtcBaseUrl) return;
    const timer = setTimeout(() => {
      setConnectionFailed(true);
    }, FIRST_FRAME_TIMEOUT_MS);
    return () => clearTimeout(timer);
  }, [hasFirstFrame, retryKey, go2rtcBaseUrl]);

  // Reset state on retry so the next mount starts fresh.
  useEffect(() => {
    setHasFirstFrame(false);
    setConnectionFailed(false);
  }, [retryKey, camera.id]);

  const displayName =
    camera.name ||
    [camera.manufacturer, camera.model].filter(Boolean).join(" ") ||
    camera.ip;

  const showOverlay = !hasFirstFrame && !connectionFailed;
  const showError = connectionFailed;

  // Click-through handler for the click-catcher overlay. We want the
  // tile to navigate to browse-footage on click, but NOT when the
  // user clicks the mute button. The button has its own onClick that
  // stopPropagation's so clicks there don't reach this handler.
  const handleTileClick = () => {
    onClick();
  };

  // Toggle audio for this specific camera. Reaches into the live
  // element and flips video.muted directly — no React rerender of
  // the whole custom element (which would interrupt playback).
  const toggleMute = (e: React.MouseEvent) => {
    e.stopPropagation();
    const element = elementRef.current;
    if (!element) return;
    const next = !muted;
    setMuted(next);
    setCameraMutePref(camera.id, next);
    if (element.video) {
      element.video.muted = next;
      // When unmuting, browsers may require a fresh play() call
      // because the first autoplay was permitted under muted-only
      // policy. Retry play() here so the audio actually starts.
      if (!next) {
        element.video.play().catch(() => {
          // Re-mute and warn if unmute autoplay is blocked —
          // browser requires a user gesture we can't synthesize.
          setMuted(true);
          setCameraMutePref(camera.id, true);
          if (element.video) element.video.muted = true;
        });
      }
    }
  };

  return (
    <div
      className={`bg-[#0a0a0a] relative h-full w-full min-h-0 overflow-hidden cursor-pointer group ${
        isMotionActive
          ? "outline outline-2 outline-red-500 outline-offset-[-2px] animate-pulse"
          : ""
      }`}
    >
      {/* Imperatively-managed container for the <video-stream> custom
          element. The useEffect above creates, configures, and
          cleans up the element; React never touches its children. */}
      <div ref={containerRef} className="absolute inset-0" />

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

      {/* Click catcher — forwards clicks on the video area to the
          tile's onClick (browse footage) without interfering with
          the <video-stream> element's own internal controls. Hidden
          during error state so the Retry button underneath stays
          clickable. */}
      {!connectionFailed && (
        <div
          className="absolute inset-0 cursor-pointer"
          onClick={handleTileClick}
        />
      )}

      {/* Hover "Browse footage" affordance. pointer-events-none so
          the click passes through to the click catcher underneath. */}
      {!connectionFailed && (
        <div className="absolute inset-0 bg-black/0 group-hover:bg-black/30 transition-colors flex items-center justify-center opacity-0 group-hover:opacity-100 pointer-events-none">
          <div className="bg-black/70 text-white text-xs font-semibold px-3 py-1.5 rounded">
            Browse footage →
          </div>
        </div>
      )}

      {/* Top status bar — name, motion flag, REC indicator */}
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

      {/* Bottom bar — live clock + mute toggle */}
      <div className="absolute bottom-0 left-0 right-0 px-3 py-2 flex justify-between items-end bg-gradient-to-t from-black/70 to-transparent">
        <span className="text-[11px] text-white/70 font-mono tabular-nums pointer-events-none">
          {clock}
        </span>
        {!connectionFailed && hasFirstFrame && (
          <button
            onClick={toggleMute}
            title={muted ? "Unmute audio" : "Mute audio"}
            aria-label={muted ? "Unmute audio" : "Mute audio"}
            className="text-white/70 hover:text-white transition-colors p-1 rounded hover:bg-black/40"
          >
            {muted ? (
              // Muted icon — speaker with an X / slash
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
                <line
                  x1="23"
                  y1="9"
                  x2="17"
                  y2="15"
                  strokeLinecap="round"
                />
                <line
                  x1="17"
                  y1="9"
                  x2="23"
                  y2="15"
                  strokeLinecap="round"
                />
              </svg>
            ) : (
              // Unmuted icon — speaker with sound waves
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
        )}
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
