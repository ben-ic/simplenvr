import { useCallback, useEffect, useRef, useState } from "react";
import { wsUrl } from "../lib/backend";
import type {
  Camera,
  DiscoveryEvent,
  MotionEvent,
  ScanStatus,
} from "../types";

const RECONNECT_BASE = 1000;
const RECONNECT_MAX = 15000;

export function useDiscovery() {
  const [cameras, setCameras] = useState<Map<string, Camera>>(new Map());
  const [scanStatus, setScanStatus] = useState<ScanStatus>({
    scanning: false,
    last_scan: null,
    cameras_found: 0,
    cameras_online: 0,
    cameras_needs_auth: 0,
  });
  const [connected, setConnected] = useState(false);
  const [initialScanDone, setInitialScanDone] = useState(false);
  // Recent motion events pre-seeded from the WS snapshot on connect.
  // Exposed to consumers (Inbox) as the cold-start seed so they don't
  // render "Nothing new" during the race window between mount and the
  // first REST poll completing. Null sentinels the "snapshot hasn't
  // arrived yet" state — consumers can distinguish "truly empty" from
  // "not yet hydrated" and render a spinner for the latter. See the
  // matching backend snapshot backfill in api/ws.py.
  const [recentMotionEvents, setRecentMotionEvents] = useState<
    MotionEvent[] | null
  >(null);
  const [activeMotion, setActiveMotion] = useState<Map<string, string>>(
    new Map()
  );
  // Latest recordings_deleted event, if any. Consumers subscribe via
  // useEffect to refetch their timelines when relevant camera_ids fire.
  // A bumping `at` timestamp lets the same camera_ids trigger repeated
  // effects even if the set is identical.
  const [lastRecordingsDeleted, setLastRecordingsDeleted] = useState<{
    camera_ids: string[];
    at: number;
  } | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const retriesRef = useRef(0);
  const motionTimersRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(
    new Map()
  );

  const clearMotionTimer = useCallback((cameraId: string) => {
    const t = motionTimersRef.current.get(cameraId);
    if (t) {
      clearTimeout(t);
      motionTimersRef.current.delete(cameraId);
    }
  }, []);

  const connect = useCallback(async () => {
    const url = await wsUrl("/ws/discovery");
    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      setConnected(true);
      retriesRef.current = 0;
    };

    ws.onclose = () => {
      setConnected(false);
      wsRef.current = null;
      const delay = Math.min(
        RECONNECT_BASE * Math.pow(2, retriesRef.current),
        RECONNECT_MAX
      );
      retriesRef.current++;
      setTimeout(connect, delay);
    };

    ws.onmessage = (e) => {
      const event: DiscoveryEvent = JSON.parse(e.data);

      switch (event.type) {
        case "snapshot": {
          const data = event.data as {
            cameras: Camera[];
            scan_status: ScanStatus;
            recent_motion_events?: MotionEvent[];
          };
          const map = new Map<string, Camera>();
          data.cameras.forEach((c) => map.set(c.id, c));
          setCameras(map);
          setScanStatus(data.scan_status);
          // Seed recent motion events from the snapshot. Default to
          // an empty array (NOT null) so consumers know hydration
          // finished — even an empty list is a real state.
          setRecentMotionEvents(data.recent_motion_events ?? []);
          if (data.scan_status.last_scan) {
            setInitialScanDone(true);
          }
          break;
        }
        case "camera_found": {
          const cam = (event.data as { camera: Camera }).camera;
          setCameras((prev) => new Map(prev).set(cam.id, cam));
          setInitialScanDone(true);
          break;
        }
        case "camera_lost": {
          const { camera_id } = event.data as { camera_id: string };
          setCameras((prev) => {
            const next = new Map(prev);
            const existing = next.get(camera_id);
            if (existing) {
              next.set(camera_id, { ...existing, status: "offline" });
            }
            return next;
          });
          break;
        }
        case "camera_deleted": {
          const { camera_id } = event.data as { camera_id: string };
          setCameras((prev) => {
            if (!prev.has(camera_id)) return prev;
            const next = new Map(prev);
            next.delete(camera_id);
            return next;
          });
          clearMotionTimer(camera_id);
          setActiveMotion((prev) => {
            if (!prev.has(camera_id)) return prev;
            const next = new Map(prev);
            next.delete(camera_id);
            return next;
          });
          break;
        }
        case "camera_updated": {
          const cam = (event.data as { camera: Camera }).camera;
          setCameras((prev) => new Map(prev).set(cam.id, cam));
          break;
        }
        case "scan_complete": {
          setInitialScanDone(true);
          break;
        }
        case "motion_started": {
          const { camera_id, event_id } = event.data as {
            camera_id: string;
            event_id: string;
          };
          setActiveMotion((prev) => {
            const next = new Map(prev);
            next.set(camera_id, event_id);
            return next;
          });
          clearMotionTimer(camera_id);
          const timer = setTimeout(() => {
            setActiveMotion((prev) => {
              if (!prev.has(camera_id)) return prev;
              const next = new Map(prev);
              next.delete(camera_id);
              return next;
            });
            motionTimersRef.current.delete(camera_id);
          }, 8000);
          motionTimersRef.current.set(camera_id, timer);
          break;
        }
        case "recordings_deleted": {
          const { camera_ids } = event.data as { camera_ids: string[] };
          setLastRecordingsDeleted({
            camera_ids: Array.isArray(camera_ids) ? camera_ids : [],
            at: Date.now(),
          });
          break;
        }
        case "camera_health": {
          // Recorder-observed health transition. Distinct from
          // camera.status (discovery view). Two payload shapes:
          //   { camera_id, health: "ok"|"stalled"|"record_failing"|"offline", last_frame_at }
          //   { camera_id, health: "chronic_recording_failure", reason }
          // The chronic variant does NOT carry last_frame_at — packets
          // haven't stopped, the recorder just auto-switched to the
          // sub-stream. Clobbering the existing last_frame_at with
          // undefined would regress the "last live" label, so keep
          // last_frame_at untouched on chronic.
          //
          // We also optimistically set fallback_reason on chronic so
          // the UI can show the Retry affordance immediately, without
          // waiting for the next camera_updated / snapshot to carry
          // the persisted field through.
          //
          // Silently dropped if we don't know this camera yet
          // (shouldn't happen but event ordering on WS reconnect is
          // not strictly guaranteed).
          const data = event.data as {
            camera_id: string;
            health:
              | "ok"
              | "stalled"
              | "record_failing"
              | "offline"
              | "chronic_recording_failure";
            last_frame_at?: string | null;
            reason?: string;
          };
          setCameras((prev) => {
            const existing = prev.get(data.camera_id);
            if (!existing) return prev;
            const next =
              data.health === "chronic_recording_failure"
                ? {
                    ...existing,
                    health: data.health,
                    fallback_reason: data.reason ?? existing.fallback_reason,
                    recording_stream_override: "sub" as const,
                  }
                : {
                    ...existing,
                    health: data.health,
                    last_frame_at: data.last_frame_at ?? null,
                  };
            return new Map(prev).set(data.camera_id, next);
          });
          break;
        }
        case "motion_ended": {
          const { camera_id } = event.data as { camera_id: string };
          setActiveMotion((prev) => {
            if (!prev.has(camera_id)) return prev;
            const next = new Map(prev);
            next.delete(camera_id);
            return next;
          });
          clearMotionTimer(camera_id);
          break;
        }
      }
    };
  }, [clearMotionTimer]);

  useEffect(() => {
    void connect();
    const timers = motionTimersRef.current;
    return () => {
      wsRef.current?.close();
      timers.forEach((t) => clearTimeout(t));
      timers.clear();
    };
  }, [connect]);

  return {
    cameras: Array.from(cameras.values()),
    scanStatus,
    connected,
    initialScanDone,
    activeMotion,
    lastRecordingsDeleted,
    recentMotionEvents,
  };
}
