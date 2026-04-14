import { memo, useEffect, useRef, useState } from "react";
import { onTileEvent } from "tauri-plugin-rtsp-mosaic-api";
import type { Camera } from "../types";
import { cameraDisplayName } from "../lib/format";
import { useStreamFallback } from "../hooks/useStreamFallback";

// Import registers the <rtsp-tile> custom element globally.
import "tauri-plugin-rtsp-mosaic-api";

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
        status="recording"
        timestamp="live"
        muted={true}
        className="block w-full h-full"
      />

      {/* Transparent hit target — mouse events pass through the native
          tile to the webview, so this div ensures clicks register. */}
      <div className="absolute inset-0" />

      {/* Quality degraded indicator — shown when main stream failed and
          we fell back to the sub-stream. Positioned bottom-right, above
          the native timestamp overlay. */}
      {degraded && (
        <div className="absolute bottom-7 right-2.5 px-1.5 py-0.5 rounded bg-black/60 text-[10px] font-medium text-white/70 pointer-events-none">
          SD
        </div>
      )}
    </div>
  );
});
