import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchRecentMotionEvents, fetchToday, searchMotionEvents } from "../api/client";
import type { MotionEventFilters, TodayCameraSummary, TodayData } from "../api/client";
import { apiUrl, thumbnailUrl } from "../lib/backend";
import { cameraDisplayName, formatDuration } from "../lib/format";
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

// Sentinel archive set. The archive feature isn't built yet — earlier
// versions of this file destructured useState without a setter, which
// meant localStorage reads could prime the set once but no UI action
// could ever add to it. That left the InboxEvent.archived bit perpetually
// stale. Until we have a real archive gesture, always pass the same
// empty Set to `motionEventToInboxEvent` so the UI is honest about state.
const EMPTY_ARCHIVED: Set<string> = new Set();

/** Deduplicate motion events by id, keeping the last occurrence. */
function dedupeEvents(events: MotionEvent[]): MotionEvent[] {
  const map = new Map<string, MotionEvent>();
  for (const ev of events) map.set(ev.id, ev);
  return Array.from(map.values());
}


type HistoryTab = "today" | "all";

// --- Activity filters (All Activity tab) ---

type ObjectClassFilter = "person" | "vehicle" | "animal" | null;
type DateRangeFilter = "all" | "today" | "yesterday" | "week";

interface ActivityFilters {
  objectClass: ObjectClassFilter;
  cameraId: string | null;
  dateRange: DateRangeFilter;
}

const DEFAULT_FILTERS: ActivityFilters = {
  objectClass: null,
  cameraId: null,
  dateRange: "all",
};

const TYPE_CHIPS: { value: ObjectClassFilter; label: string }[] = [
  { value: null, label: "All" },
  { value: "person", label: "Person" },
  { value: "vehicle", label: "Vehicle" },
  { value: "animal", label: "Animal" },
];

const DATE_OPTIONS: { value: DateRangeFilter; label: string }[] = [
  { value: "all", label: "All time" },
  { value: "today", label: "Today" },
  { value: "yesterday", label: "Yesterday" },
  { value: "week", label: "Last 7 days" },
];

function filtersToApiParams(filters: ActivityFilters): MotionEventFilters {
  const params: MotionEventFilters = { limit: 200 };
  if (filters.objectClass) {
    params.object_class = filters.objectClass;
  }
  if (filters.cameraId) {
    params.camera_id = filters.cameraId;
  }
  if (filters.dateRange !== "all") {
    const now = new Date();
    let start: Date;
    let end: Date | null = null;
    switch (filters.dateRange) {
      case "today":
        start = new Date(now.getFullYear(), now.getMonth(), now.getDate());
        break;
      case "yesterday":
        start = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
        end = new Date(now.getFullYear(), now.getMonth(), now.getDate());
        break;
      case "week":
        start = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 7);
        break;
      default:
        start = new Date(0);
    }
    params.started_after = start.toISOString();
    if (end) params.ended_before = end.toISOString();
  }
  return params;
}

function isFiltered(filters: ActivityFilters): boolean {
  return (
    filters.objectClass !== null ||
    filters.cameraId !== null ||
    filters.dateRange !== "all"
  );
}

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

const LABEL_NAMES: Record<string, string> = {
  person: "Person",
  vehicle: "Vehicle",
  animal: "Animal",
};

function motionEventToInboxEvent(
  ev: MotionEvent,
  cameraName: string,
  clientReadIds: Set<string>,
  clientArchivedIds: Set<string>,
): InboxEvent {
  const label = ev.object_class
    ? (LABEL_NAMES[ev.object_class] ?? ev.object_class)
    : "Motion";
  // Brief VLM summary if available, else YOLOX label at camera.
  const title = ev.summary ?? `${label} at ${cameraName}`;
  const duration_s = ev.ended_at
    ? Math.max(1, Math.round((new Date(ev.ended_at).getTime() - new Date(ev.started_at).getTime()) / 1000))
    : 1;
  return {
    id: ev.id,
    kind: "person_at_zone",
    title,
    subtitle: cameraName,
    started_at: ev.started_at,
    duration_s,
    camera_id: ev.camera_id,
    archived: clientArchivedIds.has(ev.id),
    urgent: false,
    unread: !clientReadIds.has(ev.id),
    summary: ev.summary,
    description: ev.description,
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

  // View mode: "today" (default) or "all" (full flat list).
  const [activeTab, setActiveTab] = useState<HistoryTab>("today");

  // Activity filters (All Activity tab only). Ephemeral — reset on nav.
  const [filters, setFilters] = useState<ActivityFilters>(DEFAULT_FILTERS);
  const updateFilter = useCallback(
    <K extends keyof ActivityFilters>(key: K, value: ActivityFilters[K]) => {
      setFilters((prev) => ({ ...prev, [key]: value }));
    },
    [],
  );

  // Today data (notable events + per-camera counts).
  const [todayData, setTodayData] = useState<TodayData | null>(null);
  const [todayLoading, setTodayLoading] = useState(true);
  useEffect(() => {
    if (activeTab !== "today") return;
    let cancelled = false;
    const load = async () => {
      const data = await fetchToday();
      if (!cancelled) {
        setTodayData(data);
        setTodayLoading(false);
      }
    };
    load();
    const interval = setInterval(load, 5_000); // Poll every 5 seconds for faster updates
    return () => { cancelled = true; clearInterval(interval); };
  }, [activeTab]);

  // Search state.
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<MotionEvent[] | null>(null);
  const [searching, setSearching] = useState(false);
  const searchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const handleSearchChange = useCallback((q: string) => {
    setSearchQuery(q);
    if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
    if (!q.trim()) {
      setSearchResults(null);
      setSearching(false);
      return;
    }
    setSearching(true);
    // Debounce 300ms so we don't fire on every keystroke.
    searchTimerRef.current = setTimeout(async () => {
      const results = await searchMotionEvents(q.trim());
      setSearchResults(results);
      setSearching(false);
    }, 300);
  }, []);

  // Cancel any pending debounced search on unmount so setSearchResults
  // is never called after the component is gone. Prevents React's
  // "setState on unmounted component" warning (and a hard error in
  // future strict modes) when the user collapses the history panel
  // within 300ms of a keystroke.
  useEffect(() => {
    return () => {
      if (searchTimerRef.current) {
        clearTimeout(searchTimerRef.current);
        searchTimerRef.current = null;
      }
    };
  }, []);

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

  // Flat motion event polling (noise-gated — labeled events only).
  const [motionEvents, setMotionEvents] = useState<MotionEvent[]>(
    dedupeEvents(initialMotionEvents ?? []),
  );
  const [loading, setLoading] = useState(initialMotionEvents === null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const hydratedFromSnapshotRef = useRef(initialMotionEvents !== null);
  useEffect(() => {
    if (hydratedFromSnapshotRef.current) return;
    if (initialMotionEvents !== null) {
      setMotionEvents(dedupeEvents(initialMotionEvents));
      setLoading(false);
      hydratedFromSnapshotRef.current = true;
    }
  }, [initialMotionEvents]);

  // Build API params from current filter state. When no filters are
  // active this produces the same {limit: 50} call as before; when
  // any filter is set the backend applies WHERE clauses in SQL.
  const apiParams = useMemo<MotionEventFilters>(() => {
    if (!isFiltered(filters)) return { limit: 50 };
    return filtersToApiParams(filters);
  }, [filters]);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const evts = await fetchRecentMotionEvents(apiParams);
        if (!cancelled) {
          setMotionEvents(dedupeEvents(evts));
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
  }, [apiParams]);

  // Read-state persistence. The companion archive feature is not yet
  // wired to a user gesture — see EMPTY_ARCHIVED at the top of this
  // file for the rationale. When archive ships, `archivedIds` will
  // become a real useState with a setter that mirrors readIds below.
  const [readIds, setReadIds] = useState<Set<string>>(() => {
    try {
      const raw = localStorage.getItem(READ_KEY);
      return new Set(raw ? (JSON.parse(raw) as string[]) : []);
    } catch {
      return new Set();
    }
  });

  const cameraNameFor = useCallback(
    (camId: string): string =>
      cameraDisplayName(cameras.find((c) => c.id === camId)),
    [cameras],
  );

  const events = useMemo<InboxEvent[]>(
    () =>
      motionEvents.map((ev) =>
        motionEventToInboxEvent(
          ev,
          cameraNameFor(ev.camera_id),
          readIds,
          EMPTY_ARCHIVED,
        ),
      ),
    [motionEvents, readIds, cameraNameFor],
  );

  const visible = useMemo(
    () => events.filter((e) => !e.archived),
    [events],
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
        <div className="px-4 pt-3 pb-2 border-b border-[#1a1a1a] shrink-0">
          {activeTab === "today" ? (
            <h2 className="text-[14px] font-bold text-[#ededed] m-0 mb-0.5">Today</h2>
          ) : (
            <div className="flex items-center gap-2 mb-1.5">
              <button
                onClick={() => { setActiveTab("today"); setSearchQuery(""); setSearchResults(null); setFilters(DEFAULT_FILTERS); }}
                className="text-[11px] text-[#666] hover:text-[#999] transition-colors"
              >
                &larr; Today
              </button>
              <h2 className="text-[14px] font-bold text-[#ededed] m-0">All Activity</h2>
            </div>
          )}
          {activeTab === "all" && (
            <>
              <input
                type="text"
                value={searchQuery}
                onChange={(e) => handleSearchChange(e.target.value)}
                placeholder="Search events..."
                className="w-full px-2.5 py-1.5 mb-1.5 bg-[#1a1a1a] border border-[#333] rounded text-[12px] text-[#ddd] placeholder-[#555] outline-none focus:border-[#555] transition-colors"
              />
              {!searchQuery && (
                <FilterBar
                  filters={filters}
                  onUpdate={updateFilter}
                  cameras={cameras}
                  motionEvents={motionEvents}
                />
              )}
              <span className="text-[11px] text-[#888] mt-1 block">
                {searchQuery
                  ? searching
                    ? "Searching..."
                    : searchResults
                      ? `${searchResults.length} result${searchResults.length !== 1 ? "s" : ""}`
                      : ""
                  : isFiltered(filters)
                    ? `${visible.length} matching events`
                    : `${visible.length} events`}
              </span>
            </>
          )}
        </div>
        <div className="flex-1 overflow-y-auto min-h-0">
          {activeTab === "today" ? (
            <TodayView
              data={todayData}
              loading={todayLoading}
              backendBase={backendBase}
              cameraNameFor={cameraNameFor}
              selectedEventId={selectedEventId}
              onSelectEvent={handleSelect}
              onSeeAll={() => setActiveTab("all")}
              readIds={readIds}
            />
          ) : searchResults !== null ? (
            searchResults.length === 0 ? (
              <div className="text-center py-12 text-[#555] text-xs px-5">
                No events match &ldquo;{searchQuery}&rdquo;
              </div>
            ) : (
              <div className="flex flex-col">
                {searchResults.map((ev) => {
                  const mapped = motionEventToInboxEvent(
                    ev, cameraNameFor(ev.camera_id), readIds, EMPTY_ARCHIVED,
                  );
                  const thumbUrl = thumbnailUrl(ev.thumbnail_url, backendBase);
                  return (
                    <HistoryRow
                      key={ev.id}
                      event={mapped}
                      selected={selectedEventId === ev.id}
                      thumbnailUrl={thumbUrl}
                      onClick={() => handleSelect(mapped)}
                    />
                  );
                })}
              </div>
            )
          ) : loading && motionEvents.length === 0 ? (
            <div className="text-center py-12 text-[#555] text-xs">
              Loading…
            </div>
          ) : loadError ? (
            <div className="text-center py-12 text-amber-400 text-xs px-4">
              Couldn't load history. Will retry shortly.
            </div>
          ) : visible.length === 0 ? (
            <div className="text-center py-12 text-[#555] text-xs px-5 leading-relaxed">
              No activity recorded today.
            </div>
          ) : (
            <div className="flex flex-col">
              {visible.map((e) => {
                const ev = motionEvents.find((m) => m.id === e.id);
                const thumbUrl = thumbnailUrl(ev?.thumbnail_url, backendBase);
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

// ---------------------------------------------------------------------------
// FilterBar — compact type chips + camera + date selects for All Activity.
// ---------------------------------------------------------------------------

function FilterBar({
  filters,
  onUpdate,
  cameras,
  motionEvents,
}: {
  filters: ActivityFilters;
  onUpdate: <K extends keyof ActivityFilters>(key: K, value: ActivityFilters[K]) => void;
  cameras: Camera[];
  motionEvents: MotionEvent[];
}) {
  // Only show cameras that have events in the current result set.
  const camerasWithEvents = useMemo(() => {
    const ids = new Set(motionEvents.map((e) => e.camera_id));
    return cameras.filter((c) => ids.has(c.id));
  }, [cameras, motionEvents]);

  return (
    <div className="flex flex-col gap-1.5 mt-1.5">
      {/* Type chips */}
      <div className="flex flex-wrap gap-1">
        {TYPE_CHIPS.map(({ value, label }) => {
          const active = filters.objectClass === value;
          return (
            <button
              key={label}
              onClick={() => onUpdate("objectClass", active ? null : value)}
              className={`px-2 py-0.5 text-[10px] font-medium rounded-full border transition-colors ${
                active
                  ? "bg-[#1f2937] text-[#93c5fd] border-blue-500/40"
                  : "bg-[#1a1a1a] text-[#666] border-[#2a2a2a] hover:text-[#999] hover:border-[#444]"
              }`}
            >
              {label}
            </button>
          );
        })}
      </div>
      {/* Camera + date selects */}
      <div className="flex flex-wrap gap-1.5">
        <select
          value={filters.cameraId ?? ""}
          onChange={(e) => onUpdate("cameraId", e.target.value || null)}
          className="flex-1 min-w-0 px-1.5 py-1 bg-[#1a1a1a] border border-[#2a2a2a] rounded text-[10px] text-[#888] outline-none focus:border-[#555] transition-colors"
        >
          <option value="">All cameras</option>
          {camerasWithEvents.map((cam) => (
            <option key={cam.id} value={cam.id}>
              {cameraDisplayName(cam)}
            </option>
          ))}
        </select>
        <select
          value={filters.dateRange}
          onChange={(e) => onUpdate("dateRange", e.target.value as DateRangeFilter)}
          className="px-1.5 py-1 bg-[#1a1a1a] border border-[#2a2a2a] rounded text-[10px] text-[#888] outline-none focus:border-[#555] transition-colors"
        >
          {DATE_OPTIONS.map(({ value, label }) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
      </div>
      {/* Clear filters link */}
      {isFiltered(filters) && (
        <button
          onClick={() => {
            onUpdate("objectClass", null);
            onUpdate("cameraId", null);
            onUpdate("dateRange", "all");
          }}
          className="self-start text-[10px] text-[#555] hover:text-[#888] transition-colors"
        >
          Clear filters
        </button>
      )}
    </div>
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

// ---------------------------------------------------------------------------
// Tab button for Inbox / Story switcher.
// ---------------------------------------------------------------------------
// Today view — notable person cards + per-camera routine counts.
// ---------------------------------------------------------------------------

function TodayView({
  data,
  loading,
  backendBase,
  cameraNameFor,
  selectedEventId,
  onSelectEvent,
  onSeeAll,
  readIds,
}: {
  data: TodayData | null;
  loading: boolean;
  backendBase: string | null;
  cameraNameFor: (camId: string) => string;
  selectedEventId: string | null;
  onSelectEvent: (event: InboxEvent) => void;
  onSeeAll: () => void;
  readIds: Set<string>;
}) {
  if (loading && !data) {
    return (
      <div className="text-center py-12 text-[#555] text-xs">Loading…</div>
    );
  }
  if (!data) {
    return (
      <div className="text-center py-12 text-[#555] text-xs px-5">
        Couldn&rsquo;t load today&rsquo;s activity.
      </div>
    );
  }

  const hasNotable = data.notable.length > 0;
  const activeCameras = data.cameras.filter((c) => c.total > 0);
  const quietCameras = data.cameras.filter((c) => c.total === 0);

  return (
    <div className="flex flex-col">
      {/* Notable events — person cards */}
      {hasNotable ? (
        <div className="flex flex-col gap-1.5 px-3 py-3">
          {data.notable.map((ev) => {
            const mapped = motionEventToInboxEvent(
              ev, cameraNameFor(ev.camera_id), readIds, EMPTY_ARCHIVED,
            );
            const thumbUrl = thumbnailUrl(ev.thumbnail_url, backendBase);
            return (
              <NotableCard
                key={ev.id}
                event={mapped}
                thumbnailUrl={thumbUrl}
                selected={selectedEventId === ev.id}
                onClick={() => onSelectEvent(mapped)}
              />
            );
          })}
        </div>
      ) : (
        <div className="text-center py-10 px-5">
          <div className="text-[#666] text-[13px] mb-1">All quiet</div>
          <div className="text-[#444] text-[11px]">
            Nothing unusual across your cameras today.
          </div>
        </div>
      )}

      {/* Per-camera routine counts */}
      {activeCameras.length > 0 && (
        <div className="border-t border-[#1a1a1a] px-4 py-2.5">
          {activeCameras.map((cam) => (
            <CameraCountLine key={cam.camera_id} camera={cam} />
          ))}
          {quietCameras.map((cam) => (
            <div key={cam.camera_id} className="flex justify-between py-0.5">
              <span className="text-[11px] text-[#444]">{cam.camera_name}</span>
              <span className="text-[11px] text-[#333]">Quiet</span>
            </div>
          ))}
        </div>
      )}

      {/* See all activity link */}
      <div className="border-t border-[#1a1a1a] px-4 py-3 text-center">
        <button
          onClick={onSeeAll}
          className="text-[11px] text-[#666] hover:text-[#999] transition-colors"
        >
          See all activity &rarr;
        </button>
      </div>
    </div>
  );
}

function NotableCard({
  event,
  thumbnailUrl,
  selected,
  onClick,
}: {
  event: InboxEvent;
  thumbnailUrl: string | null;
  selected: boolean;
  onClick: () => void;
}) {
  return (
    <div
      onClick={onClick}
      className={`flex gap-3 p-2.5 rounded-lg cursor-pointer transition-colors ${
        selected
          ? "bg-[rgba(59,130,246,0.12)] ring-1 ring-blue-500/30"
          : "bg-[#141414] hover:bg-[#1a1a1a]"
      }`}
    >
      <div className="relative w-[88px] h-[52px] rounded overflow-hidden shrink-0 bg-[#0a0a0a]">
        {thumbnailUrl ? (
          <img
            src={thumbnailUrl}
            alt=""
            className="w-full h-full object-cover"
            loading="lazy"
          />
        ) : (
          <div className="w-full h-full flex items-center justify-center">
            <svg className="w-4 h-4 opacity-[0.15]" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.4">
              <rect x="2" y="6" width="15" height="12" rx="2" />
              <path d="M17 10 L22 7 L22 17 L17 14 Z" strokeLinejoin="round" />
            </svg>
          </div>
        )}
        <span className="absolute bottom-0.5 right-0.5 text-[8px] text-[#ddd] bg-black/65 px-1 py-[0.5px] rounded-sm tabular-nums">
          {formatDuration(event.duration_s)}
        </span>
      </div>
      <div className="flex-1 min-w-0 flex flex-col justify-center">
        <p className="text-[12.5px] font-semibold text-[#ededed] m-0 truncate">
          {event.title}
        </p>
        <p className="text-[11px] text-[#888] m-0 mt-0.5">
          {event.subtitle} &middot; {formatClock(event.started_at)}
        </p>
      </div>
    </div>
  );
}

function CameraCountLine({ camera }: { camera: TodayCameraSummary }) {
  const parts: string[] = [];
  if (camera.person_count > 0)
    parts.push(`${camera.person_count} ${camera.person_count === 1 ? "person" : "people"}`);
  if (camera.vehicle_count > 0)
    parts.push(`${camera.vehicle_count} ${camera.vehicle_count === 1 ? "vehicle" : "vehicles"}`);
  if (camera.animal_count > 0)
    parts.push(`${camera.animal_count} ${camera.animal_count === 1 ? "animal" : "animals"}`);

  return (
    <div className="flex justify-between py-0.5">
      <span className="text-[11px] text-[#888]">{camera.camera_name}</span>
      <span className="text-[11px] text-[#666] tabular-nums">
        {parts.join(" · ")}
      </span>
    </div>
  );
}
