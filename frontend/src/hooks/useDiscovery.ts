import { useCallback, useEffect, useRef, useState } from "react";
import { wsUrl } from "../lib/backend";
import type { Camera, DiscoveryEvent, ScanStatus } from "../types";

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
          };
          const map = new Map<string, Camera>();
          data.cameras.forEach((c) => map.set(c.id, c));
          setCameras(map);
          setScanStatus(data.scan_status);
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
  };
}
