import { useCallback, useEffect, useState } from "react";
import {
  fetchMotionEventsForDate,
  fetchRecentMotionEvents,
} from "../api/client";
import type { MotionEvent } from "../types";

const POLL_MS = 10000;

export function useMotionEvents(activeMotion: Map<string, string>) {
  const [recentEvents, setRecentEvents] = useState<MotionEvent[]>([]);

  useEffect(() => {
    let cancelled = false;
    const load = () => {
      fetchRecentMotionEvents(20)
        .then((events) => {
          if (!cancelled) setRecentEvents(events);
        })
        .catch(() => {});
    };
    load();
    const id = setInterval(load, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  // Re-poll shortly after a motion event becomes active so the new event
  // appears in the recent list without waiting for the next interval.
  useEffect(() => {
    if (activeMotion.size === 0) return;
    const id = setTimeout(() => {
      fetchRecentMotionEvents(20)
        .then(setRecentEvents)
        .catch(() => {});
    }, 1500);
    return () => clearTimeout(id);
  }, [activeMotion]);

  const fetchEventsForDate = useCallback(
    (cameraId: string, date: string) =>
      fetchMotionEventsForDate(cameraId, date),
    []
  );

  return { recentEvents, activeMotion, fetchEventsForDate };
}
