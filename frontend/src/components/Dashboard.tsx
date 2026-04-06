import { useEffect, useState } from "react";
import { useStorage } from "../hooks/useStorage";
import type { Camera } from "../types";
import { SettingsModal } from "./SettingsModal";
import { StorageBanner } from "./StorageBanner";

export function Dashboard({
  cameras,
  onPlayback,
  onManageCameras,
}: {
  cameras: Camera[];
  onPlayback: (cameraId?: string) => void;
  onManageCameras: () => void;
}) {
  const online = cameras.filter((c) => c.status === "online" && c.rtsp_uri);
  const storage = useStorage();
  const [showSettings, setShowSettings] = useState(false);

  return (
    <div className="flex-1 flex flex-col">
      {/* Topbar */}
      <div className="flex items-center justify-between px-5 h-12 bg-[#1a1a1a] border-b border-[#333] shrink-0">
        <div className="flex items-center gap-3">
          <span className="text-[#ddd] font-bold text-[15px]">SimpleNVR</span>
          {storage && (
            <span className="flex items-center gap-1.5 text-xs text-red-500 font-medium">
              <span className="w-1.5 h-1.5 rounded-full bg-red-500 animate-pulse" />
              Recording {storage.cameras_recording} camera
              {storage.cameras_recording !== 1 ? "s" : ""}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={onManageCameras}
            className="px-3 py-1.5 text-[#888] hover:text-[#ddd] text-xs transition-colors"
          >
            Cameras
          </button>
          <button
            onClick={() => onPlayback()}
            className="px-3 py-1.5 bg-[#222] border border-[#333] text-[#ddd] text-xs font-semibold rounded hover:bg-[#2a2a2a] transition-colors flex items-center gap-1.5"
          >
            <svg className="w-3 h-3" fill="currentColor" viewBox="0 0 24 24">
              <path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10 10-4.5 10-10S17.5 2 12 2zm0 18c-4.4 0-8-3.6-8-8s3.6-8 8-8 8 3.6 8 8-3.6 8-8 8zm.5-13H11v6l5.2 3.2.8-1.3-4.5-2.7V7z" />
            </svg>
            Recordings
          </button>
        </div>
      </div>

      {/* Camera grid */}
      {online.length === 0 ? (
        <div className="flex-1 flex items-center justify-center text-[#555] text-sm">
          No connected cameras to display
        </div>
      ) : (
        <div
          className="flex-1 grid gap-[1px] bg-black p-[1px]"
          style={{
            gridTemplateColumns: `repeat(${
              online.length === 1 ? 1 : 2
            }, 1fr)`,
          }}
        >
          {online.map((cam) => (
            <CameraTile
              key={cam.id}
              camera={cam}
              onClick={() => onPlayback(cam.id)}
            />
          ))}
        </div>
      )}

      {/* Storage banner */}
      <StorageBanner storage={storage} onSettings={() => setShowSettings(true)} />

      {/* Settings modal */}
      {showSettings && <SettingsModal onClose={() => setShowSettings(false)} />}
    </div>
  );
}

function CameraTile({
  camera,
  onClick,
}: {
  camera: Camera;
  onClick: () => void;
}) {
  const [clock, setClock] = useState(formatNow);

  // Live clock
  useEffect(() => {
    const id = setInterval(() => setClock(formatNow()), 1000);
    return () => clearInterval(id);
  }, []);

  const displayName =
    camera.name ||
    [camera.manufacturer, camera.model].filter(Boolean).join(" ") ||
    camera.ip;

  return (
    <div
      onClick={onClick}
      className="bg-[#0a0a0a] relative aspect-video overflow-hidden cursor-pointer group"
    >
      <img
        src={`/api/cameras/${camera.id}/stream.mjpeg`}
        alt={displayName}
        className="w-full h-full object-cover"
      />

      {/* Hover hint */}
      <div className="absolute inset-0 bg-black/0 group-hover:bg-black/30 transition-colors flex items-center justify-center opacity-0 group-hover:opacity-100">
        <div className="bg-black/70 text-white text-xs font-semibold px-3 py-1.5 rounded">
          View recordings →
        </div>
      </div>

      {/* Top overlay */}
      <div className="absolute top-0 left-0 right-0 px-3 py-2 flex justify-between items-start bg-gradient-to-b from-black/70 to-transparent pointer-events-none">
        <span className="text-xs font-semibold text-white drop-shadow">
          {displayName}
        </span>
        <span className="flex items-center gap-1 text-[10px] font-bold uppercase tracking-wide text-red-500">
          <span className="w-1 h-1 rounded-full bg-red-500 animate-pulse" />
          REC
        </span>
      </div>

      {/* Bottom overlay */}
      <div className="absolute bottom-0 left-0 right-0 px-3 py-2 flex justify-between items-end bg-gradient-to-t from-black/70 to-transparent pointer-events-none">
        <span className="text-[11px] text-white/70 font-mono tabular-nums">
          {clock}
        </span>
      </div>
    </div>
  );
}

function formatNow(): string {
  const d = new Date();
  return `${String(d.getHours()).padStart(2, "0")}:${String(
    d.getMinutes()
  ).padStart(2, "0")}:${String(d.getSeconds()).padStart(2, "0")}`;
}
