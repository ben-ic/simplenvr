import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchRecentMotionEvents, fetchTimeline } from "../api/client";
import type { TimelineSegment } from "../api/client";
import { useStorage } from "../hooks/useStorage";
import { apiUrl } from "../lib/backend";
import type { Camera, InboxEvent, MotionEvent } from "../types";
import { CameraTile } from "./CameraTile";
import { SettingsModal } from "./SettingsModal";
import { StorageBanner } from "./StorageBanner";

// ---------------------------------------------------------------------------
// Home — the unified hero screen.
//
// Replaces the old Inbox + Dashboard screens. Layout is a persistent split
// view modeled on Cursor's chat/editor panes:
//
//   ┌─────────────────────────────────────────────────────────┐
//   │  SimpleNVR  • Recording 5 cameras    Cameras  Browse  ⚙│
//   ├─────────────┬───────────────────────────────────────────┤
//   │             │                                           │
//   │  History    │   Main stage:                             │
//   │  (motion    │   - LIVE: grid of camera tiles            │
//   │   events)   │   - CLIP: event playback with ← Live      │
//   │             │                                           │
//   │  ════════   │                                           │
//   │  (resize)   │                                           │
//   └─────────────┴───────────────────────────────────────────┘
//   │ Storage banner                                          │
//   └─────────────────────────────────────────────────────────┘
//
// The history panel is always visible, so there's no "which screen am I
// on" navigation and no one-way doors. Clicking an event row swaps the
// main stage from the live grid to a full-stage clip player with a
// "← Live" affordance. Panel width is persisted to localStorage so the
// user's layout survives reloads.
//
// First-run correctness: because the live grid is always visible on the
// right half, a brand-new user with zero motion events still sees their
// cameras light up on first launch — no empty-inbox blank screen.
// ---------------------------------------------------------------------------

const HISTORY_MIN_WIDTH = 240;
const HISTORY_MAX_WIDTH = 560;
const HISTORY_DEFAULT_WIDTH = 340;
const HISTORY_WIDTH_KEY = "simplenvr.home.historyWidth";
const HISTORY_COLLAPSED_KEY = "simplenvr.home.historyCollapsed";

// Resolve the backend base URL once on mount so we can synchronously
// build thumbnail <img src> strings.
function useBackendBaseUrl(): string | null {
  const [base, setBase] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const b = await apiUrl("");
        if (!cancelled) setBase(b);
      } catch {
        if (!cancelled) setBase("");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);
  return base;
}

// Persistent history-panel width with drag-to-resize.
function useHistoryWidth(): [number, (w: number) => void] {
  const [width, setWidth] = useState<number>(() => {
    try {
      const raw = localStorage.getItem(HISTORY_WIDTH_KEY);
      if (raw) {
        const n = parseInt(raw, 10);
        if (Number.isFinite(n)) {
          return Math.max(HISTORY_MIN_WIDTH, Math.min(HISTORY_MAX_WIDTH, n));
        }
      }
    } catch {
      // ignore
    }
    return HISTORY_DEFAULT_WIDTH;
  });
  const set = useCallback((w: number) => {
    const clamped = Math.max(HISTORY_MIN_WIDTH, Math.min(HISTORY_MAX_WIDTH, w));
    setWidth(clamped);
    try {
      localStorage.setItem(HISTORY_WIDTH_KEY, String(clamped));
    } catch {
      // ignore
    }
  }, []);
  return [width, set];
}

// Render a motion event as a history-panel row. The classifier verdict
// rules the sentence: labeled rows read "Person at Carport", unlabeled
// rows fall back to "Motion at Carport". The user never sees a
// confidence score — the label is either there or it isn't.
function motionEventToHistoryItem(
  motion: MotionEvent,
  cameraName: string,
  clientReadIds: Set<string>,
  clientArchivedIds: Set<string>,
): InboxEvent {
  const startedAt = new Date(motion.started_at);
  const durationS = motion.ended_at
    ? Math.max(
        1,
        Math.round(
          (new Date(motion.ended_at).getTime() - startedAt.getTime()) / 1000,
        ),
      )
    : 1;
  const labelTitle = (() => {
    switch (motion.object_class) {
      case "person":
        return `Person at ${cameraName}`;
      case "vehicle":
        return `Vehicle at ${cameraName}`;
      case "animal":
        return `Animal at ${cameraName}`;
      default:
        return `Motion at ${cameraName}`;
    }
  })();
  return {
    id: motion.id,
    kind: "person_at_zone",
    title: labelTitle,
    subtitle: `${cameraName} · ${durationS} sec`,
    started_at: motion.started_at,
    duration_s: durationS,
    camera_id: motion.camera_id,
    archived: clientArchivedIds.has(motion.id),
    urgent: false,
    unread: !clientReadIds.has(motion.id),
  };
}

function formatClock(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function formatRelativeDay(iso: string): string {
  const d = new Date(iso);
  const now = new Date();
  if (d.toDateString() === now.toDateString()) return formatClock(iso);
  const y = new Date(now);
  y.setDate(y.getDate() - 1);
  if (d.toDateString() === y.toDateString()) {
    return `Yesterday ${formatClock(iso)}`;
  }
  return d.toLocaleDateString([], { weekday: "short" }) + " " + formatClock(iso);
}

function formatDuration(s: number): string {
  if (s < 60) return `0:${String(s).padStart(2, "0")}`;
  const m = Math.floor(s / 60);
  const sec = s % 60;
  if (m < 60) return `${m}:${String(sec).padStart(2, "0")}`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

// ---------------------------------------------------------------------------

export function Home({
  cameras,
  activeMotion,
  initialMotionEvents,
  onBrowseFootage,
  onManageCameras,
  onNameCameras,
}: {
  cameras: Camera[];
  activeMotion: Map<string, string>;
  // Seeded from the WS snapshot via App.tsx → useDiscovery. Null means
  // the snapshot hasn't arrived yet.
  initialMotionEvents: MotionEvent[] | null;
  // Navigate to the full-screen Browse footage (Recordings) view. When
  // called without arguments, opens the most recent camera/date. When
  // called with a camera id and/or started_at, jumps there directly.
  onBrowseFootage: (cameraId?: string, startedAt?: string) => void;
  onManageCameras: () => void;
  onNameCameras: () => void;
}) {
  const backendBase = useBackendBaseUrl();
  const storage = useStorage();
  const [historyWidth, setHistoryWidth] = useHistoryWidth();
  const [historyCollapsed, setHistoryCollapsed] = useState<boolean>(() => {
    try {
      return localStorage.getItem(HISTORY_COLLAPSED_KEY) === "1";
    } catch {
      return false;
    }
  });
  const toggleHistoryCollapsed = useCallback(() => {
    setHistoryCollapsed((prev) => {
      const next = !prev;
      try {
        localStorage.setItem(HISTORY_COLLAPSED_KEY, next ? "1" : "0");
      } catch {
        // ignore
      }
      return next;
    });
  }, []);
  const [showSettings, setShowSettings] = useState(false);

  // Motion events. Seeded from the WS snapshot so the cold-start render
  // doesn't flash an empty list while the REST poll catches up.
  const [motionEvents, setMotionEvents] = useState<MotionEvent[]>(
    initialMotionEvents ?? [],
  );
  const [loading, setLoading] = useState(initialMotionEvents === null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const hydratedFromSnapshotRef = useRef(initialMotionEvents !== null);
  useEffect(() => {
    if (hydratedFromSnapshotRef.current) return;
    if (initialMotionEvents !== null) {
      setMotionEvents(initialMotionEvents);
      setLoading(false);
      hydratedFromSnapshotRef.current = true;
    }
  }, [initialMotionEvents]);

  // Client-side read/archived state. Persisted to localStorage until the
  // backend adds a reviewed-flag column.
  const [readIds, setReadIds] = useState<Set<string>>(() => {
    try {
      const raw = localStorage.getItem("simplenvr.inbox.read");
      return new Set(raw ? (JSON.parse(raw) as string[]) : []);
    } catch {
      return new Set();
    }
  });
  const [archivedIds, setArchivedIds] = useState<Set<string>>(() => {
    try {
      const raw = localStorage.getItem("simplenvr.inbox.archived");
      return new Set(raw ? (JSON.parse(raw) as string[]) : []);
    } catch {
      return new Set();
    }
  });

  // Which event, if any, is playing in the main stage. null = live mode.
  const [selectedEventId, setSelectedEventId] = useState<string | null>(null);

  // Poll for motion events on a 10s interval. Cheap endpoint, small
  // payload; replaceable with a WS push when event_bus grows the channel.
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const events = await fetchRecentMotionEvents(50);
        if (!cancelled) {
          setMotionEvents(events);
          setLoading(false);
          setLoadError(null);
        }
      } catch (err) {
        if (!cancelled) {
          setLoading(false);
          setLoadError(err instanceof Error ? err.message : "Failed to load");
        }
      }
    };
    load();
    const interval = setInterval(load, 10_000);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, []);

  // Display name priority: user-set name → manufacturer + IP → hostname → IP.
  const cameraNameFor = useCallback(
    (camId: string): string => {
      const cam = cameras.find((c) => c.id === camId);
      if (!cam) return "Camera";
      if (cam.name) return cam.name;
      if (cam.manufacturer) return `${cam.manufacturer} (${cam.ip})`;
      if (cam.hostname) return cam.hostname;
      return cam.ip;
    },
    [cameras],
  );

  const events = useMemo<InboxEvent[]>(
    () =>
      motionEvents.map((m) =>
        motionEventToHistoryItem(
          m,
          cameraNameFor(m.camera_id),
          readIds,
          archivedIds,
        ),
      ),
    [motionEvents, readIds, archivedIds, cameraNameFor],
  );

  const visible = useMemo(
    () => events.filter((e) => !e.archived),
    [events],
  );
  const unreadCount = useMemo(
    () => visible.filter((e) => e.unread).length,
    [visible],
  );

  const selectedEvent = useMemo(
    () => (selectedEventId ? events.find((e) => e.id === selectedEventId) : undefined),
    [selectedEventId, events],
  );

  const persistSet = (key: string, set: Set<string>) => {
    try {
      localStorage.setItem(key, JSON.stringify([...set]));
    } catch {
      // ignore
    }
  };

  const archive = (id: string) => {
    setArchivedIds((prev) => {
      const next = new Set(prev);
      next.add(id);
      persistSet("simplenvr.inbox.archived", next);
      return next;
    });
    if (selectedEventId === id) setSelectedEventId(null);
  };

  const selectEvent = (id: string) => {
    setReadIds((prev) => {
      if (prev.has(id)) return prev;
      const next = new Set(prev);
      next.add(id);
      persistSet("simplenvr.inbox.read", next);
      return next;
    });
    setSelectedEventId(id);
  };

  // Resize handle drag. Uses global listeners so the drag survives when
  // the cursor leaves the handle element itself.
  const onResizeMouseDown = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      const startX = e.clientX;
      const startWidth = historyWidth;
      const onMove = (ev: MouseEvent) => {
        setHistoryWidth(startWidth + (ev.clientX - startX));
      };
      const onUp = () => {
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
      };
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
    },
    [historyWidth, setHistoryWidth],
  );

  const online = cameras.filter((c) => c.status === "online" && c.rtsp_uri);

  // Derived topbar status. If any camera has recorded activity, that's the
  // signal — otherwise show camera count. No "All quiet" lie when a camera
  // is offline (the tile itself shows the offline state in the grid).
  const offlineCount = cameras.filter((c) => c.status !== "online").length;

  return (
    <div className="flex-1 flex flex-col bg-[#0a0a0a] text-[#ededed]">
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
          {offlineCount > 0 && (
            <span className="text-xs text-amber-400 font-medium">
              {offlineCount} offline
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={toggleHistoryCollapsed}
            className="p-1.5 text-[#888] hover:text-[#ddd] transition-colors"
            title={historyCollapsed ? "Show history" : "Hide history"}
            aria-label={historyCollapsed ? "Show history" : "Hide history"}
          >
            {historyCollapsed ? (
              <svg
                className="w-4 h-4"
                fill="none"
                stroke="currentColor"
                strokeWidth={2}
                viewBox="0 0 24 24"
              >
                <path d="M3 6h18M3 12h12M3 18h18" strokeLinecap="round" />
              </svg>
            ) : (
              <svg
                className="w-4 h-4"
                fill="none"
                stroke="currentColor"
                strokeWidth={2}
                viewBox="0 0 24 24"
              >
                <path d="M9 6l-6 6 6 6" strokeLinecap="round" strokeLinejoin="round" />
                <path d="M21 6v12" strokeLinecap="round" />
                <path d="M14 6v12" strokeLinecap="round" />
              </svg>
            )}
          </button>
          <button
            onClick={onNameCameras}
            className="px-3 py-1.5 text-[#888] hover:text-[#ddd] text-xs transition-colors"
          >
            Name cameras
          </button>
          <button
            onClick={onManageCameras}
            className="px-3 py-1.5 text-[#888] hover:text-[#ddd] text-xs transition-colors"
          >
            Cameras
          </button>
          <button
            onClick={() => onBrowseFootage()}
            className="px-3 py-1.5 bg-[#222] border border-[#333] text-[#ddd] text-xs font-semibold rounded hover:bg-[#2a2a2a] transition-colors"
          >
            Browse footage
          </button>
          <button
            onClick={() => setShowSettings(true)}
            className="p-1.5 text-[#888] hover:text-[#ddd] transition-colors"
            title="Settings"
            aria-label="Settings"
          >
            <svg
              className="w-4 h-4"
              fill="none"
              stroke="currentColor"
              strokeWidth={2}
              viewBox="0 0 24 24"
            >
              <circle cx="12" cy="12" r="3" />
              <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z" />
            </svg>
          </button>
        </div>
      </div>

      {/* Split view */}
      <div className="flex-1 flex min-h-0 overflow-hidden">
        {/* History panel — hidden entirely when collapsed */}
        {!historyCollapsed && (
        <div
          className="bg-[#0e0e0e] border-r border-[#1a1a1a] flex flex-col shrink-0 min-h-0"
          style={{ width: historyWidth }}
        >
          <div className="px-4 pt-4 pb-3 border-b border-[#1a1a1a] shrink-0">
            <div className="flex items-baseline justify-between">
              <h2 className="text-[14px] font-bold text-[#ededed] m-0">
                History
              </h2>
              <span className="text-[11px] text-[#888]">
                {visible.length === 0
                  ? "No activity yet"
                  : `${unreadCount} new · ${visible.length - unreadCount} seen`}
              </span>
            </div>
          </div>
          <div className="flex-1 overflow-y-auto min-h-0">
            {loading && motionEvents.length === 0 ? (
              <div className="text-center py-12 text-[#555] text-xs">
                Loading…
              </div>
            ) : loadError ? (
              <div className="text-center py-12 text-amber-400 text-xs px-4">
                Couldn't load history. Will retry shortly.
              </div>
            ) : visible.length === 0 ? (
              <div className="text-center py-12 text-[#555] text-xs px-5 leading-relaxed">
                Nothing yet.
                <br />
                <br />
                When a camera sees movement, it'll show up here.
              </div>
            ) : (
              <div className="flex flex-col">
                {visible.map((e) => {
                  const motion = motionEvents.find((m) => m.id === e.id);
                  const thumbUrl =
                    motion?.thumbnail_url && backendBase !== null
                      ? `${backendBase}${motion.thumbnail_url}`
                      : null;
                  return (
                    <HistoryRow
                      key={e.id}
                      event={e}
                      selected={selectedEventId === e.id}
                      thumbnailUrl={thumbUrl}
                      onClick={() => selectEvent(e.id)}
                    />
                  );
                })}
              </div>
            )}
          </div>
        </div>
        )}

        {/* Resize handle — only visible when history panel is expanded */}
        {!historyCollapsed && (
          <div
            onMouseDown={onResizeMouseDown}
            className="w-[6px] bg-transparent hover:bg-[#333] active:bg-[#444] cursor-col-resize shrink-0 transition-colors"
            title="Drag to resize"
          />
        )}

        {/* Main stage */}
        <div className="flex-1 flex flex-col min-w-0 min-h-0 relative">
          {selectedEvent ? (
            <ClipStage
              event={selectedEvent}
              cameraName={cameraNameFor(selectedEvent.camera_id)}
              onBackToLive={() => setSelectedEventId(null)}
              onArchive={() => archive(selectedEvent.id)}
              onOpenInBrowseFootage={() =>
                onBrowseFootage(selectedEvent.camera_id, selectedEvent.started_at)
              }
            />
          ) : (
            <LiveGrid
              cameras={online}
              activeMotion={activeMotion}
              onTileClick={(camId) => onBrowseFootage(camId)}
              onSetupCameras={onManageCameras}
            />
          )}
        </div>
      </div>

      <StorageBanner storage={storage} />
      {showSettings && <SettingsModal onClose={() => setShowSettings(false)} />}
    </div>
  );
}

// ---------------------------------------------------------------------------
// History panel row.
// ---------------------------------------------------------------------------

function HistoryRow({
  event,
  selected,
  thumbnailUrl,
  onClick,
}: {
  event: InboxEvent;
  selected: boolean;
  thumbnailUrl: string | null;
  onClick: () => void;
}) {
  const selectedBg = selected
    ? "bg-[rgba(59,130,246,0.12)] border-l-blue-500"
    : event.unread
      ? "bg-[rgba(245,158,11,0.04)] border-l-transparent hover:bg-white/[0.02]"
      : "border-l-transparent hover:bg-white/[0.02]";
  const readOpacity = !event.unread && !selected ? "opacity-65" : "";
  return (
    <div
      onClick={onClick}
      className={`flex items-center gap-3 px-3 py-2.5 border-l-2 cursor-pointer transition-colors ${selectedBg} ${readOpacity}`}
    >
      <HistoryThumb
        durationLabel={formatDuration(event.duration_s)}
        thumbnailUrl={thumbnailUrl}
      />
      <div className="flex-1 min-w-0">
        <p className="text-[12.5px] font-semibold text-[#ededed] m-0 truncate">
          {event.title}
          {event.unread && (
            <span className="ml-1.5 inline-block text-[9px] font-bold tracking-wide uppercase px-1 py-[1px] rounded-[8px] bg-[rgba(245,158,11,0.18)] text-[#fbbf24] align-[1px]">
              New
            </span>
          )}
        </p>
        <p className="text-[11px] text-[#888] m-0 mt-0.5 truncate">
          {formatRelativeDay(event.started_at)}
        </p>
      </div>
    </div>
  );
}

function HistoryThumb({
  durationLabel,
  thumbnailUrl,
}: {
  durationLabel: string;
  thumbnailUrl: string | null;
}) {
  return (
    <div
      className="relative w-[76px] h-[44px] rounded border border-[#333] shrink-0 overflow-hidden"
      style={
        thumbnailUrl
          ? undefined
          : {
              background:
                "linear-gradient(135deg, rgba(255,255,255,0.02), rgba(0,0,0,0.3)), radial-gradient(circle at 30% 40%, #1f1f1f, #0a0a0a 70%)",
            }
      }
    >
      {thumbnailUrl ? (
        <img
          src={thumbnailUrl}
          alt=""
          className="w-full h-full object-cover"
          loading="lazy"
        />
      ) : (
        <svg
          className="absolute inset-0 m-auto w-4 h-4 opacity-[0.18]"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.4"
        >
          <rect x="2" y="6" width="15" height="12" rx="2" />
          <path d="M17 10 L22 7 L22 17 L17 14 Z" strokeLinejoin="round" />
        </svg>
      )}
      <span className="absolute bottom-0.5 right-0.5 text-[8.5px] text-[#ddd] bg-black/65 px-1 py-[0.5px] rounded-sm tabular-nums">
        {durationLabel}
      </span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Live grid — the default main-stage mode.
// ---------------------------------------------------------------------------

function LiveGrid({
  cameras,
  activeMotion,
  onTileClick,
  onSetupCameras,
}: {
  cameras: Camera[];
  activeMotion: Map<string, string>;
  onTileClick: (cameraId: string) => void;
  onSetupCameras: () => void;
}) {
  if (cameras.length === 0) {
    return (
      <div className="flex-1 flex flex-col items-center justify-center gap-4 text-[#555]">
        <div className="text-sm">No cameras connected yet.</div>
        <button
          onClick={onSetupCameras}
          className="px-4 py-2 bg-[#222] border border-[#333] text-[#ddd] text-xs font-semibold rounded hover:bg-[#2a2a2a] transition-colors"
        >
          Set up cameras
        </button>
      </div>
    );
  }
  // Grid shape: 1 camera = 1 col, 2-4 = 2 cols, 5+ = 3 cols. The old
  // Dashboard was hard-coded to 2 cols which made small deployments
  // huge-tile and big deployments cramped. This scales smoother.
  const cols = cameras.length === 1 ? 1 : cameras.length <= 4 ? 2 : 3;
  return (
    <div
      className="flex-1 grid gap-[1px] bg-black p-[1px]"
      style={{ gridTemplateColumns: `repeat(${cols}, 1fr)` }}
    >
      {cameras.map((cam) => (
        <CameraTile
          key={cam.id}
          camera={cam}
          isMotionActive={activeMotion.has(cam.id)}
          onClick={() => onTileClick(cam.id)}
        />
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Clip stage — full-stage motion-event playback with ← Live, Archive,
// and "Open in Browse footage" affordances.
//
// Segment resolution logic: a motion event doesn't carry a direct
// recording-id foreign key, so we load the camera's day timeline and
// find the segment whose [second_of_day, second_of_day + duration)
// window contains the event. Backend times are UTC; we match with
// getUTCHours() etc. to avoid silent timezone drift.
//
// Gap fallback: recorder restarts/segment rollovers sometimes leave
// events stranded between finalized segments. Fall back to the nearest
// segment within 5 minutes and show a notice explaining the gap.
// ---------------------------------------------------------------------------

function ClipStage({
  event,
  cameraName,
  onBackToLive,
  onArchive,
  onOpenInBrowseFootage,
}: {
  event: InboxEvent;
  cameraName: string;
  onBackToLive: () => void;
  onArchive: () => void;
  onOpenInBrowseFootage: () => void;
}) {
  const startTime = useMemo(() => new Date(event.started_at), [event.started_at]);
  const [videoSrc, setVideoSrc] = useState<string | null>(null);
  const [seekOffset, setSeekOffset] = useState<number>(0);
  const [gapNotice, setGapNotice] = useState<string | null>(null);
  const [resolveError, setResolveError] = useState<string | null>(null);
  const videoRef = useRef<HTMLVideoElement | null>(null);

  useEffect(() => {
    let cancelled = false;
    setVideoSrc(null);
    setResolveError(null);
    setGapNotice(null);
    (async () => {
      try {
        const utcDate = startTime.toISOString().slice(0, 10);
        const eventSecondOfDay =
          startTime.getUTCHours() * 3600 +
          startTime.getUTCMinutes() * 60 +
          startTime.getUTCSeconds();

        const timeline = await fetchTimeline(event.camera_id, utcDate);

        const exact: TimelineSegment | undefined = timeline.segments.find(
          (s) =>
            eventSecondOfDay >= s.second_of_day &&
            eventSecondOfDay < s.second_of_day + s.duration_s,
        );

        let segment: TimelineSegment | undefined = exact;
        let gap: string | null = null;

        if (!segment && timeline.segments.length > 0) {
          const WINDOW = 5 * 60;
          let best: TimelineSegment | undefined;
          let bestDelta = Infinity;
          for (const s of timeline.segments) {
            const segEnd = s.second_of_day + s.duration_s;
            const delta =
              eventSecondOfDay < s.second_of_day
                ? s.second_of_day - eventSecondOfDay
                : eventSecondOfDay - segEnd;
            if (delta < bestDelta) {
              bestDelta = delta;
              best = s;
            }
          }
          if (best && bestDelta <= WINDOW) {
            segment = best;
            const mins = Math.round(bestDelta / 60);
            gap = `There's a gap in the recording here — showing the nearest clip, ${mins} min away.`;
          }
        }

        if (!segment) {
          if (!cancelled) {
            setResolveError("No recording saved for this moment.");
          }
          return;
        }

        const src = await apiUrl(`/api/recordings/${segment.id}/file`);
        const offset = Math.max(
          0,
          eventSecondOfDay - segment.second_of_day - 2,
        );
        if (!cancelled) {
          setVideoSrc(src);
          setSeekOffset(offset);
          setGapNotice(gap);
        }
      } catch (err) {
        if (!cancelled) {
          setResolveError(
            err instanceof Error ? err.message : "Couldn't load this clip.",
          );
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [event.camera_id, event.started_at, startTime]);

  const handleLoadedMetadata = () => {
    const v = videoRef.current;
    if (!v) return;
    v.currentTime = seekOffset;
    v.play().catch(() => {
      // Autoplay policies may block; user can still press play.
    });
  };

  return (
    <div className="flex-1 flex flex-col bg-black min-h-0">
      {/* Clip header */}
      <div className="flex items-center justify-between px-4 h-11 bg-[#141414] border-b border-[#2a2a2a] shrink-0">
        <div className="flex items-center gap-3 min-w-0">
          <button
            onClick={onBackToLive}
            className="text-[#888] hover:text-[#ddd] text-sm font-medium shrink-0"
          >
            ← Live
          </button>
          <span className="text-[#ededed] font-semibold text-[13px] truncate">
            {event.title}
          </span>
          <span className="text-[#666] text-[12px] tabular-nums shrink-0">
            {startTime.toLocaleTimeString([], {
              hour: "numeric",
              minute: "2-digit",
              second: "2-digit",
            })}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={onOpenInBrowseFootage}
            className="px-3 py-1.5 text-[#888] hover:text-[#ddd] text-xs transition-colors"
          >
            Open in Browse footage
          </button>
          <button
            onClick={onArchive}
            className="px-3 py-1.5 bg-[#222] border border-[#333] text-[#ededed] text-xs font-semibold rounded hover:bg-[#2a2a2a] transition-colors"
          >
            Dismiss
          </button>
        </div>
      </div>

      {gapNotice && (
        <div className="px-4 py-2 bg-[rgba(245,158,11,0.08)] border-b border-[rgba(245,158,11,0.25)] text-[12px] text-[#fbbf24] shrink-0">
          {gapNotice}
        </div>
      )}

      <div className="flex-1 flex items-center justify-center bg-black min-h-0">
        {videoSrc ? (
          <video
            ref={videoRef}
            src={videoSrc}
            controls
            muted
            autoPlay
            onLoadedMetadata={handleLoadedMetadata}
            className="w-full h-full object-contain"
          />
        ) : resolveError ? (
          <div className="text-[#888] text-sm text-center px-6 max-w-md">
            {resolveError}
          </div>
        ) : (
          <div className="text-[#555] text-xs">Loading clip…</div>
        )}
      </div>

      <div className="px-4 py-2 bg-[#0e0e0e] border-t border-[#1a1a1a] text-[11px] text-[#666] shrink-0">
        {cameraName} ·{" "}
        {startTime.toLocaleDateString([], {
          weekday: "short",
          month: "short",
          day: "numeric",
        })}
      </div>
    </div>
  );
}
