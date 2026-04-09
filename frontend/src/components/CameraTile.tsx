import { useEffect, useRef, useState } from "react";
import Hls from "hls.js";
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

// Auto-retry budget for transient <video> / hls.js failures. React
// Strict Mode double-mounts in dev and brief network hiccups can both
// cause the video element to emit an error event without the underlying
// stream being dead. Retrying a few times silently before surfacing the
// "Can't reach" error state is much friendlier than showing the manual
// retry button on every blip.
const AUTO_RETRY_MAX = 3;
const AUTO_RETRY_DELAY_MS = 1_500;

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
  const [hasFirstFrame, setHasFirstFrame] = useState(false);
  const [connectionFailed, setConnectionFailed] = useState(false);
  const [retryKey, setRetryKey] = useState(0);
  const [autoRetryCount, setAutoRetryCount] = useState(0);
  const videoRef = useRef<HTMLVideoElement | null>(null);
  // isFirstConnect is captured at mount time so the copy doesn't flip
  // mid-connection when we markCameraSeen() after the first frame.
  const [isFirstConnect] = useState(() => !getSeenCameras().has(camera.id));

  useEffect(() => {
    const id = setInterval(() => setClock(formatNow()), 1000);
    return () => clearInterval(id);
  }, []);

  // HLS via hls.js (Chrome/Firefox/Edge) or native <video src> (Safari).
  // The URL is resolved through apiUrl() so the frontend only ever
  // talks to the Python backend's dynamic port — go2rtc is an internal
  // implementation detail proxied by the backend at
  // /api/cameras/{id}/live.m3u8 + /api/cameras/{id}/hls/{path}. This
  // keeps cross-platform portability (Tauri WebView on Win/Mac/Linux
  // doesn't need to know go2rtc's port) and means any future port
  // collision only affects the backend side, not the UX.
  //
  // Why not fragmented MP4 (briefly tried): go2rtc's /api/stream.mp4
  // advertises a finite 3-second duration in the moov atom, which
  // browsers interpret as a VOD file — play to "end" and stop. HLS is
  // the right delivery for live streams because the playlist is
  // continuously refreshed and hls.js knows to keep polling.
  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;

    let cancelled = false;
    setHasFirstFrame(false);
    setConnectionFailed(false);

    // Safari has native HLS support. hls.js's recommended pattern
    // defers to the browser's built-in player in that case rather
    // than doing MSE — lower latency, less JS.
    const canPlayHlsNatively = !!video.canPlayType(
      "application/vnd.apple.mpegurl"
    );

    let hls: Hls | null = null;

    const handleFatal = () => {
      if (autoRetryCount < AUTO_RETRY_MAX) {
        setAutoRetryCount((n) => n + 1);
        setTimeout(() => setRetryKey((k) => k + 1), AUTO_RETRY_DELAY_MS);
      } else {
        setConnectionFailed(true);
      }
    };

    apiUrl(`/api/cameras/${camera.id}/live.m3u8`).then((url) => {
      if (cancelled) return;

      if (canPlayHlsNatively) {
        // Safari / WebKit — native HLS. Cache-buster keeps a new
        // retryKey from reusing stale <video> source state.
        const sep = url.includes("?") ? "&" : "?";
        video.src = retryKey === 0 ? url : `${url}${sep}_r=${retryKey}`;
        return;
      }

      if (!Hls.isSupported()) {
        // Browser supports neither native HLS nor MSE — unusable.
        setConnectionFailed(true);
        return;
      }

      // Chrome / Firefox / Edge / Tauri WebView on Win+Linux —
      // hls.js via MSE. Standard (NOT low-latency) config:
      //
      // Why not lowLatencyMode+liveSyncDurationCount=1: go2rtc
      // produces small 500ms segments and its playlist uses a
      // non-sliding MEDIA-SEQUENCE:0 counter that grows forever.
      // hls.js's low-latency live-edge tracker misinterpreted this
      // and froze playback on the first decoded frame (seen 2026-
      // 04-09 — tiles would show a single frame then stop). We also
      // saw dark-green half-frame artifacts from decoder ref-frame
      // starvation when the decoder started mid-GOP on Reolink
      // cameras (2s keyframe interval vs 500ms segment size).
      //
      // Standard buffering trades ~1-2s of latency for reliable
      // playback and gives the decoder enough pre-buffer to find
      // a keyframe before rendering, eliminating both symptoms.
      hls = new Hls({
        // Play 3 segments back from live edge = ~1.5s latency at
        // go2rtc's 500ms segment size.
        liveSyncDurationCount: 3,
        // 10s of forward buffer. Keeps memory bounded (TS segments
        // are small, ~100KB each at our bitrate) while giving
        // enough headroom for brief network hiccups without a stall.
        maxBufferLength: 10,
        // Enable hls.js's web worker for parsing — keeps the main
        // thread free on cams that churn a lot of segments.
        enableWorker: true,
      });
      hls.loadSource(url);
      hls.attachMedia(video);
      hls.on(Hls.Events.ERROR, (_, data) => {
        // hls.js classifies errors as fatal or recoverable. Only
        // escalate fatal ones; recoverable errors (brief network
        // hiccup, non-fatal parser blips) are handled internally.
        // Log recoverable errors to the console so they show up in
        // browser devtools during debugging without spamming the
        // React state.
        if (data.fatal) {
          handleFatal();
        } else {
          // eslint-disable-next-line no-console
          console.warn(
            `hls.js recoverable error on ${camera.id}:`,
            data.type,
            data.details
          );
        }
      });
    });

    return () => {
      cancelled = true;
      if (hls) {
        try {
          hls.destroy();
        } catch {
          /* best-effort */
        }
      }
      try {
        video.removeAttribute("src");
        video.load();
      } catch {
        /* best-effort */
      }
    };
    // autoRetryCount intentionally omitted from deps — a state update
    // inside the effect must not retrigger the effect itself.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [camera.id, retryKey]);

  // Failure-timeout watchdog: if no frame arrives within
  // FIRST_FRAME_TIMEOUT_MS, transition to the manual error state so
  // the user sees something actionable instead of an indefinite spinner.
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
      // h-full w-full instead of aspect-video: in the Home grid the
      // cell already owns the size (grid-template-rows: minmax(0,1fr)),
      // and letting the tile compute its own aspect-ratio-driven
      // height caused the grid to overflow its flex parent, which
      // dragged the sibling history panel down past the viewport.
      className={`bg-[#0a0a0a] relative h-full w-full min-h-0 overflow-hidden cursor-pointer group ${
        isMotionActive
          ? "outline outline-2 outline-red-500 outline-offset-[-2px] animate-pulse"
          : ""
      }`}
    >
      {/* The <video> element is always rendered (not gated on
          connectionFailed) so videoRef stays stable for the hls.js
          effect above. When connectionFailed flips true, the overlay
          covers it and we let hls.js teardown stop feeding it. */}
      <video
        ref={videoRef}
        // autoPlay requires muted in most browsers (autoplay policy
        // blocks unmuted playback without a user gesture). Security
        // cameras don't have audio anyway — the backend explicitly
        // strips it with -an upstream.
        autoPlay
        muted
        playsInline
        loop={false}
        className={`w-full h-full object-cover ${connectionFailed ? "hidden" : ""}`}
        // onCanPlay fires when the browser has buffered enough to play
        // without stalling — our "first frame received" signal for both
        // native-HLS (Safari) and hls.js-MSE (Chromium) paths. Reset
        // the auto-retry counter on success so future transients get
        // a fresh budget.
        onCanPlay={() => {
          setHasFirstFrame(true);
          setAutoRetryCount(0);
          markCameraSeen(camera.id);
        }}
        onError={() => {
          // Safari-native path emits MediaError on <video>. The hls.js
          // path gets Hls.Events.ERROR first, but this is a backstop
          // for the case where hls.js never successfully attached.
          if (autoRetryCount < AUTO_RETRY_MAX) {
            setAutoRetryCount((n) => n + 1);
            setTimeout(() => setRetryKey((k) => k + 1), AUTO_RETRY_DELAY_MS);
          } else {
            setConnectionFailed(true);
          }
        }}
      />

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
              setAutoRetryCount(0);
              setRetryKey((k) => k + 1);
            }}
            className="px-3 py-1 text-xs font-semibold text-[#ddd] bg-[#222] border border-[#333] rounded hover:bg-[#2a2a2a] transition-colors"
          >
            Retry
          </button>
        </div>
      )}

      {/* Hover "Browse footage" overlay. Hidden entirely when the tile
          is in its failure state so the Retry button underneath stays
          clickable. pointer-events-none is belt-and-suspenders: the
          parent <div onClick={onClick}> handles click-to-browse at the
          tile level and the overlay should never intercept clicks. */}
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
