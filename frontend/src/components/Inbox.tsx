import { useEffect, useMemo, useRef, useState } from "react";
import { fetchRecentMotionEvents, fetchTimeline } from "../api/client";
import type { TimelineSegment } from "../api/client";
import { apiUrl } from "../lib/backend";
import type { Camera, InboxEvent, MotionEvent } from "../types";

// Resolve the backend base URL once on mount so we can synchronously
// build thumbnail <img src> strings without racing React renders.
// apiUrl() is async (it invokes a Tauri command to get the backend
// port), so we can't call it inline in JSX — we'd hand <img> a
// Promise<string>, which stringifies to "[object Promise]".
function useBackendBaseUrl(): string | null {
  const [base, setBase] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        // apiUrl("") returns just the base with no path appended.
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

// ---------------------------------------------------------------------------
// Motion event → Inbox event conversion.
//
// v2 ships with motion-only events (no object classification yet). The
// sentence template is intentionally honest: "Motion at [camera]". Once
// the detector lands (NanoDet-Plus / RT-DETR, Apache 2.0 — NOT the
// AGPL-licensed Ultralytics YOLO family) the backend will enrich
// motion_events with person/vehicle/animal/box class labels and this
// conversion will upgrade to "Person at front door" / "Package
// delivered to front porch" style sentences.
//
// Archived/unread state is client-side only for now. When the backend
// adds a reviewed-flag column we'll sync it.
// ---------------------------------------------------------------------------
function motionEventToInboxEvent(
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

  return {
    id: motion.id,
    kind: "person_at_zone",
    title: `Motion at ${cameraName}`,
    subtitle: `${cameraName} · ${durationS} sec`,
    started_at: motion.started_at,
    duration_s: durationS,
    camera_id: motion.camera_id,
    archived: clientArchivedIds.has(motion.id),
    urgent: false,
    unread: !clientReadIds.has(motion.id),
  };
}

// ---------------------------------------------------------------------------
// Time formatting helpers.
// ---------------------------------------------------------------------------
function formatClock(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleTimeString([], {
    hour: "numeric",
    minute: "2-digit",
  });
}

function formatRelativeDay(iso: string): string {
  const d = new Date(iso);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  if (sameDay) return formatClock(iso);
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
// Inbox component. The hero landing view in background mode. Renders a
// list of event rows, supports time-range filtering (Today / Yesterday /
// This week), and expands a row inline into a clip player when clicked.
//
// This is the v2 happy-flow UI. The clip player here is minimal — it shows
// the video-still chrome and Archive/Save actions, but does not yet wire
// to a real video stream. Real wiring happens after the backend event
// library lands.
// ---------------------------------------------------------------------------
type TimeRange = "today" | "yesterday" | "week";

function isInRange(iso: string, range: TimeRange): boolean {
  const d = new Date(iso);
  const now = new Date();
  const startOfToday = new Date(now);
  startOfToday.setHours(0, 0, 0, 0);
  if (range === "today") {
    return d >= startOfToday;
  }
  if (range === "yesterday") {
    const startOfYesterday = new Date(startOfToday);
    startOfYesterday.setDate(startOfYesterday.getDate() - 1);
    return d >= startOfYesterday && d < startOfToday;
  }
  // week
  const weekAgo = new Date(startOfToday);
  weekAgo.setDate(weekAgo.getDate() - 7);
  return d >= weekAgo;
}

export function Inbox({
  cameras,
  onBrowseAllFootage,
  onOpenLiveDashboard,
  onManageCameras,
  onNameCameras,
}: {
  cameras: Camera[];
  onBrowseAllFootage: () => void;
  onOpenLiveDashboard: () => void;
  onManageCameras: () => void;
  onNameCameras: () => void;
}) {
  // Resolved backend base URL for thumbnail <img> srcs.
  const backendBase = useBackendBaseUrl();

  // Real motion events from the backend. Client-side read/archived
  // state is persisted in localStorage so it survives reloads; once
  // the backend gets a reviewed-flag column we'll sync it server-side.
  const [motionEvents, setMotionEvents] = useState<MotionEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
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
  const [range, setRange] = useState<TimeRange>("today");
  const [expandedId, setExpandedId] = useState<string | null>(null);

  // Load recent motion events on mount. Poll every 10 seconds so new
  // events appear without requiring a manual refresh — cheap endpoint,
  // small payload, acceptable for now. WebSocket push will replace
  // polling once we add it to the existing event_bus fan-out.
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

  // Display name priority: user-set name → manufacturer (with IP to
  // disambiguate if there are multiple of the same brand) → hostname
  // → IP. Showing "Reolink (10.0.0.14)" is dramatically more useful
  // than a bare "10.0.0.14" for a camera the user hasn't named yet.
  const cameraNameFor = (camId: string): string => {
    const cam = cameras.find((c) => c.id === camId);
    if (!cam) return "Camera";
    if (cam.name) return cam.name;
    if (cam.manufacturer) return `${cam.manufacturer} (${cam.ip})`;
    if (cam.hostname) return cam.hostname;
    return cam.ip;
  };

  // Derive the Inbox event list from the raw motion events + client state.
  const events = useMemo<InboxEvent[]>(
    () =>
      motionEvents.map((m) =>
        motionEventToInboxEvent(m, cameraNameFor(m.camera_id), readIds, archivedIds),
      ),
    // cameraNameFor depends on `cameras` which is stable for a render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [motionEvents, readIds, archivedIds, cameras],
  );

  const visible = useMemo(
    () => events.filter((e) => !e.archived && isInRange(e.started_at, range)),
    [events, range],
  );
  const unreadCount = useMemo(
    () => visible.filter((e) => e.unread).length,
    [visible],
  );

  const persistSet = (key: string, set: Set<string>) => {
    try {
      localStorage.setItem(key, JSON.stringify([...set]));
    } catch {
      // localStorage quota or disabled — silently ignore.
    }
  };

  const archive = (id: string) => {
    setArchivedIds((prev) => {
      const next = new Set(prev);
      next.add(id);
      persistSet("simplenvr.inbox.archived", next);
      return next;
    });
    if (expandedId === id) setExpandedId(null);
  };

  const toggleExpand = (id: string) => {
    setReadIds((prev) => {
      if (prev.has(id)) return prev;
      const next = new Set(prev);
      next.add(id);
      persistSet("simplenvr.inbox.read", next);
      return next;
    });
    setExpandedId((prev) => (prev === id ? null : id));
  };

  return (
    <div className="flex-1 flex flex-col bg-[#0a0a0a]">
      {/* Topbar — matches Dashboard chrome */}
      <div className="flex items-center justify-between px-5 h-12 bg-[#1a1a1a] border-b border-[#333] shrink-0">
        <div className="flex items-center gap-3">
          <span className="text-[#ddd] font-bold text-[15px]">SimpleNVR</span>
          <span className="flex items-center gap-1.5 text-xs text-[#4ade80] font-medium">
            <span className="w-1.5 h-1.5 rounded-full bg-[#4ade80]" />
            All quiet
          </span>
        </div>
        <div className="flex items-center gap-2">
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
            onClick={onOpenLiveDashboard}
            className="px-3 py-1.5 text-[#888] hover:text-[#ddd] text-xs transition-colors"
          >
            Live
          </button>
          <button
            onClick={onBrowseAllFootage}
            className="px-3 py-1.5 bg-[#222] border border-[#333] text-[#ddd] text-xs font-semibold rounded hover:bg-[#2a2a2a] transition-colors"
          >
            Browse all footage
          </button>
        </div>
      </div>

      {/* Body */}
      <div className="flex-1 overflow-y-auto">
        <div className="max-w-[780px] mx-auto px-6 py-10">
          {/* Header */}
          <div className="flex items-baseline gap-3 mb-5">
            <h1 className="text-[26px] font-bold text-[#ededed] m-0">Inbox</h1>
            <span className="text-[14px] text-[#888]">
              {visible.length === 0
                ? "All caught up"
                : `${unreadCount} new · ${visible.length - unreadCount} earlier`}
            </span>
          </div>

          {/* Time range buttons — big words, no dropdowns */}
          <div className="flex gap-2 mb-5">
            <RangeButton
              active={range === "today"}
              onClick={() => setRange("today")}
              label="Today"
            />
            <RangeButton
              active={range === "yesterday"}
              onClick={() => setRange("yesterday")}
              label="Yesterday"
            />
            <RangeButton
              active={range === "week"}
              onClick={() => setRange("week")}
              label="This week"
            />
          </div>

          {/* Event list — with loading, error, and empty states */}
          {loading && motionEvents.length === 0 ? (
            <div className="text-center py-16 text-[#555] text-sm">
              Loading events…
            </div>
          ) : loadError ? (
            <div className="text-center py-16 text-[#f59e0b] text-sm">
              Could not load events: {loadError}
            </div>
          ) : visible.length === 0 ? (
            <div className="text-center py-16 text-[#555] text-sm">
              Nothing new.
              <br />
              <br />
              <button
                onClick={onBrowseAllFootage}
                className="text-[#888] text-[12px] hover:text-[#ddd] underline underline-offset-2"
              >
                Browse all footage →
              </button>
            </div>
          ) : (
            <div className="flex flex-col gap-1">
              {visible.map((e) => {
                const motion = motionEvents.find((m) => m.id === e.id);
                const thumbUrl =
                  motion?.thumbnail_url && backendBase !== null
                    ? `${backendBase}${motion.thumbnail_url}`
                    : null;
                return (
                  <div key={e.id}>
                    <Row
                      event={e}
                      cameraName={cameraNameFor(e.camera_id)}
                      thumbnailUrl={thumbUrl}
                      onClick={() => toggleExpand(e.id)}
                    />
                    {expandedId === e.id && (
                      <ExpandedClip
                        event={e}
                        cameraName={cameraNameFor(e.camera_id)}
                        onArchive={() => archive(e.id)}
                        onClose={() => setExpandedId(null)}
                      />
                    )}
                  </div>
                );
              })}

              {/* Bottom fallback to the scrubber timeline view */}
              <div className="mt-10 pt-6 border-t border-[#1a1a1a] text-center">
                <button
                  onClick={onBrowseAllFootage}
                  className="text-[#888] text-[12px] hover:text-[#ddd] underline underline-offset-2"
                >
                  Browse all footage →
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sub-components kept inside this file so the Inbox view is a single read.
// ---------------------------------------------------------------------------

function RangeButton({
  active,
  onClick,
  label,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
}) {
  return (
    <button
      onClick={onClick}
      className={
        active
          ? "px-4 py-1.5 bg-[#222] border border-[#333] text-[#ededed] text-[13px] font-semibold rounded-md transition-colors"
          : "px-4 py-1.5 border border-[#2a2a2a] text-[#888] text-[13px] font-medium rounded-md hover:text-[#ddd] transition-colors bg-transparent"
      }
    >
      {label}
    </button>
  );
}

function Row({
  event,
  cameraName,
  thumbnailUrl,
  onClick,
}: {
  event: InboxEvent;
  cameraName: string;
  thumbnailUrl: string | null;
  onClick: () => void;
}) {
  const unreadBg = event.unread
    ? "bg-[rgba(245,158,11,0.06)] border-[rgba(245,158,11,0.25)]"
    : "border-transparent";
  const readOpacity = !event.unread ? "opacity-60" : "";
  return (
    <div
      onClick={onClick}
      className={`flex items-center gap-4 px-3 py-3 rounded-lg border cursor-pointer hover:bg-white/[0.02] transition-colors ${unreadBg} ${readOpacity}`}
    >
      <Thumb
        durationLabel={formatDuration(event.duration_s)}
        thumbnailUrl={thumbnailUrl}
      />
      <div className="flex-1 min-w-0">
        <p className="text-[15px] font-semibold text-[#ededed] m-0">
          {event.title}
          {event.unread && (
            <span className="ml-2 inline-block text-[10px] font-bold tracking-wide uppercase px-1.5 py-0.5 rounded-[10px] bg-[rgba(245,158,11,0.18)] text-[#fbbf24] align-[2px]">
              New
            </span>
          )}
        </p>
        <p className="text-[12.5px] text-[#888] m-0 mt-0.5">
          {cameraName} · {event.subtitle.split(" · ").slice(1).join(" · ") || event.subtitle}
        </p>
      </div>
      <span className="text-[12px] text-[#555] whitespace-nowrap tabular-nums">
        {formatRelativeDay(event.started_at)}
      </span>
    </div>
  );
}

function Thumb({
  durationLabel,
  thumbnailUrl,
}: {
  durationLabel: string;
  thumbnailUrl: string | null;
}) {
  // Prefer the real motion thumbnail when the backend provides one;
  // fall back to a subtle neutral placeholder (thin border, low-
  // saturation gradient, minimal camera glyph). Deliberately NOT an
  // emoji or a colored swatch — per v1 mockup feedback.
  return (
    <div
      className="relative w-[110px] h-[62px] rounded-md border border-[#333] shrink-0 overflow-hidden"
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
          className="absolute inset-0 m-auto w-5 h-5 opacity-[0.18]"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.4"
        >
          <rect x="2" y="6" width="15" height="12" rx="2" />
          <path d="M17 10 L22 7 L22 17 L17 14 Z" strokeLinejoin="round" />
        </svg>
      )}
      <span className="absolute bottom-1 right-1 text-[9.5px] text-[#bbb] bg-black/55 px-1.5 py-[1px] rounded-sm tabular-nums">
        {durationLabel}
      </span>
    </div>
  );
}

function ExpandedClip({
  event,
  cameraName,
  onArchive,
  onClose,
}: {
  event: InboxEvent;
  cameraName: string;
  onArchive: () => void;
  onClose: () => void;
}) {
  // Resolve the recording segment that contains the motion event's
  // timestamp, then render a real <video> element that plays the
  // segment file and seeks to the correct offset on load.
  //
  // There's no direct motion_event → recording relation in the DB, so
  // we fetch the day's timeline for the camera and find the segment
  // whose [second_of_day, second_of_day + duration) contains the
  // event's second-of-day.
  //
  // In-progress segments: the backend computes an effective duration
  // server-side (see backend/api/recordings.py `get_timeline`), so we
  // can just use `segment.duration_s` directly and don't need a
  // frontend workaround.
  //
  // Short recording gaps (recorder restart, segment rollover) leave
  // motion events stranded between finalized segments. Rather than
  // bail, fall back to the nearest segment within 5 minutes and show
  // a small banner explaining the gap.
  const startTime = useMemo(() => new Date(event.started_at), [event.started_at]);
  const [videoSrc, setVideoSrc] = useState<string | null>(null);
  const [seekOffset, setSeekOffset] = useState<number>(0);
  const [gapNotice, setGapNotice] = useState<string | null>(null);
  const [resolveError, setResolveError] = useState<string | null>(null);
  const videoRef = useRef<HTMLVideoElement | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        // Backend stores all timestamps as UTC ISO strings with +00:00
        // suffix, and computes second_of_day from the UTC hour
        // (datetime.fromisoformat(...).hour). We MUST match that on
        // the frontend — using local getHours()/getDate() silently
        // breaks in every non-UTC timezone.
        const utcDate = startTime.toISOString().slice(0, 10); // YYYY-MM-DD (UTC)
        const eventSecondOfDay =
          startTime.getUTCHours() * 3600 +
          startTime.getUTCMinutes() * 60 +
          startTime.getUTCSeconds();

        const timeline = await fetchTimeline(event.camera_id, utcDate);

        // Exact match: the event falls inside this segment's range.
        // Backend returns effective duration for in-progress rows,
        // so `s.duration_s` is authoritative.
        const exact: TimelineSegment | undefined = timeline.segments.find(
          (s) =>
            eventSecondOfDay >= s.second_of_day &&
            eventSecondOfDay < s.second_of_day + s.duration_s,
        );

        let segment: TimelineSegment | undefined = exact;
        let gap: string | null = null;

        // Gap fallback: no segment contains the event. Find the
        // closest segment within 5 minutes (before or after) and
        // surface the gap so the user understands what they're seeing.
        if (!segment && timeline.segments.length > 0) {
          const WINDOW = 5 * 60; // seconds
          let best: TimelineSegment | undefined;
          let bestDelta = Infinity;
          for (const s of timeline.segments) {
            const segEnd = s.second_of_day + s.duration_s;
            // Distance from the event to the closest edge of the segment.
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
            gap = `Motion was during a brief recording gap — playing the nearest segment (${mins} min away).`;
          }
        }

        if (!segment) {
          if (!cancelled) {
            setResolveError("No recording found near this moment");
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
            err instanceof Error ? err.message : "Failed to load clip",
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
    <div className="mt-3 border border-[#333] rounded-[10px] overflow-hidden bg-[#080808]">
      <div className="flex items-center justify-between px-4 py-2.5 bg-[#1a1a1a] border-b border-[#2a2a2a] text-[12px] text-[#888] tabular-nums">
        <span>
          <strong className="text-[#ededed] font-semibold">{cameraName}</strong>
          &nbsp;·&nbsp;
          {startTime.toLocaleDateString([], {
            weekday: "short",
            month: "short",
            day: "numeric",
          })}{" "}
          ·{" "}
          {startTime.toLocaleTimeString([], {
            hour: "numeric",
            minute: "2-digit",
            second: "2-digit",
          })}
        </span>
        <span>{formatDuration(event.duration_s)}</span>
      </div>
      {gapNotice && (
        <div className="px-4 py-2 bg-[rgba(245,158,11,0.08)] border-b border-[rgba(245,158,11,0.25)] text-[12px] text-[#fbbf24]">
          {gapNotice}
        </div>
      )}
      <div
        className="aspect-video flex items-center justify-center bg-black"
        style={
          videoSrc
            ? undefined
            : {
                background:
                  "radial-gradient(circle at 40% 50%, #1a1a1a, #050505 75%)",
              }
        }
      >
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
          <div className="text-[#888] text-[13px] text-center px-6">
            {resolveError}
          </div>
        ) : (
          <svg
            className="w-12 h-12 opacity-[0.15]"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.2"
          >
            <rect x="2" y="6" width="15" height="12" rx="2" />
            <path d="M17 10 L22 7 L22 17 L17 14 Z" strokeLinejoin="round" />
          </svg>
        )}
      </div>
      {/* Actions */}
      <div className="flex gap-2.5 px-4 py-3 bg-[#141414] border-t border-[#1a1a1a]">
        <button
          disabled={!videoSrc}
          className="px-4 py-2 bg-[#2b4c1f] border border-[#3a6428] text-[#d9f5c4] text-[12.5px] font-semibold rounded-md hover:bg-[#355d24] transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
        >
          Save this clip
        </button>
        <button
          onClick={onArchive}
          className="px-4 py-2 bg-[#222] border border-[#333] text-[#ededed] text-[12.5px] font-semibold rounded-md hover:bg-[#2a2a2a] transition-colors"
        >
          Archive
        </button>
        <div className="flex-1" />
        <button
          onClick={onClose}
          className="px-4 py-2 bg-transparent text-[#888] text-[12.5px] font-semibold rounded-md hover:text-[#ededed] transition-colors"
        >
          Close
        </button>
      </div>
    </div>
  );
}
