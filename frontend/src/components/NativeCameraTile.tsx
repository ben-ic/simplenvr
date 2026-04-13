import { memo, useEffect, useRef } from "react";
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
