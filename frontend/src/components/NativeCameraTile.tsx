import { useEffect, useRef } from "react";
import type { Camera } from "../types";
import { cameraDisplayName } from "../lib/format";

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
// click catcher below still receives clicks for focus navigation.
// ---------------------------------------------------------------------------

// Typed ref for the <rtsp-tile> custom element properties.
interface RtspTileElement extends HTMLElement {
  motion: boolean;
  muted: boolean;
  fullscreen: boolean;
  tileId: string | null;
  toggleMute(): void;
}

export function NativeCameraTile({
  camera,
  isMotionActive,
  preferSubstream,
  onClick,
}: {
  camera: Camera;
  isMotionActive: boolean;
  preferSubstream: boolean;
  onClick?: () => void;
}) {
  const ref = useRef<RtspTileElement>(null);
  const displayName = cameraDisplayName(camera);

  const streamId =
    preferSubstream && camera.substream_uri ? `${camera.id}_sub` : camera.id;
  const src = `rtsp://127.0.0.1:58554/${streamId}`;

  // Sync motion via the element's JS property.
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.motion = isMotionActive;
  }, [isMotionActive]);

  return (
    <div className="relative w-full h-full bg-black overflow-hidden">
      <rtsp-tile
        ref={ref as React.RefObject<HTMLElement>}
        src={src}
        name={displayName}
        status="recording"
        timestamp="live"
        muted={true}
        className="block w-full h-full"
      />

      {/* Click catcher — mouse events pass through the native tile,
          so clicks reach this div for focus navigation. */}
      <div
        className="absolute inset-0 cursor-pointer"
        onClick={onClick}
      />
    </div>
  );
}
