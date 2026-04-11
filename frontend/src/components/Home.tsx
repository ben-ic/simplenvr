import { useEffect, useMemo, useRef, useState } from "react";
import { fetchTimeline } from "../api/client";
import type { TimelineSegment } from "../api/client";
import { useStorage } from "../hooks/useStorage";
import { recordingFileUrl } from "../lib/backend";
import { cameraDisplayName, formatDuration } from "../lib/format";
import type { Camera, InboxEvent, MotionEvent } from "../types";
import { CameraTile } from "./CameraTile";
import { ErrorBoundary } from "./ErrorBoundary";
import { HistoryPanel, useHistoryCollapsed } from "./HistoryPanel";
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
  go2rtcBaseUrl,
  onBrowseFootage,
  onManageCameras,
}: {
  cameras: Camera[];
  activeMotion: Map<string, string>;
  initialMotionEvents: MotionEvent[] | null;
  go2rtcBaseUrl: string | null;
  onBrowseFootage: (cameraId?: string, startedAt?: string) => void;
  onManageCameras: () => void;
}) {
  const storage = useStorage();
  const [historyCollapsed, toggleHistoryCollapsed] = useHistoryCollapsed();
  const [showSettings, setShowSettings] = useState(false);

  // Which event, if any, is playing in the main stage. null = live mode.
  const [selectedEvent, setSelectedEvent] = useState<InboxEvent | null>(null);

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
              go2rtcBaseUrl={go2rtcBaseUrl}
              onBrowseFootage={(camId) => onBrowseFootage(camId)}
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
  go2rtcBaseUrl,
  onBrowseFootage,
  onSetupCameras,
}: {
  cameras: Camera[];
  activeMotion: Map<string, string>;
  go2rtcBaseUrl: string | null;
  onBrowseFootage: (cameraId: string) => void;
  onSetupCameras: () => void;
}) {
  const [focusedCameraId, setFocusedCameraId] = useState<string | null>(null);

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

  if (focusedCameraId) {
    const cam = cameras.find((c) => c.id === focusedCameraId);
    if (!cam) {
      setFocusedCameraId(null);
      return null;
    }
    return (
      <div className="flex-1 flex flex-col bg-black min-h-0 overflow-hidden">
        <div className="flex items-center px-4 h-10 bg-[#141414] border-b border-[#2a2a2a] shrink-0">
          <button
            onClick={() => setFocusedCameraId(null)}
            className="text-[#888] hover:text-[#ddd] text-sm font-medium"
          >
            &larr; All cameras
          </button>
        </div>
        <div className="flex-1 min-h-0">
          <CameraTile
            key={cam.id}
            camera={cam}
            isMotionActive={activeMotion.has(cam.id)}
            go2rtcBaseUrl={go2rtcBaseUrl}
            onClick={() => setFocusedCameraId(null)}
            onBrowseFootage={() => onBrowseFootage(cam.id)}
          />
        </div>
      </div>
    );
  }

  const cols = cameras.length === 1 ? 1 : cameras.length <= 4 ? 2 : 3;
  const rows = Math.max(1, Math.ceil(cameras.length / cols));
  return (
    <div
      className="flex-1 grid gap-[1px] bg-black p-[1px] min-h-0 overflow-hidden"
      style={{
        gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))`,
        gridTemplateRows: `repeat(${rows}, minmax(0, 1fr))`,
      }}
    >
      {cameras.map((cam) => (
        // Isolate each tile behind an error boundary. A render-time crash
        // in one CameraTile (hls.js decode fault, WebRTC negotiation
        // bug, stale closure) would otherwise unmount the entire grid
        // and blank every other camera the user was watching.
        <ErrorBoundary
          key={cam.id}
          fallback={() => <CameraTileFallback camera={cam} />}
        >
          <CameraTile
            camera={cam}
            isMotionActive={activeMotion.has(cam.id)}
            go2rtcBaseUrl={go2rtcBaseUrl}
            onClick={() => setFocusedCameraId(cam.id)}
            onBrowseFootage={() => onBrowseFootage(cam.id)}
          />
        </ErrorBoundary>
      ))}
    </div>
  );
}

// Fallback tile rendered when a CameraTile throws during render. Keeps
// the rest of the grid alive and tells the user which camera is broken
// instead of blanking the whole screen.
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
        // Backend stores all timestamps as UTC ISO strings and computes
        // second_of_day from the UTC hour. We MUST match that on the
        // frontend — local time would silently desync outside UTC.
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

        // Gap fallback: recorder restarts and segment rollovers sometimes
        // leave events stranded between finalized segments. Fall back to
        // the nearest segment within 5 minutes.
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
