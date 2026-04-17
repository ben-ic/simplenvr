import { useEffect, useRef, useState } from "react";
import { onTileEvent } from "tauri-plugin-rtsp-mosaic-api";
import type { Camera } from "../types";

// ---------------------------------------------------------------------------
// useStreamFallback — start on main stream, fall back to sub on failure.
//
// Returns the current streamId and whether quality has degraded. The hook
// subscribes to native tile events and watches for "failed" (max retries
// exhausted). Brief stalls/restarts are normal and don't trigger fallback.
//
// When the tile gains focus (user clicked to expand), the hook resets to
// main — the user is paying attention and wants the best quality. If main
// fails again, fallback re-triggers.
//
// Upstream-health gate: fall back to sub only when the backend's
// `camera_health` still says the camera is healthy — a "failed" event
// against a sick upstream (camera offline, go2rtc stream unregistered)
// tells us nothing about whether sub would do any better, since both
// streams ride the same camera hardware. Without this gate, a single
// camera outage permanently flips the tile to sub the moment the tile
// gives up on main, and only a focus click ever restores main.
// ---------------------------------------------------------------------------

interface RtspTileHandle {
  tileId: string | null;
}

export function useStreamFallback(
  camera: Camera,
  isFocused: boolean,
  tileRef: React.RefObject<RtspTileHandle | null>,
): { streamId: string; degraded: boolean } {
  const hasSubstream = !!camera.substream_uri;
  const [quality, setQuality] = useState<"main" | "sub">("main");
  const qualityRef = useRef(quality);
  qualityRef.current = quality;

  const upstreamOk = camera.health === undefined || camera.health === "ok";
  const upstreamOkRef = useRef(upstreamOk);
  upstreamOkRef.current = upstreamOk;

  // Reset to main when gaining focus.
  useEffect(() => {
    if (isFocused) setQuality("main");
  }, [isFocused]);

  // Subscribe to tile events; fall back on "failed".
  useEffect(() => {
    if (!hasSubstream) return;

    let unsub: (() => void) | null = null;
    let disposed = false;

    onTileEvent((event) => {
      const el = tileRef.current;
      if (!el || event.tile_id !== el.tileId) return;

      if (
        event.kind === "failed"
        && qualityRef.current === "main"
        && upstreamOkRef.current
      ) {
        setQuality("sub");
      }
    }).then((u) => {
      if (disposed) {
        u();
        return;
      }
      unsub = u;
    });

    return () => {
      disposed = true;
      unsub?.();
    };
  }, [hasSubstream, tileRef]);

  const streamId =
    quality === "sub" && hasSubstream ? `${camera.id}_sub` : camera.id;

  return { streamId, degraded: quality === "sub" };
}
