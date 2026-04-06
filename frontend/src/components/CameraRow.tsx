import type { Camera } from "../types";
import { StatusBadge } from "./StatusBadge";

export function CameraRow({
  camera,
  onAuthClick,
}: {
  camera: Camera;
  onAuthClick: () => void;
}) {
  const displayName =
    camera.name ||
    [camera.manufacturer, camera.model].filter(Boolean).join(" ") ||
    "Unknown Camera";
  const bestRes = camera.resolutions[0] || "—";

  return (
    <div className="flex items-center gap-4 px-4 py-3 bg-[#1a1a1a] border-b border-[#333] hover:bg-[#222] transition-colors">
      {/* Preview */}
      <div className="w-24 h-[54px] bg-[#0d0d0d] rounded flex items-center justify-center shrink-0">
        <svg
          className="w-5 h-5 text-[#555]"
          fill="none"
          stroke="currentColor"
          strokeWidth={2}
          viewBox="0 0 24 24"
        >
          <path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z" />
          <circle cx="12" cy="13" r="4" />
        </svg>
      </div>

      {/* Info */}
      <div className="flex-1 min-w-0">
        <div className="text-sm font-semibold text-[#ddd] truncate">
          {displayName}
        </div>
        <div className="text-xs text-[#888] mt-0.5">
          {camera.manufacturer || "Unknown"}
          {camera.model ? ` ${camera.model}` : ""}
        </div>
      </div>

      {/* IP */}
      <div className="text-[13px] text-[#888] font-mono w-[130px] shrink-0 hidden sm:block">
        {camera.ip}
      </div>

      {/* Resolution */}
      <div className="text-[13px] text-[#888] w-20 shrink-0 hidden md:block">
        {bestRes}
      </div>

      {/* Status */}
      <div className="shrink-0">
        <StatusBadge status={camera.status} onClick={onAuthClick} />
      </div>
    </div>
  );
}
