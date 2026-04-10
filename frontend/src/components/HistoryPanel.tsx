import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchRecentEpisodes, fetchRecentMotionEvents } from "../api/client";
import type { Episode } from "../api/client";
import { apiUrl } from "../lib/backend";
import type { Camera, InboxEvent, MotionEvent } from "../types";

// ---------------------------------------------------------------------------
// HistoryPanel — the persistent "what happened" left rail, reused on
// both Home and Recordings (Browse footage).
//
// Internal state:
//   - motion events (polled every 10s from the backend)
//   - client-side read / archived set (localStorage)
//   - panel width (localStorage, drag-to-resize)
//
// Controlled by the parent:
//   - collapsed (shown/hidden via the topbar hamburger)
//   - selectedEventId (purely visual — the parent decides what "selected"
//     means in its context: on Home it's the clip playing in the main
//     stage, on Recordings it's the most recent row the user clicked
//     to seek the scrubber)
//
// The parent owns those two bits because the toggle lives in the topbar
// (outside this component) and because different parents have different
// notions of what "selected" should render. Polling + read/archived +
// width all live here because nobody else needs them.
// ---------------------------------------------------------------------------

const HISTORY_MIN_WIDTH = 240;
const HISTORY_MAX_WIDTH = 560;
const HISTORY_DEFAULT_WIDTH = 340;
const HISTORY_WIDTH_KEY = "simplenvr.home.historyWidth";
const HISTORY_COLLAPSED_KEY = "simplenvr.home.historyCollapsed";
const READ_KEY = "simplenvr.inbox.read";
const ARCHIVED_KEY = "simplenvr.inbox.archived";

// Shared collapse state between Home and Recordings. Both screens own
// local React state but read/write the same localStorage key, so toggling
// in one surface is visible on the other after navigation.
export function useHistoryCollapsed(): [boolean, () => void] {
  const [collapsed, setCollapsed] = useState<boolean>(() => {
    try {
      return localStorage.getItem(HISTORY_COLLAPSED_KEY) === "1";
    } catch {
      return false;
    }
  });
  const toggle = useCallback(() => {
    setCollapsed((prev) => {
      const next = !prev;
      try {
        localStorage.setItem(HISTORY_COLLAPSED_KEY, next ? "1" : "0");
      } catch {
        // ignore
      }
      return next;
    });
  }, []);
  return [collapsed, toggle];
}

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

function episodeToHistoryItem(
  ep: Episode,
  cameraName: string,
  clientReadIds: Set<string>,
  clientArchivedIds: Set<string>,
): InboxEvent {
  const label = (() => {
    switch (ep.object_class) {
      case "person":
        return "Person";
      case "vehicle":
        return "Vehicle";
      case "animal":
        return "Animal";
      default:
        return "Activity";
    }
  })();
  const countSuffix = ep.event_count > 1 ? ` (${ep.event_count} events)` : "";
  return {
    id: ep.id,
    kind: "person_at_zone",
    title: `${label} at ${cameraName}${countSuffix}`,
    subtitle: `${cameraName} · ${formatDuration(ep.duration_s)}`,
    started_at: ep.started_at,
    duration_s: ep.duration_s,
    camera_id: ep.camera_id,
    archived: ep.event_ids.some((id) => clientArchivedIds.has(id)),
    urgent: false,
    unread: !ep.event_ids.some((id) => clientReadIds.has(id)),
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

export function HistoryPanel({
  cameras,
  initialMotionEvents,
  selectedEventId,
  onSelectEvent,
  collapsed,
}: {
  cameras: Camera[];
  // Seeded from the WS snapshot so the cold-start render doesn't flash
  // "No activity yet" while the REST poll catches up.
  initialMotionEvents: MotionEvent[] | null;
  selectedEventId: string | null;
  onSelectEvent: (event: InboxEvent) => void;
  collapsed: boolean;
}) {
  const backendBase = useBackendBaseUrl();

  // Width + resize handling.
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
  const setWidthPersisted = useCallback((w: number) => {
    const clamped = Math.max(HISTORY_MIN_WIDTH, Math.min(HISTORY_MAX_WIDTH, w));
    setWidth(clamped);
    try {
      localStorage.setItem(HISTORY_WIDTH_KEY, String(clamped));
    } catch {
      // ignore
    }
  }, []);
  const onResizeMouseDown = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      const startX = e.clientX;
      const startWidth = width;
      const onMove = (ev: MouseEvent) => {
        setWidthPersisted(startWidth + (ev.clientX - startX));
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
    [width, setWidthPersisted],
  );

  // Episode polling (grouped, noise-gated motion events).
  const [episodes, setEpisodes] = useState<Episode[]>([]);
  // Legacy flat events kept for the WS snapshot seed and clip playback.
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

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const eps = await fetchRecentEpisodes(50);
        if (!cancelled) {
          setEpisodes(eps);
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

  // Read / archived client state.
  const [readIds, setReadIds] = useState<Set<string>>(() => {
    try {
      const raw = localStorage.getItem(READ_KEY);
      return new Set(raw ? (JSON.parse(raw) as string[]) : []);
    } catch {
      return new Set();
    }
  });
  const [archivedIds] = useState<Set<string>>(() => {
    try {
      const raw = localStorage.getItem(ARCHIVED_KEY);
      return new Set(raw ? (JSON.parse(raw) as string[]) : []);
    } catch {
      return new Set();
    }
  });

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
      episodes.map((ep) =>
        episodeToHistoryItem(
          ep,
          cameraNameFor(ep.camera_id),
          readIds,
          archivedIds,
        ),
      ),
    [episodes, readIds, archivedIds, cameraNameFor],
  );

  const visible = useMemo(
    () => events.filter((e) => !e.archived),
    [events],
  );
  const unreadCount = useMemo(
    () => visible.filter((e) => e.unread).length,
    [visible],
  );

  const handleSelect = useCallback(
    (event: InboxEvent) => {
      setReadIds((prev) => {
        if (prev.has(event.id)) return prev;
        const next = new Set(prev);
        next.add(event.id);
        try {
          localStorage.setItem(READ_KEY, JSON.stringify([...next]));
        } catch {
          // ignore
        }
        return next;
      });
      onSelectEvent(event);
    },
    [onSelectEvent],
  );

  if (collapsed) return null;

  return (
    <>
      <div
        className="bg-[#0e0e0e] border-r border-[#1a1a1a] flex flex-col shrink-0 min-h-0"
        style={{ width }}
      >
        <div className="px-4 pt-4 pb-3 border-b border-[#1a1a1a] shrink-0">
          <div className="flex items-baseline justify-between">
            <h2 className="text-[14px] font-bold text-[#ededed] m-0">History</h2>
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
                const ep = episodes.find((ep) => ep.id === e.id);
                const thumbUrl =
                  ep?.thumbnail_url && backendBase !== null
                    ? `${backendBase}${ep.thumbnail_url}`
                    : null;
                return (
                  <HistoryRow
                    key={e.id}
                    event={e}
                    selected={selectedEventId === e.id}
                    thumbnailUrl={thumbUrl}
                    onClick={() => handleSelect(e)}
                  />
                );
              })}
            </div>
          )}
        </div>
      </div>

      {/* Resize handle */}
      <div
        onMouseDown={onResizeMouseDown}
        className="w-[6px] bg-transparent hover:bg-[#333] active:bg-[#444] cursor-col-resize shrink-0 transition-colors"
        title="Drag to resize"
      />
    </>
  );
}

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
