import { memo, useEffect, useRef, useState } from "react";
import {
  onTileEvent,
  setAllVisible,
  setTileVisible,
} from "tauri-plugin-rtsp-mosaic-api";
import type { Camera } from "../types";
import { cameraDisplayName } from "../lib/format";
import { useStreamFallback } from "../hooks/useStreamFallback";

// Import registers the <rtsp-tile> custom element globally.
import "tauri-plugin-rtsp-mosaic-api";

// Module-level latch mirroring "should native tiles be visible right now."
// `setAllVisible` is a point-in-time IPC — it iterates the currently-live
// tiles — so a tile created AFTER the last setAllVisible call (reconnect,
// Windows warmup attempt bump, src fallback) starts visible by default and
// pops over whichever screen the user is on. Every tile mount re-applies
// this latch via setTileVisible as soon as its tileId lands.
let desiredTilesVisible = true;

export function setDesiredTilesVisible(visible: boolean): void {
  desiredTilesVisible = visible;
  setAllVisible(visible).catch(() => {});
}

// ---------------------------------------------------------------------------
// NativeCameraTile — thin React wrapper around <rtsp-tile>.
//
// The custom element handles the full native tile lifecycle: create on
// connectedCallback, position via ResizeObserver, destroy on disconnect.
// This wrapper bridges React props → element properties/attributes.
//
// The plugin renders overlays (name, status, timestamp, motion, mute
// button) natively as CATextLayers on macOS — no DOM overlays needed.
// Mouse events pass through the native tile to the webview, so the
// wrapper div receives clicks for focus navigation. On Windows, the
// native ⛶ button dispatches a synthetic click on the rtsp-tile
// element which bubbles up to this wrapper div.
// ---------------------------------------------------------------------------

// Typed ref for the <rtsp-tile> custom element properties.
interface RtspTileElement extends HTMLElement {
  motion: boolean;
  muted: boolean;
  fullscreen: boolean;
  tileId: string | null;
  toggleMute(): void;
}

export const NativeCameraTile = memo(function NativeCameraTile({
  camera,
  isMotionActive,
  isFocused,
  onToggleFocus,
}: {
  camera: Camera;
  isMotionActive: boolean;
  isFocused: boolean;
  onToggleFocus?: (cameraId: string) => void;
}) {
  const ref = useRef<RtspTileElement>(null);
  const displayName = cameraDisplayName(camera);

  const { streamId } = useStreamFallback(camera, isFocused, ref);
  const src = `rtsp://127.0.0.1:58554/${streamId}`;

  // Native status badge (top-right). DOM overlays can't sit on top of
  // the mpv view — it's ordered NSWindowOrderingMode::Above the webview
  // — so all tile status must travel through the plugin's `status`
  // prop to render natively. The plugin's color table lives in
  // tauri-plugin-rtsp-mosaic/src/overlay.rs::badge_color_for_status:
  //   "live"             → green   (not used here; playback is always live)
  //   "recording"        → red
  //   "unable to record" → red-orange  (detect-ffmpeg proves the
  //                          camera is reachable but the recorder
  //                          stopped writing bytes — MPV is rendering
  //                          live video, so OFFLINE would mislead)
  //   "degraded"         → amber   (recording-side fallback: chronic
  //                          circuit breaker tripped or the operator
  //                          pinned recording_stream_override="sub"
  //                          in Camera Setup)
  //   anything else → gray
  //
  // Viewer-side sub-stream fallback (useStreamFallback flipping the
  // local mpv renderer to sub) is deliberately NOT badged. The
  // recorder is still on main, the lower-res preview is its own
  // signal, and a focus click re-tries main automatically. Badging
  // it the same amber as recording-fallback drifted the tile out of
  // sync with both the Home topbar count and the Camera Setup panel,
  // which track only the recording-side signals.
  //
  // Precedence (most to least severe):
  //   offline → record_failing → reconnecting → degraded → recording
  const isDegraded =
    camera.health === "chronic_recording_failure" ||
    camera.recording_stream_override === "sub";
  const badgeStatus =
    camera.health === "offline"        ? "offline"          :
    camera.health === "record_failing" ? "unable to record" :
    camera.health === "stalled"        ? "reconnecting"     :
    isDegraded                         ? "degraded"         :
    "recording";

  // Warmup retry — Windows only. go2rtc is a lazy producer; the
  // upstream RTSP handshake only kicks off once a consumer subscribes.
  // If mpv attaches before go2rtc has primed the upstream the first
  // attempt gets no video track. The plugin's monitor self-heals on
  // macOS by destroying and recreating the tile, but on Windows the
  // recreate sometimes doesn't fire and the tile stays black. Drive
  // the same teardown from React: bump `attempt`, which changes the
  // element's key, which forces React to unmount (→ plugin destroys
  // tile) and remount (→ new mpv instance, fresh handshake). Backoff
  // sequence means a single bad camera doesn't spin forever; each
  // attempt gives go2rtc more time to settle.
  //
  // Upstream-health gate: retries only help when the local libmpv
  // session is sick while upstream is healthy. When the backend's
  // camera_health says anything other than "ok", the problem is
  // upstream (go2rtc stream unregistered, single-client camera being
  // dual-claimed, recorder-direct-fallback poisoning the loopback) —
  // remounting mpv against the same broken source just cycles state.
  // `camera.health` undefined means "no signal yet" (pre-first-event),
  // which we treat as permissive so fresh-launch retries still fire.
  const upstreamOk = camera.health === undefined || camera.health === "ok";
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (!navigator.userAgent.includes("Windows")) return;
    if (!upstreamOk) return;

    let cancelled = false;
    let unsub: (() => void) | null = null;
    // `warmed` is a continuous-health flag, not a one-shot latch.
    // VideoToolbox/D3D11 can emit a placeholder green/black first frame
    // during a mid-stream SPS/PPS reconfig loop; under the old latch
    // that single first_frame stuck `warmed=true` forever and the
    // watchdog never re-armed. Clearing on stalled/restarting lets the
    // retry fire again if the tile slides into one of those states.
    let warmed = false;

    const backoffMs = [3000, 5000, 8000, 13000, 21000, 30000];
    const watchdog = window.setTimeout(() => {
      if (warmed || cancelled) return;
      setAttempt((a) => a + 1);
    }, backoffMs[Math.min(attempt, backoffMs.length - 1)]);

    onTileEvent((event) => {
      const el = ref.current;
      if (!el || event.tile_id !== el.tileId) return;
      if (event.kind === "first_frame" || event.kind === "resumed") {
        warmed = true;
      } else if (event.kind === "stalled" || event.kind === "restarting") {
        warmed = false;
      } else if (event.kind === "failed" && !warmed && !cancelled) {
        window.clearTimeout(watchdog);
        setAttempt((a) => a + 1);
      }
    }).then((u) => {
      if (cancelled) {
        u();
        return;
      }
      unsub = u;
    });

    return () => {
      cancelled = true;
      window.clearTimeout(watchdog);
      unsub?.();
    };
  }, [attempt, src, upstreamOk]);

  // Sync motion via the element's JS property.
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.motion = isMotionActive;
  }, [isMotionActive]);

  // Re-apply the host's current visibility intent every time this tile
  // mounts (or remounts via attempt/src change). createTile has no
  // visibility field so the new native surface always starts visible;
  // poll for tileId to land, then sync against the latch. Deps mirror
  // the warmup effect above — anything that causes a fresh createTile
  // call warrants a fresh re-apply.
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    let cancelled = false;
    const apply = () => {
      if (cancelled) return;
      const id = el.tileId;
      if (id) {
        if (!desiredTilesVisible) setTileVisible(id, false).catch(() => {});
        return;
      }
      requestAnimationFrame(apply);
    };
    apply();
    return () => {
      cancelled = true;
    };
  }, [attempt, src]);

  return (
    <div
      className="relative w-full h-full bg-black overflow-hidden cursor-pointer"
      onClick={() => onToggleFocus?.(camera.id)}
    >
      <rtsp-tile
        key={attempt}
        ref={ref as React.RefObject<HTMLElement>}
        src={src}
        name={displayName}
        status={badgeStatus}
        timestamp="live"
        muted={true}
        className="block w-full h-full"
      />

      {/* Transparent hit target — mouse events pass through the native
          tile to the webview, so this div ensures clicks register. */}
      <div className="absolute inset-0" />
    </div>
  );
});
