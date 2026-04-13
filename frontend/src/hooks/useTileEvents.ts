import { useEffect, useRef, useState } from "react";
import { onTileEvent } from "tauri-plugin-rtsp-mosaic-api";
import type { TileEvent } from "tauri-plugin-rtsp-mosaic-api";

// ---------------------------------------------------------------------------
// useTileEvents — subscribes to native tile lifecycle events and exposes
// per-tile health state to React components. Tiles emit events like
// first_frame, error, stalled, resumed, restarting, failed, and stats.
//
// Returns a Map<tileId, TileHealth> that components can read to show
// connection/error badges.
// ---------------------------------------------------------------------------

export type TileHealthKind =
  | "connecting" // initial state before first_frame
  | "live" // first_frame received, stream playing
  | "stalled" // no frames for a while
  | "restarting" // mpv reconnecting (attempt N)
  | "error" // transient error
  | "failed"; // gave up after max retries

export interface TileHealth {
  kind: TileHealthKind;
  message?: string;
  attempt?: number;
}

export function useTileEvents(): Map<string, TileHealth> {
  const [health, setHealth] = useState<Map<string, TileHealth>>(new Map());
  const healthRef = useRef(health);
  healthRef.current = health;

  useEffect(() => {
    let unsubscribe: (() => void) | null = null;
    let disposed = false;

    onTileEvent((event: TileEvent) => {
      setHealth((prev) => {
        const next = new Map(prev);
        switch (event.kind) {
          case "first_frame":
            next.set(event.tile_id, { kind: "live" });
            break;
          case "error":
            next.set(event.tile_id, {
              kind: "error",
              message: event.message,
            });
            break;
          case "stalled":
            next.set(event.tile_id, { kind: "stalled" });
            break;
          case "resumed":
            next.set(event.tile_id, { kind: "live" });
            break;
          case "restarting":
            next.set(event.tile_id, {
              kind: "restarting",
              attempt: event.attempt,
            });
            break;
          case "failed":
            next.set(event.tile_id, {
              kind: "failed",
              message: event.message,
            });
            break;
          case "stats":
            // Stats don't change health state — could be used for
            // a debug overlay later.
            break;
        }
        return next;
      });
    }).then((unsub) => {
      if (disposed) {
        unsub();
        return;
      }
      unsubscribe = unsub;
    });

    return () => {
      disposed = true;
      unsubscribe?.();
    };
  }, []);

  return health;
}
