import { useEffect, useRef, useState } from "react";
import type { Camera } from "../types";
// Side-effect import: loading video-stream.js runs its
// `customElements.define('video-stream', VideoStream)` at module-
// evaluation time, so by the time any CameraTile mounts, the custom
// element is already registered and ready to instantiate. Vendored
// from go2rtc v1.9.14 (MIT-licensed) because go2rtc serves CORS only
// on /api/* endpoints — loading these files cross-origin from
// go2rtc's static file server is browser-blocked. See
// frontend/src/vendor/go2rtc/README.md for the lineage.
import "../vendor/go2rtc/video-stream.js";

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

// Reconnect backoff schedule (milliseconds). Each element is the
// delay before the Nth attempt after a failure. Capped at 30s so a
// persistently-dead camera doesn't peg the user's CPU reopening
// WebRTC peers every few seconds, but the first few retries are
// fast enough that transient go2rtc hiccups self-heal before the
// user notices. The counter resets to 0 once a first frame shows up.
//
// We never give up. Home users have PoE switches that brown out at
// night when the camera IR illuminator kicks in, cameras that power
// cycle briefly during a storm, ISP hiccups — all of these are
// minutes-long transients where the camera eventually comes back by
// itself. The backoff caps at 30s and keeps retrying forever; the
// UI escalates its wording after a few minutes so the user knows we
// haven't silently given up, but the retry loop itself never stops.
const RECONNECT_BACKOFF_MS = [2_000, 4_000, 8_000, 16_000, 30_000];

// How long the outage has to persist before the overlay escalates
// from "reconnecting" (amber, reassuring) to "camera unreachable"
// (red, error-tone). Below this threshold we keep the user calm
// through normal PoE brownouts and Wi-Fi flaps without crying wolf.
// Above it, the visual gets stronger — but the retry loop itself
// keeps running. The user still has "Try now" and the tile self-
// heals the moment the camera comes back.
const ERROR_ESCALATION_MS = 5 * 60_000;

// Stall watchdog: once the first frame has been seen, we expect
// `timeupdate` to keep firing as the video plays. If it stalls for
// this long after playback started, flip to the error state so the
// user gets a Retry button instead of a silently-frozen tile.
//
// This is the safety net for the "stuck on the first frame" failure
// mode: VideoRTC activates MSE and WebRTC simultaneously (they are
// separate `if` blocks in onopen(), not fallbacks), MSE delivers
// the first keyframe and fires canplay, then WebRTC finishes
// negotiation, onpcvideo picks WebRTC, and `this.video.srcObject`
// is swapped. If the WebRTC track then fails to deliver decodable
// frames (Tauri WebView codec limit, bad handshake, etc.) the video
// element is left parked on whatever MSE delivered last, with no
// error event. The stall timer converts that into a visible
// "Can't reach" state plus a retry.
const STALL_TIMEOUT_MS = 5_000;

// The custom element from go2rtc's video-stream.js. We reach in via
// known properties (video, src, mode, pcConfig) which are part of
// the VideoRTC public interface defined in go2rtc's video-rtc.js.
// `ws` and `pc` are internal transport handles we defensively close
// during cleanup — see the effect below for why.
//
// Intentionally NOT using `background`: setting it true makes
// VideoRTC.disconnectedCallback() a no-op, so removing the element
// from the DOM leaks its WebSocket / RTCPeerConnection and every
// React Strict Mode double-mount + every retry piles orphan
// subscribers onto go2rtc. In a desktop Tauri app the tab-hidden
// behavior is irrelevant, so leaving background at the VideoRTC
// default (false) is the right call.
type VideoStreamElement = HTMLElement & {
  video: HTMLVideoElement | null;
  src: string;
  mode: string;
  pcConfig: RTCConfiguration;
  ws?: WebSocket | null;
  pc?: RTCPeerConnection | null;
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
  // Set to true the first time a frame lands and NEVER reset for the
  // life of this tile mount. Used to distinguish two visually-distinct
  // outage states:
  //   false + !hasFirstFrame → first-connect: centered spinner +
  //                            "Connecting to X…" (user needs full
  //                            feedback because the tile is black
  //                            and there's nothing else to look at)
  //   true  + !hasFirstFrame → re-connect after a working stream:
  //                            corner badge only, click-through to
  //                            Browse footage stays live, no full-
  //                            tile takeover (matches Frigate/Blue
  //                            Iris/OBS behavior — users expect the
  //                            tile to self-heal silently through
  //                            PoE brownouts and brief outages)
  const [hadFirstFrameOnce, setHadFirstFrameOnce] = useState(false);
  // Counter of failed attempts since the last successful first frame.
  // 0 means "currently connected OR on the first attempt". Drives the
  // backoff schedule and is reset whenever a frame shows up.
  const [retryAttempt, setRetryAttempt] = useState(0);
  // Wall-clock timestamp (Date.now()) when the next auto-reconnect
  // fires. Null when we aren't currently waiting to reconnect. Used
  // to render a live "Reconnecting in Ns…" countdown without
  // re-scheduling the timer on each tick.
  const [reconnectAt, setReconnectAt] = useState<number | null>(null);
  const [nowTick, setNowTick] = useState(0);
  // Wall-clock timestamp of the first failure in the current outage
  // burst. Reset to null on successful first frame. Drives the
  // "escalate to red error state after 5 minutes" UI — we use the
  // real elapsed time instead of an attempt count so that home users
  // with stable networks and the occasional brownout hit the
  // escalation slowly, while a user staring at a truly-dead camera
  // gets the stronger signal at the same 5-minute mark regardless
  // of how the backoff schedule lined up.
  const [firstFailureAt, setFirstFailureAt] = useState<number | null>(null);
  // Bumped when the reconnect timer fires (or the user clicks "Try
  // now") to re-run the element-init effect with a fresh transport.
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

    // Create the custom element and attach it. The vendored
    // video-stream.js module is imported at the top of this file,
    // so customElements.define() has already run by the time this
    // effect executes. VideoRTC's connectedCallback creates the
    // internal <video> element on append, so by the time
    // appendChild returns, element.video should be populated.
    const element = document.createElement(
      "video-stream",
    ) as VideoStreamElement;
    // Override the WebRTC ICE servers config to empty. The vendored
    // video-rtc.js defaults to Cloudflare + Google STUN servers for
    // NAT traversal across the public internet. SimpleNVR is local-
    // only — every browser session is on the same LAN as the
    // cameras and can reach go2rtc via host candidates without any
    // STUN at all. The default config would leak the user's public
    // IP to Cloudflare and Google on every tile connection for zero
    // benefit. If we ever ship a "view cameras from your phone over
    // the internet" feature, this override needs revisiting (and
    // we'll probably want a self-hosted TURN server, not third-
    // party STUN).
    element.pcConfig = { iceServers: [] };
    // Mode list. VideoRTC.onopen() treats the first block
    // (mse/hls/mp4) as if/else-if but WebRTC as a SEPARATE if, so
    // with both "webrtc" and "mse" present BOTH transports activate
    // simultaneously. MSE delivers the first keyframe, then WebRTC
    // finishes ICE negotiation and onpcvideo() swaps srcObject to
    // the WebRTC track. We keep WebRTC in the list because the
    // handover gives us ~200ms latency when it works, but the stall
    // watchdog below catches the case where the handover produces
    // a frozen video element.
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
    //   - Disable native controls (clean tile, no play button)
    //   - Set muted to the per-camera localStorage preference
    //   - Ensure autoplay + playsInline + object-cover styling
    const video = element.video;
    let cleanup: (() => void) | null = null;
    let stallTimer: ReturnType<typeof setTimeout> | null = null;
    // Once we've decided playback is broken (video error, stall
    // timeout, etc.) we tear the element out of the DOM so VideoRTC's
    // internal reconnect loop stops. The retry button then bumps
    // retryKey to re-run the effect and build a fresh element.
    let tornDown = false;

    // Hard-kill the element and all of its transports. VideoRTC's
    // own error handler calls `this.ws.close()` to trigger a
    // reconnect, which in a persistently-broken stream (codec
    // mismatch, bad fMP4 init segment, etc.) loops forever and
    // spams `[VideoRTC] Video error:` into the console every
    // reconnect cycle. Removing the element from the DOM triggers
    // disconnectedCallback which closes ws + pc and breaks the loop.
    const forceTeardown = () => {
      if (tornDown) return;
      tornDown = true;
      if (stallTimer) {
        clearTimeout(stallTimer);
        stallTimer = null;
      }
      try {
        if (element.ws) element.ws.close();
      } catch {
        /* best-effort */
      }
      try {
        if (element.pc) element.pc.close();
      } catch {
        /* best-effort */
      }
      try {
        element.remove();
      } catch {
        /* best-effort */
      }
    };

    const scheduleReconnect = () => {
      // Bump attempt counter and schedule the next retry. The effect
      // watching reconnectAt arms the actual setTimeout; we just set
      // the wall-clock deadline here so the countdown UI can show a
      // stable target instead of drifting on every rerender.
      setFirstFailureAt((prev) => prev ?? Date.now());
      setRetryAttempt((prev) => {
        const next = prev + 1;
        const delay =
          RECONNECT_BACKOFF_MS[
            Math.min(prev, RECONNECT_BACKOFF_MS.length - 1)
          ];
        setReconnectAt(Date.now() + delay);
        return next;
      });
    };

    const armStallTimer = () => {
      if (tornDown) return;
      if (stallTimer) clearTimeout(stallTimer);
      stallTimer = setTimeout(() => {
        // No `timeupdate` in STALL_TIMEOUT_MS after playback started.
        // Tear down and schedule an auto-reconnect with backoff.
        forceTeardown();
        scheduleReconnect();
      }, STALL_TIMEOUT_MS);
    };

    if (video) {
      video.controls = false;
      video.muted = muted;
      video.autoplay = true;
      video.playsInline = true;
      video.style.objectFit = "cover";
      video.style.width = "100%";
      video.style.height = "100%";

      // canplay / playing fire once the browser can begin playback —
      // our "first frame ready" signal. Also arms the stall watchdog
      // so a subsequent silent freeze doesn't leave the tile stuck.
      const onCanPlay = () => {
        if (tornDown) return;
        setHasFirstFrame(true);
        setHadFirstFrameOnce(true);
        // A successful frame resets the backoff counter so the next
        // failure starts fresh from the shortest delay. Without this,
        // a camera that flaps every few minutes would eventually wind
        // up in the 30s bucket and stay there, making recovery from
        // a transient hiccup feel like a permanent outage.
        setRetryAttempt(0);
        setReconnectAt(null);
        setFirstFailureAt(null);
        markCameraSeen(camera.id);
        armStallTimer();
      };
      // timeupdate fires as currentTime advances — our "still
      // playing" heartbeat. Resets the stall timer on every tick.
      const onTimeUpdate = () => {
        armStallTimer();
      };
      // VideoRTC installs its own video error handler that closes
      // the WebSocket to trigger a reconnect. That reconnect will
      // fail again against a persistently-broken stream and log
      // `[VideoRTC] Video error:` every time, spamming the console
      // indefinitely. forceTeardown() cuts the whole thing off at
      // the knees so the user sees the "Can't reach" state and
      // hits Retry to start fresh.
      const onVideoError = () => {
        forceTeardown();
        scheduleReconnect();
      };

      video.addEventListener("canplay", onCanPlay);
      video.addEventListener("playing", onCanPlay);
      video.addEventListener("timeupdate", onTimeUpdate);
      video.addEventListener("error", onVideoError);
      cleanup = () => {
        video.removeEventListener("canplay", onCanPlay);
        video.removeEventListener("playing", onCanPlay);
        video.removeEventListener("timeupdate", onTimeUpdate);
        video.removeEventListener("error", onVideoError);
      };
    }

    return () => {
      if (cleanup) cleanup();
      if (stallTimer) clearTimeout(stallTimer);

      // Force-disconnect the internal transports. VideoRTC's
      // disconnectedCallback() handles this on element.remove() as
      // long as `background` is false (which it now is), but we
      // reach in and close ws + pc directly as a belt-and-suspenders
      // so React Strict Mode's double-mount cleanup can't leave
      // orphan subscribers connected to go2rtc.
      try {
        if (element.ws) element.ws.close();
      } catch {
        /* best-effort */
      }
      try {
        if (element.pc) element.pc.close();
      } catch {
        /* best-effort */
      }
      try {
        element.remove();
      } catch {
        /* best-effort */
      }
      elementRef.current = null;
    };
    // muted intentionally NOT in deps — we mutate video.muted
    // directly in the toggle handler below instead of recreating the
    // element every time the user clicks the mute button.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [camera.id, retryKey, go2rtcBaseUrl]);

  // Failure-timeout watchdog: if the element hasn't fired canplay /
  // playing within FIRST_FRAME_TIMEOUT_MS, count this attempt as a
  // failure and schedule an auto-reconnect. Previously this flipped
  // a sticky "connectionFailed" flag and asked the user to click
  // Retry — which got old fast when a camera was briefly offline.
  useEffect(() => {
    if (hasFirstFrame) return;
    if (!go2rtcBaseUrl) return;
    // Don't arm the first-frame timeout while we're already waiting
    // for a scheduled reconnect — otherwise we'd race with the
    // scheduler and double-bump the attempt counter.
    if (reconnectAt !== null) return;
    const timer = setTimeout(() => {
      setFirstFailureAt((prev) => prev ?? Date.now());
      setRetryAttempt((prev) => {
        const delay =
          RECONNECT_BACKOFF_MS[
            Math.min(prev, RECONNECT_BACKOFF_MS.length - 1)
          ];
        setReconnectAt(Date.now() + delay);
        return prev + 1;
      });
    }, FIRST_FRAME_TIMEOUT_MS);
    return () => clearTimeout(timer);
  }, [hasFirstFrame, retryKey, go2rtcBaseUrl, reconnectAt]);

  // The reconnect scheduler: when reconnectAt is set, wait until it
  // passes then bump retryKey to force the element-init effect to
  // re-run with a fresh transport. Split from the failure handlers
  // so the countdown UI has a single stable deadline to read.
  useEffect(() => {
    if (reconnectAt === null) return;
    const delay = Math.max(0, reconnectAt - Date.now());
    const fire = setTimeout(() => {
      setReconnectAt(null);
      setHasFirstFrame(false);
      setRetryKey((k) => k + 1);
    }, delay);
    // Tick once a second so the "Reconnecting in Ns…" label updates.
    const tick = setInterval(() => setNowTick((t) => t + 1), 1000);
    return () => {
      clearTimeout(fire);
      clearInterval(tick);
    };
  }, [reconnectAt]);

  // Reset per-attempt UI state on each retry so the next mount
  // starts fresh. The attempt counter itself persists across retries
  // so the backoff schedule progresses.
  useEffect(() => {
    setHasFirstFrame(false);
  }, [retryKey, camera.id]);

  // Keep the "last live Nm" label fresh during an outage. The
  // reconnect-scheduler effect above also ticks once a second but
  // only while a retry is scheduled — during an active retry
  // attempt (reconnectAt=null, retryAttempt>0) there would be no
  // rerender and the elapsed label would freeze. This effect
  // covers the gap by ticking whenever an outage is in progress.
  useEffect(() => {
    if (firstFailureAt === null) return;
    const tick = setInterval(() => setNowTick((t) => t + 1), 1000);
    return () => clearInterval(tick);
  }, [firstFailureAt]);

  const displayName =
    camera.name ||
    [camera.manufacturer, camera.model].filter(Boolean).join(" ") ||
    camera.ip;

  // Derived UI state:
  //   hasFirstFrame=true                → playing, no overlay
  //   reconnectAt set                   → waiting out the backoff window
  //   retryAttempt>0, reconnectAt=null  → actively retrying (new transport)
  //   retryAttempt=0, no first frame    → first-time connecting
  const isWaitingForRetry = reconnectAt !== null;
  const isReconnecting = retryAttempt > 0 && !hasFirstFrame;
  // Countdown seconds for the waiting-for-retry label. Recomputed
  // once a second via nowTick (see the reconnect scheduler effect).
  void nowTick;
  const secondsUntilRetry = isWaitingForRetry
    ? Math.max(0, Math.ceil((reconnectAt! - Date.now()) / 1000))
    : 0;

  // Two independent outage signals can fire the corner badge:
  //   (a) FRONTEND transport loss — go2rtc WS closed, WebRTC failed,
  //       frame stall after playing. Detected locally. Already drives
  //       hadFirstFrameOnce && !hasFirstFrame.
  //   (b) BACKEND recorder outage — camera.health is stalled/offline.
  //       The ffmpeg that's writing segments hasn't seen packets from
  //       the camera. Authoritative: if the recorder says packets
  //       stopped, the camera is in some sense down regardless of
  //       what the frontend's WS pipe is doing.
  // Render one badge covering whichever signal (or both) is active.
  const backendOffline = camera.health === "offline";
  const backendStalled = camera.health === "stalled";
  const backendUnhealthy = backendOffline || backendStalled;
  const frontendOutage = hadFirstFrameOnce && !hasFirstFrame;
  // Prefer the backend's last_frame_at for the "ago" label when
  // available — it's the authoritative wall-clock of the last
  // packet the recorder saw, whereas firstFailureAt is just when
  // the frontend noticed things were wrong (could lag by seconds
  // or be ahead by seconds depending on which end broke first).
  const backendLastFrameMs = camera.last_frame_at
    ? Date.now() - new Date(camera.last_frame_at).getTime()
    : null;
  const outageDurationMs =
    backendLastFrameMs !== null
      ? backendLastFrameMs
      : firstFailureAt !== null
        ? Date.now() - firstFailureAt
        : 0;
  // Escalated visual once the outage has lasted long enough that
  // we'd rather be explicit than reassuring. Backend "offline" is
  // immediately escalated regardless of elapsed time — the recorder
  // has already waited 60s of silence to decide this, so we don't
  // need to double-buffer it on the frontend.
  const isEscalated =
    backendOffline || outageDurationMs >= ERROR_ESCALATION_MS;
  const showOutageBadge =
    (frontendOutage || backendUnhealthy) && hadFirstFrameOnce;
  const showFirstConnectOverlay = !hadFirstFrameOnce && !hasFirstFrame;

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

      {/* First-connect full overlay. Only shown on the very first
          attempt for this tile mount — subsequent outages fall
          through to the corner badge below. The tile is black with
          nothing to see on first connect, so a full overlay with a
          centered spinner is the right amount of feedback. */}
      {showFirstConnectOverlay && (
        <div
          className="absolute inset-0 flex flex-col items-center justify-center bg-[#0a0a0a] text-center px-6 pointer-events-none"
        >
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
              Connecting to <span className="text-white">{displayName}</span>…
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

      {/* Outage corner badge. Fires on either a frontend transport
          loss OR a backend recorder reporting stalled/offline. The
          badge label picks the most authoritative signal: if the
          backend says "offline" (packets stopped at the recorder),
          we say so explicitly; otherwise we label it "Reconnecting"
          since it's a transport issue we're actively retrying. Tile
          stays clickable during the outage so Browse-footage
          navigation still works. */}
      {showOutageBadge && (
        <>
          {/* Dim the tile so the badge reads cleanly over whatever
              last frame (or black) is behind it. Pointer-events-none
              so the click catcher under the badge still handles
              Browse-footage navigation during the outage. */}
          <div className="absolute inset-0 bg-black/60 pointer-events-none" />
          <div
            className="absolute top-10 left-3 right-3 flex items-center gap-2 pointer-events-none"
          >
            <div
              className={`flex items-center gap-1.5 px-2 py-1 rounded text-[11px] font-semibold backdrop-blur ${
                isEscalated
                  ? "bg-red-500/20 text-red-300 border border-red-500/40"
                  : "bg-amber-500/20 text-amber-300 border border-amber-500/40"
              }`}
            >
              {backendOffline ? (
                // Static warning icon for backend-confirmed outage
                // (we're not "trying" in the WebRTC sense; the
                // recorder has already decided packets aren't
                // flowing). Less spinny than the transport case.
                <svg
                  className="w-3 h-3"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth={2.5}
                  viewBox="0 0 24 24"
                >
                  <circle cx="12" cy="12" r="10" />
                  <line x1="12" y1="8" x2="12" y2="12" />
                  <line x1="12" y1="16" x2="12.01" y2="16" />
                </svg>
              ) : (
                <svg
                  className="w-3 h-3 animate-spin"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth={2.5}
                  viewBox="0 0 24 24"
                >
                  <path
                    d="M21 12a9 9 0 1 1-6.219-8.56"
                    strokeLinecap="round"
                  />
                </svg>
              )}
              <span>
                {backendOffline
                  ? "Camera offline"
                  : backendStalled
                    ? "Stalled"
                    : "Reconnecting"}
                {!backendOffline &&
                isWaitingForRetry &&
                secondsUntilRetry > 0
                  ? ` · ${secondsUntilRetry}s`
                  : !backendOffline
                    ? "…"
                    : ""}
              </span>
            </div>
            {outageDurationMs > 0 && (
              <div
                className={`px-2 py-1 rounded text-[11px] backdrop-blur ${
                  isEscalated
                    ? "bg-red-500/10 text-red-300/90"
                    : "bg-black/50 text-white/70"
                }`}
              >
                last live {formatAgo(outageDurationMs)}
              </div>
            )}
            {isWaitingForRetry && !backendOffline && (
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  // "Try now" — fire the scheduled reconnect
                  // immediately without resetting the backoff
                  // counter, so if it fails again the next wait is
                  // still the longer one.
                  setReconnectAt(null);
                  setHasFirstFrame(false);
                  setRetryKey((k) => k + 1);
                }}
                className="pointer-events-auto px-2 py-1 text-[11px] font-semibold text-white/80 bg-black/50 backdrop-blur border border-white/10 rounded hover:bg-black/70 hover:text-white transition-colors"
              >
                Try now
              </button>
            )}
          </div>
        </>
      )}

      {/* Click catcher — forwards clicks on the video area to the
          tile's onClick (browse footage). Active during outages too
          once the tile has ever worked: when live view is down,
          clicking through to recorded footage is often exactly what
          the user wants. Only hidden during the very first connect
          (nothing to click through to yet) so the "Try now" button
          on the corner badge during outage stays reachable via its
          own pointer-events-auto. */}
      {hadFirstFrameOnce && (
        <div
          className="absolute inset-0 cursor-pointer"
          onClick={handleTileClick}
        />
      )}

      {/* Hover "Browse footage" affordance. pointer-events-none so
          the click passes through to the click catcher underneath. */}
      {hadFirstFrameOnce && (
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
        {hasFirstFrame && (
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

function formatAgo(ms: number): string {
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m ago`;
}

function formatNow(): string {
  const d = new Date();
  return `${String(d.getHours()).padStart(2, "0")}:${String(
    d.getMinutes(),
  ).padStart(2, "0")}:${String(d.getSeconds()).padStart(2, "0")}`;
}
