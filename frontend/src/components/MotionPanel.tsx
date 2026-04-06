import { useMotionEvents } from "../hooks/useMotionEvents";
import type { Camera, MotionEvent } from "../types";

export function MotionPanel({
  open,
  onClose,
  cameras,
  activeMotion,
  onJumpToEvent,
}: {
  open: boolean;
  onClose: () => void;
  cameras: Camera[];
  activeMotion: Map<string, string>;
  onJumpToEvent: (cameraId: string, startedAt: string) => void;
}) {
  const { recentEvents } = useMotionEvents(activeMotion);

  const cameraName = (id: string) => {
    const cam = cameras.find((c) => c.id === id);
    if (!cam) return id;
    return (
      cam.name ||
      [cam.manufacturer, cam.model].filter(Boolean).join(" ") ||
      cam.ip
    );
  };

  return (
    <>
      {/* Backdrop */}
      {open && (
        <div
          className="fixed inset-0 bg-black/40 z-40"
          onClick={onClose}
        />
      )}

      {/* Slide-out panel */}
      <aside
        className={`fixed top-0 right-0 h-full w-80 bg-[#1a1a1a] border-l border-[#333] z-50 transform transition-transform duration-200 flex flex-col ${
          open ? "translate-x-0" : "translate-x-full"
        }`}
      >
        <div className="flex items-center justify-between px-4 h-12 border-b border-[#333] shrink-0">
          <div className="flex items-center gap-2">
            <span className="text-[#ddd] font-bold text-sm">Motion</span>
            {activeMotion.size > 0 && (
              <span className="flex items-center gap-1 text-[10px] font-bold uppercase tracking-wide text-red-500">
                <span className="w-1.5 h-1.5 rounded-full bg-red-500 animate-pulse" />
                {activeMotion.size} active
              </span>
            )}
          </div>
          <button
            onClick={onClose}
            className="text-[#888] hover:text-[#ddd] text-lg leading-none"
            aria-label="Close motion panel"
          >
            ×
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-3 flex flex-col gap-2">
          {recentEvents.length === 0 ? (
            <div className="text-[#555] text-xs text-center py-8">
              No motion events yet
            </div>
          ) : (
            recentEvents.map((ev) => (
              <MotionEventCard
                key={ev.id}
                event={ev}
                cameraName={cameraName(ev.camera_id)}
                isActive={activeMotion.get(ev.camera_id) === ev.id}
                onClick={() => onJumpToEvent(ev.camera_id, ev.started_at)}
              />
            ))
          )}
        </div>
      </aside>
    </>
  );
}

function MotionEventCard({
  event,
  cameraName,
  isActive,
  onClick,
}: {
  event: MotionEvent;
  cameraName: string;
  isActive: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={`text-left bg-[#0a0a0a] border rounded overflow-hidden hover:border-[#555] transition-colors ${
        isActive ? "border-red-500" : "border-[#333]"
      }`}
    >
      {event.thumbnail_url ? (
        <img
          src={event.thumbnail_url}
          alt=""
          className="w-full aspect-video object-cover bg-black"
        />
      ) : (
        <div className="w-full aspect-video bg-black flex items-center justify-center text-[#444] text-xs">
          no thumbnail
        </div>
      )}
      <div className="px-2.5 py-1.5 flex justify-between items-center">
        <span className="text-xs text-[#ddd] truncate">{cameraName}</span>
        <span className="text-[10px] text-[#666] tabular-nums shrink-0 ml-2">
          {formatLocalTime(event.started_at)}
        </span>
      </div>
    </button>
  );
}

function formatLocalTime(iso: string): string {
  const d = new Date(iso);
  const now = new Date();
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate();
  const time = `${String(d.getHours()).padStart(2, "0")}:${String(
    d.getMinutes()
  ).padStart(2, "0")}`;
  if (sameDay) return time;
  return `${d.getMonth() + 1}/${d.getDate()} ${time}`;
}
