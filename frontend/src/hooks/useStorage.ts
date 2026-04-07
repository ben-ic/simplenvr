import { useEffect, useState } from "react";
import { fetchStorage, fetchStorageStats } from "../api/client";
import type { StorageStats, StorageStatus } from "../types";

const POLL_INTERVAL = 5000;
const STATS_POLL_INTERVAL = 30000;

export function useStorage() {
  const [storage, setStorage] = useState<StorageStatus | null>(null);

  useEffect(() => {
    let cancelled = false;

    const refresh = async () => {
      try {
        const data = await fetchStorage();
        if (!cancelled) setStorage(data);
      } catch {
        // Ignore — will retry on next interval
      }
    };

    refresh();
    const id = setInterval(refresh, POLL_INTERVAL);

    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  return storage;
}

export function useStorageStats() {
  const [stats, setStats] = useState<StorageStats | null>(null);

  useEffect(() => {
    let cancelled = false;

    const refresh = async () => {
      try {
        const data = await fetchStorageStats();
        if (!cancelled) setStats(data);
      } catch {
        // Ignore — will retry on next interval
      }
    };

    refresh();
    const id = setInterval(refresh, STATS_POLL_INTERVAL);

    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  return stats;
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

export function formatDuration(seconds: number): string {
  if (seconds < 0) return "Estimating...";
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) {
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    return `${h}h ${m}m`;
  }
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  return `${d}d ${h}h`;
}
