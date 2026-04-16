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

  const { streamId, degraded } = useStreamFallback(camera, isFocused, ref);
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
  //   "degraded"         → amber   (viewer sub-stream fallback OR recorder
  //                          circuit breaker tripped — unified signal
  //                          because the user can't act on the
  //                          difference)
  //   anything else → gray
  //
  // `degraded` (from useStreamFallback) means the frontend mpv viewer
  // exhausted main-stream retries and fell back to sub. The chronic
  // recorder state means the backend did the same thing in the other
  // direction. Either is worth telling the user about; both land on
  // the same amber badge.
  //
  // Precedence (most to least severe):
  //   offline → record_failing → reconnecting → degraded → recording
  // record_failing wins over degraded because the recorder is actively
  // failing *now*; chronic/degraded means we already recovered onto sub.
  // Stalled (a brief flap) keeps beating degraded — preserves the
  // pre-record_failing ordering where any active-recording-state label
  // wins over "recovered on sub."
  const isDegraded =
    degraded ||
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
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (!navigator.userAgent.includes("Windows")) return;

    let cancelled = false;
    let unsub: (() => void) | null = null;
    let warmed = false;

    const backoffMs = [3000, 5000, 8000, 13000, 21000, 30000];
    const watchdog = window.setTimeout(() => {
      if (warmed || cancelled) return;
      setAttempt((a) => a + 1);
    }, backoffMs[Math.min(attempt, backoffMs.length - 1)]);

    onTileEvent((event) => {
      const el = ref.current;
      if (!el || event.tile_id !== el.tileId) return;
      if (event.kind === "first_frame") {
        warmed = true;
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
  }, [attempt, src]);

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
