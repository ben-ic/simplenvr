import { formatBytes, useStorageStats } from "../hooks/useStorage";
import type { StorageStatus } from "../types";

function formatRetentionDays(days: number): string {
  if (days < 1) {
    const hours = days * 24;
    if (hours < 1) {
      return `${Math.round(hours * 60)}m`;
    }
    return `${hours.toFixed(1)}h`;
  }
  if (days < 10) return `${days.toFixed(1)} days`;
  return `${Math.round(days)} days`;
}

export function StorageBanner({
  storage,
}: {
  storage: StorageStatus | null;
}) {
  const stats = useStorageStats();

  if (!storage) {
    return (
      <div className="bg-[#1a1a1a] border-t border-[#333] px-5 py-3 text-xs text-[#555]">
        Loading storage...
      </div>
    );
  }

  const usedPct =
    storage.limit_bytes > 0
      ? Math.min(100, (storage.used_bytes / storage.limit_bytes) * 100)
      : 0;

  // Headline reflects circular-buffer retention: budget / bitrate.
  // Free disk is shown only as context.
  const retentionDays = stats?.ready ? stats.retention_days : null;
  const isLow = retentionDays !== null && retentionDays < 0.5; // < 12h
  const isWarn =
    retentionDays !== null && retentionDays < 2 && !isLow; // < 2 days
  const headlineColor = isLow
    ? "text-red-400"
    : isWarn
    ? "text-amber-400"
    : "text-[#ddd]";
  const barColor = isLow
    ? "bg-red-500"
    : isWarn
    ? "bg-amber-500"
    : "bg-blue-500";

  const headline =
    retentionDays !== null
      ? formatRetentionDays(retentionDays)
      : "measuring…";

  return (
    <div className="bg-[#1a1a1a] border-t border-[#333] px-5 py-4 shrink-0">
      <div className="flex items-center justify-between gap-6">
        {/* Headline — the most important number in the app */}
        <div className="flex flex-col gap-0.5 min-w-0">
          <div className="text-[10px] uppercase tracking-wide text-[#555] font-semibold">
            Retention
          </div>
          <div className={`text-2xl font-bold tabular-nums leading-tight ${headlineColor}`}>
            {headline}
          </div>
          <div className="text-[11px] text-[#888]">
            {retentionDays !== null
              ? "of recording retained at current bitrate"
              : "collecting bitrate data…"}
          </div>
        </div>

        {/* Bar takes the rest of the width */}
        <div className="flex-1">
          <div className="h-2 bg-[#2a2a2a] rounded overflow-hidden">
            <div
              className={`h-full transition-all ${barColor}`}
              style={{ width: `${usedPct}%` }}
            />
          </div>
          <div className="flex justify-between text-[11px] text-[#888] mt-1.5 tabular-nums">
            <span>
              {formatBytes(storage.used_bytes)} of {formatBytes(storage.limit_bytes)} budget
              {stats?.bitrate_gb_per_day != null && (
                <>
                  {" · "}
                  {stats.bitrate_gb_per_day.toFixed(1)} GB/day
                </>
              )}
            </span>
            <span>
              {storage.cameras_recording} recording ·{" "}
              {(storage.total_bitrate_bps / 1000 / 1000).toFixed(1)} Mbps
              {stats != null && (
                <>
                  {" · "}
                  free disk {stats.free_disk_gb.toFixed(0)} GB
                </>
              )}
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}
