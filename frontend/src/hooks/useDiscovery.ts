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
  // go2rtc base URL, also from the WS snapshot. The frontend talks
  // to go2rtc directly for live HLS preview and snapshot frames —
  // no backend proxy — because go2rtc serves CORS `*` on its admin
  // API. Null until the snapshot arrives; null also when go2rtc is
  // not running (production misconfiguration or dev_go2rtc spawn
  // failure), in which case consumers should render an error state
  // for live preview affordances.
  const [go2rtcBaseUrl, setGo2rtcBaseUrl] = useState<string | null>(null);
  const [storyEnabled, setStoryEnabled] = useState(false);
  const [activeMotion, setActiveMotion] = useState<Map<string, string>>(
    new Map()
  );
  // Model download progress from the summarizer backend.
  const [modelDownload, setModelDownload] = useState<{
    model: string;
    status: "downloading" | "done" | "error";
    message: string;
  } | null>(null);
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
            go2rtc_base_url?: string | null;
            story_enabled?: boolean;
          };
          const map = new Map<string, Camera>();
          data.cameras.forEach((c) => map.set(c.id, c));
          setCameras(map);
          setScanStatus(data.scan_status);
          // Seed recent motion events from the snapshot. Default to
          // an empty array (NOT null) so consumers know hydration
          // finished — even an empty list is a real state.
          setRecentMotionEvents(data.recent_motion_events ?? []);
          // Seed the go2rtc base URL. May be null/absent if go2rtc
          // is not running (production misconfig, dev spawn failed);
          // consumers handle that by showing an error on live tiles.
          setGo2rtcBaseUrl(data.go2rtc_base_url ?? null);
          setStoryEnabled(data.story_enabled ?? false);
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
          // camera.status (discovery view) — merges health +
          // last_frame_at into the camera record so the live tile
          // can render "last live Nm ago" during a packet outage
          // without waiting for the next discovery scan. Silently
          // dropped if we don't know this camera yet (shouldn't
          // happen but event ordering on WS reconnect is not
          // strictly guaranteed).
          const { camera_id, health, last_frame_at } = event.data as {
            camera_id: string;
            health: "ok" | "stalled" | "offline";
            last_frame_at: string | null;
          };
          setCameras((prev) => {
            const existing = prev.get(camera_id);
            if (!existing) return prev;
            return new Map(prev).set(camera_id, {
              ...existing,
              health,
              last_frame_at,
            });
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
        case "model_download": {
          const dl = event.data as {
            model: string;
            status: "downloading" | "done" | "error";
            message: string;
          };
          setModelDownload(dl);
          // Auto-dismiss "done" after 5 seconds.
          if (dl.status === "done") {
            setTimeout(() => setModelDownload(null), 5000);
          }
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
    go2rtcBaseUrl,
    modelDownload,
    storyEnabled,
  };
}
