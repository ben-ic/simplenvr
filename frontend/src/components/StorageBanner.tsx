import { formatBytes, formatDuration } from "../hooks/useStorage";
import type { StorageStatus } from "../types";

export function StorageBanner({
  storage,
}: {
  storage: StorageStatus | null;
}) {
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

  // Color the headline based on time remaining
  const seconds = storage.seconds_remaining;
  const isLow = seconds > 0 && seconds < 12 * 3600; // < 12 hours
  const isWarn = seconds > 0 && seconds < 2 * 86400 && !isLow; // < 2 days
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

  return (
    <div className="bg-[#1a1a1a] border-t border-[#333] px-5 py-4 shrink-0">
      <div className="flex items-center justify-between gap-6">
        {/* Headline — the most important number in the app */}
        <div className="flex flex-col gap-0.5 min-w-0">
          <div className="text-[10px] uppercase tracking-wide text-[#555] font-semibold">
            Storage
          </div>
          <div className={`text-2xl font-bold tabular-nums leading-tight ${headlineColor}`}>
            {formatDuration(storage.seconds_remaining)}
          </div>
          <div className="text-[11px] text-[#888]">
            of recording remaining
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
              {formatBytes(storage.used_bytes)} of {formatBytes(storage.limit_bytes)} used
            </span>
            <span>
              {storage.cameras_recording} recording ·{" "}
              {(storage.total_bitrate_bps / 1000 / 1000).toFixed(1)} Mbps
            </span>
          </div>
        </div>

      </div>
    </div>
  );
}
