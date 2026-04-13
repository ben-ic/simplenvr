import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { setAllVisible } from "tauri-plugin-rtsp-mosaic-api";
import { fetchTimeline } from "../api/client";
import type { TimelineSegment } from "../api/client";
import { useStorage } from "../hooks/useStorage";
import { recordingFileUrl } from "../lib/backend";
import { cameraDisplayName, formatDuration } from "../lib/format";
import type { Camera, InboxEvent, MotionEvent } from "../types";
import { ErrorBoundary } from "./ErrorBoundary";
import { HistoryPanel, useHistoryCollapsed } from "./HistoryPanel";
import { NativeCameraTile } from "./NativeCameraTile";
import { SettingsModal } from "./SettingsModal";
import { StorageBanner } from "./StorageBanner";

// ---------------------------------------------------------------------------
// Home — the unified hero screen. Layout is the shared split view
// (HistoryPanel on the left, main stage on the right) with a
// Cursor-style resizable handle between them. See HistoryPanel.tsx
// for the panel itself and its state management.
//
// Main stage has two modes:
//   - LIVE: grid of camera tiles (default)
//   - CLIP: full-stage event playback with ← Live button
//
// Clicking a history row switches the main stage to CLIP for that
// event; clicking ← Live restores the grid. First-run correctness:
// because the live grid is always visible in the main stage, a
// brand-new user with zero events still sees their cameras light up
// on first launch — the old "empty Inbox on first run" failure mode
// is structurally impossible here.
// ---------------------------------------------------------------------------

export function Home({
  cameras,
  activeMotion,
  initialMotionEvents,
  onBrowseFootage,
  onManageCameras,
}: {
  cameras: Camera[];
  activeMotion: Map<string, string>;
  initialMotionEvents: MotionEvent[] | null;
  onBrowseFootage: (cameraId?: string, startedAt?: string) => void;
  onManageCameras: () => void;
}) {
  const storage = useStorage();
  const [historyCollapsed, toggleHistoryCollapsed] = useHistoryCollapsed();
  const [showSettings, setShowSettings] = useState(false);

  // Timestamp for change verification
  const [timestamp, setTimestamp] = useState(() => new Date().toLocaleTimeString());

  useEffect(() => {
    const interval = setInterval(() => {
      setTimestamp(new Date().toLocaleTimeString());
    }, 1000);
    return () => clearInterval(interval);
  }, []);

  // Which event, if any, is playing in the main stage. null = live mode.
  const [selectedEvent, setSelectedEvent] = useState<InboxEvent | null>(null);

  // Hide native tiles when viewing a clip or a modal so they don't
  // render over the overlay. The <rtsp-tile> elements stay mounted
  // (React keeps them alive) — we just toggle the native NSView
  // visibility.
  useEffect(() => {
    if (selectedEvent || showSettings) {
      setAllVisible(false).catch(() => {});
    } else {
      setAllVisible(true).catch(() => {});
    }
  }, [selectedEvent, showSettings]);

  const online = cameras.filter((c) => c.status === "online" && c.rtsp_uri);
  const offlineCount = cameras.filter((c) => c.status !== "online").length;

  const cameraNameFor = (camId: string): string =>
    cameraDisplayName(cameras.find((c) => c.id === camId));

  return (
    <div
      className="flex flex-col bg-[#0a0a0a] text-[#ededed] overflow-hidden"
      style={{ height: "100vh", maxHeight: "100vh" }}
    >
      {/* Topbar */}
      <div className="flex items-center justify-between pl-2 pr-5 h-12 bg-[#1a1a1a] border-b border-[#333] shrink-0">
        <div className="flex items-center gap-3">
          <HistoryToggleButton
            collapsed={historyCollapsed}
            onToggle={toggleHistoryCollapsed}
          />
          <span className="text-[#ddd] font-bold text-[15px]">SimpleNVR</span>
          <span className="text-[#666] text-[10px] font-mono">{timestamp}</span>
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
            onClick={onManageCameras}
            className="px-3 py-1.5 text-[#888] hover:text-[#ddd] text-xs transition-colors"
          >
            Camera setup
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
        <HistoryPanel
          cameras={cameras}
          initialMotionEvents={initialMotionEvents}
          selectedEventId={selectedEvent?.id ?? null}
          onSelectEvent={setSelectedEvent}
          collapsed={historyCollapsed}
        />

        {/* Main stage */}
        <div className="flex-1 flex flex-col min-w-0 min-h-0 relative">
          {selectedEvent ? (
            <ErrorBoundary
              fallback={() => (
                <ClipStageFallback
                  cameraName={cameraNameFor(selectedEvent.camera_id)}
                  onBackToLive={() => setSelectedEvent(null)}
                />
              )}
            >
              <ClipStage
                event={selectedEvent}
                cameraName={cameraNameFor(selectedEvent.camera_id)}
                onBackToLive={() => setSelectedEvent(null)}
                onOpenInBrowseFootage={() =>
                  onBrowseFootage(selectedEvent.camera_id, selectedEvent.started_at)
                }
              />
            </ErrorBoundary>
          ) : (
            <LiveGrid
              cameras={online}
              activeMotion={activeMotion}
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
// Shared history toggle button. Exported so Recordings can use the same
// button in its topbar, keeping the affordance identical across screens.
// ---------------------------------------------------------------------------

export function HistoryToggleButton({
  collapsed,
  onToggle,
}: {
  collapsed: boolean;
  onToggle: () => void;
}) {
  return (
    <button
      onClick={onToggle}
      className="p-1.5 text-[#888] hover:text-[#ddd] transition-colors"
      title={collapsed ? "Show history" : "Hide history"}
      aria-label={collapsed ? "Show history" : "Hide history"}
    >
      {collapsed ? (
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
          <path
            d="M9 6l-6 6 6 6"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
          <path d="M21 6v12" strokeLinecap="round" />
          <path d="M14 6v12" strokeLinecap="round" />
        </svg>
      )}
    </button>
  );
}

// ---------------------------------------------------------------------------
// Live grid — the default main-stage mode.
// ---------------------------------------------------------------------------

function LiveGrid({
  cameras,
  activeMotion,
  onSetupCameras,
}: {
  cameras: Camera[];
  activeMotion: Map<string, string>;
  onSetupCameras: () => void;
}) {
  const [focusedCameraId, setFocusedCameraId] = useState<string | null>(null);
  const toggleFocus = useCallback(
    (id: string) => setFocusedCameraId((prev) => (prev === id ? null : id)),
    [],
  );
  const tileRefs = useRef<Map<string, HTMLElement>>(new Map());

  useEffect(() => {
    if (!focusedCameraId) return;
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setFocusedCameraId(null);
    };
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [focusedCameraId]);

  // Clear focus if the focused camera goes offline / disappears
  useEffect(() => {
    if (focusedCameraId && !cameras.find((c) => c.id === focusedCameraId)) {
      setFocusedCameraId(null);
    }
  }, [cameras, focusedCameraId]);

  // Set the fullscreen attribute on the focused tile. The plugin
  // handles hiding all other tiles and expanding this one.
  useEffect(() => {
    for (const [id, el] of tileRefs.current) {
      const tile = el.querySelector("rtsp-tile") as any;
      if (!tile) continue;
      if (id === focusedCameraId) {
        tile.fullscreen = true;
      } else {
        tile.fullscreen = false;
      }
    }
  }, [focusedCameraId]);

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

  // Layout rules:
  //   1 camera  → full screen (1 col)
  //   2 cameras → stacked vertically, each full width (1 col, 2 rows)
  //   3 cameras → 2 top + 1 bottom spanning full width (2 cols)
  //   4 cameras → 2×2 grid (2 cols)
  //   5+ cameras → 3 cols
  const cols = cameras.length <= 2 ? 1 : cameras.length <= 4 ? 2 : 3;
  const rows = Math.max(1, Math.ceil(cameras.length / cols));
  const lastRowSpans = cameras.length === 3;

  return (
    <div
      className="flex-1 grid gap-[1px] bg-black p-[1px] min-h-0 overflow-hidden"
      style={{
        gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))`,
        gridTemplateRows: `repeat(${rows}, minmax(0, 1fr))`,
      }}
    >
      {cameras.map((cam, i) => (
        <ErrorBoundary
          key={cam.id}
          fallback={() => <CameraTileFallback camera={cam} />}
        >
          <div
            ref={(el) => {
              if (el) tileRefs.current.set(cam.id, el);
              else tileRefs.current.delete(cam.id);
            }}
            className={
              focusedCameraId === cam.id
                ? "fixed inset-0 z-50 bg-black"
                : focusedCameraId
                  ? "hidden"
                  : "h-full w-full min-h-0"
            }
            style={
              !focusedCameraId && lastRowSpans && i === cameras.length - 1
                ? { gridColumn: "1 / -1" }
                : undefined
            }
          >
            <NativeCameraTile
              camera={cam}
              isMotionActive={activeMotion.has(cam.id)}
              isFocused={focusedCameraId === cam.id}
              onToggleFocus={toggleFocus}
            />
          </div>
        </ErrorBoundary>
      ))}
    </div>
  );
}

// Fallback tile rendered when a NativeCameraTile throws during render.
// Keeps the rest of the grid alive and tells the user which camera is
// broken instead of blanking the whole screen.
function CameraTileFallback({ camera }: { camera: Camera }) {
  return (
    <div className="relative w-full h-full bg-[#0a0a0a] flex items-center justify-center">
      <div className="text-center px-4">
        <div className="text-[#888] text-xs mb-1">
          {cameraDisplayName(camera)}
        </div>
        <div className="text-[#555] text-[11px]">
          Couldn&rsquo;t load this camera. Try reopening.
        </div>
      </div>
    </div>
  );
}

function ClipStageFallback({
  cameraName,
  onBackToLive,
}: {
  cameraName: string;
  onBackToLive: () => void;
}) {
  return (
    <div className="flex-1 flex flex-col bg-black min-h-0">
      <div className="flex items-center px-4 h-11 bg-[#141414] border-b border-[#2a2a2a] shrink-0">
        <button
          onClick={onBackToLive}
          className="text-[#888] hover:text-[#ddd] text-sm font-medium"
        >
          ← Live
        </button>
      </div>
      <div className="flex-1 flex items-center justify-center text-center px-6">
        <div className="text-[#888] text-sm max-w-md">
          Couldn&rsquo;t play this clip from {cameraName}. Try reopening from
          Browse footage.
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Clip stage — full-stage motion event playback with a ← Live button
// and an "Open in Browse footage" shortcut. Uses the day-timeline
// endpoint to map the event's wall-clock time back to its containing
// segment, then seeks into that segment's mp4 file on load.
// ---------------------------------------------------------------------------

function ClipStage({
  event,
  cameraName,
  onBackToLive,
  onOpenInBrowseFootage,
}: {
  event: InboxEvent;
  cameraName: string;
  onBackToLive: () => void;
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
        // Recording rows are date-indexed in UTC, while second_of_day in the
        // timeline is localized for UI rendering. Try multiple date candidates
        // so motion events around local midnight still resolve to a segment.
        const localDate = new Date(
          startTime.getTime() - startTime.getTimezoneOffset() * 60000,
        )
          .toISOString()
          .slice(0, 10);
        const utcDate = event.started_at.slice(0, 10);

        const prevLocal = new Date(startTime);
        prevLocal.setDate(prevLocal.getDate() - 1);
        const prevLocalDate = new Date(
          prevLocal.getTime() - prevLocal.getTimezoneOffset() * 60000,
        )
          .toISOString()
          .slice(0, 10);

        const nextLocal = new Date(startTime);
        nextLocal.setDate(nextLocal.getDate() + 1);
        const nextLocalDate = new Date(
          nextLocal.getTime() - nextLocal.getTimezoneOffset() * 60000,
        )
          .toISOString()
          .slice(0, 10);

        const dateCandidates = Array.from(
          new Set([localDate, utcDate, prevLocalDate, nextLocalDate]),
        );

        const eventSecondOfDay =
          startTime.getHours() * 3600 +
          startTime.getMinutes() * 60 +
          startTime.getSeconds();

        let allSegments: TimelineSegment[] = [];
        for (const date of dateCandidates) {
          try {
            const timeline = await fetchTimeline(event.camera_id, date);
            allSegments = allSegments.concat(timeline.segments);
          } catch {
            // Ignore missing dates and keep trying candidates.
          }
        }

        const exact: TimelineSegment | undefined = allSegments.find(
          (s) =>
            eventSecondOfDay >= s.second_of_day &&
            eventSecondOfDay < s.second_of_day + s.duration_s,
        );

        let segment: TimelineSegment | undefined = exact;
        let gap: string | null = null;

        // Gap fallback: recorder restarts and segment rollovers sometimes
        // leave events stranded between finalized segments. Fall back to
        // the nearest segment within 5 minutes.
        if (!segment && allSegments.length > 0) {
          const WINDOW = 5 * 60;
          let best: TimelineSegment | undefined;
          let bestDelta = Infinity;
          for (const s of allSegments) {
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

        const src = await recordingFileUrl(segment.id);
        const offset = Math.max(0, eventSecondOfDay - segment.second_of_day - 2);
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
    // startTime is derived from event.started_at via useMemo, so it changes
    // one-for-one with started_at. Listing both would double-fire the effect
    // under React Strict Mode / concurrent rendering.
  }, [event.camera_id, event.started_at]);

  const handleLoadedMetadata = () => {
    const v = videoRef.current;
    if (!v) return;
    v.currentTime = seekOffset;
    v.play().catch(() => {
      // Autoplay policies may block; user can still press play.
    });
  };

  const handleVideoError = () => {
    setResolveError("Failed to load video file");
  };

  return (
    <div className="flex-1 flex flex-col bg-black min-h-0">
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
            })}{" "}
            · {formatDuration(event.duration_s)}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={onOpenInBrowseFootage}
            className="px-3 py-1.5 text-[#888] hover:text-[#ddd] text-xs transition-colors"
          >
            Open in Browse footage
          </button>
        </div>
      </div>

      {(event.summary || event.description) && (
        <div className="px-4 py-2.5 bg-[#1a1a1a] border-b border-[#2a2a2a] shrink-0">
          {event.summary && (
            <p className="text-[13px] text-[#ccc] leading-relaxed m-0">
              {event.summary}
            </p>
          )}
          {event.description && (
            <p className="text-[11px] text-[#666] leading-relaxed m-0 mt-1">
              Details: {event.description}
            </p>
          )}
        </div>
      )}

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
            onError={handleVideoError}
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
