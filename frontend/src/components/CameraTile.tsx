import { useEffect, useState } from "react";
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

// How long to wait for the iframe to load before showing a failure
// state. The iframe itself handles retries internally, so this is
// mostly about telling the user something is wrong if go2rtc's own
// player sits on "loading" forever (camera offline, stream never
// came up, etc.).
const FIRST_FRAME_TIMEOUT_MS = 15_000;

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

  useEffect(() => {
    const id = setInterval(() => setClock(formatNow()), 1000);
    return () => clearInterval(id);
  }, []);

  // Failure-timeout watchdog: if the iframe hasn't flipped
  // hasFirstFrame true within FIRST_FRAME_TIMEOUT_MS, show the manual
  // retry button. hasFirstFrame is set on the iframe's onLoad event
  // (which fires once the initial HTML + video-rtc.js has been parsed
  // — NOT when the first frame of video actually renders, because
  // iframe contents don't expose that event to the parent). A 15s
  // budget covers slow camera handshakes while still surfacing
  // genuinely dead streams.
  useEffect(() => {
    if (hasFirstFrame) return;
    if (!go2rtcBaseUrl) return;
    const timer = setTimeout(() => {
      setConnectionFailed(true);
    }, FIRST_FRAME_TIMEOUT_MS);
    return () => clearTimeout(timer);
  }, [hasFirstFrame, retryKey, go2rtcBaseUrl]);

  // Reset state on retry so the next iframe reload starts fresh.
  useEffect(() => {
    setHasFirstFrame(false);
    setConnectionFailed(false);
  }, [retryKey, camera.id, go2rtcBaseUrl]);

  const displayName =
    camera.name ||
    [camera.manufacturer, camera.model].filter(Boolean).join(" ") ||
    camera.ip;

  // go2rtc's built-in web player. stream.html is a ~2KB HTML shim
  // that loads video-stream.js + video-rtc.js (the go2rtc WebRTC web
  // component, 695 lines) and instantiates a player with the given
  // camera ID. The player negotiates WebRTC first (sub-second
  // latency, the right answer for live NVR preview), falls back to
  // MSE over WebSocket, then HLS, then MJPEG, all automatically.
  //
  // Why iframe instead of importing video-rtc.js directly as a web
  // component: zero integration code. go2rtc's player handles
  // WebRTC signaling, MSE codec config, reconnect, codec fallback,
  // pause on tab hidden — all the things that would otherwise be
  // custom integration code in this React component. The cost is
  // that clicks inside the iframe don't bubble to our onClick
  // handler; we fix that by overlaying an invisible click catcher
  // (see ClickCatcher at the bottom of the render).
  //
  // Why we tried HLS directly first and gave up: go2rtc's HLS muxer
  // produces MPEG-TS segments with missing SPS/PPS NAL units for
  // camera streams that don't emit inline parameter sets in every
  // GOP (most Reolink/Tapo cameras). Verified via ffprobe on
  // 2026-04-09: "non-existing PPS 0 referenced, decode_slice_header
  // error" on every segment. Recording via go2rtc's RTSP loopback
  // path works fine because that path injects SPS/PPS correctly;
  // only the HLS muxer is broken. WebRTC bypasses the HLS muxer
  // entirely.
  const streamUrl =
    go2rtcBaseUrl !== null
      ? `${go2rtcBaseUrl}/stream.html?src=${encodeURIComponent(
          camera.id,
        )}&mode=webrtc%2Cmse%2Chls%2Cmjpeg${retryKey > 0 ? `&_r=${retryKey}` : ""}`
      : null;

  const showOverlay = !hasFirstFrame && !connectionFailed;
  const showError = connectionFailed;

  return (
    <div
      className={`bg-[#0a0a0a] relative h-full w-full min-h-0 overflow-hidden cursor-pointer group ${
        isMotionActive
          ? "outline outline-2 outline-red-500 outline-offset-[-2px] animate-pulse"
          : ""
      }`}
    >
      {streamUrl && !connectionFailed && (
        <iframe
          // retryKey in the src forces the browser to hard-reload
          // the iframe on retry, giving go2rtc's player a fresh
          // start. Without it, the iframe stays on whatever failed
          // state it was in.
          key={`${camera.id}:${retryKey}`}
          src={streamUrl}
          // scrolling="no" kills the scrollbar that video-stream.js
          // can leave behind on small containers. sandbox allows
          // scripts (required for the player) and same-origin
          // (required for the WebSocket to go2rtc's own origin).
          // NO allow-top-navigation so the iframe can't redirect us.
          sandbox="allow-scripts allow-same-origin"
          scrolling="no"
          // allow=autoplay gives the iframe permission to autoplay
          // audio — critical in the hls.js MSE fallback path where
          // some browsers block unmuted playback without it. The
          // backend explicitly strips audio (ffmpeg -an) but the
          // autoplay permission is about the ELEMENT, not the
          // content, so we grant it unconditionally.
          allow="autoplay; fullscreen"
          className="absolute inset-0 w-full h-full border-0"
          onLoad={() => {
            setHasFirstFrame(true);
            markCameraSeen(camera.id);
          }}
          onError={() => {
            setConnectionFailed(true);
          }}
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

      {/* Click catcher — transparent div over the iframe that forwards
          clicks to the parent's onClick handler for "browse footage".
          Without this, the iframe's document event tree swallows clicks
          and the user can't click a tile to see its recordings.
          Hidden during error state so the Retry button underneath
          remains clickable. */}
      {!connectionFailed && (
        <div
          className="absolute inset-0 cursor-pointer"
          onClick={onClick}
        />
      )}

      {/* Hover "Browse footage" affordance. Sits above the click
          catcher so it shows on hover. pointer-events-none so the
          click passes through to the catcher underneath. */}
      {!connectionFailed && (
        <div className="absolute inset-0 bg-black/0 group-hover:bg-black/30 transition-colors flex items-center justify-center opacity-0 group-hover:opacity-100 pointer-events-none">
          <div className="bg-black/70 text-white text-xs font-semibold px-3 py-1.5 rounded">
            Browse footage →
          </div>
        </div>
      )}

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
