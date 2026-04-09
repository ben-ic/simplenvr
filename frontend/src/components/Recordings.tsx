import Hls from "hls.js";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  fetchMotionTimeline,
  fetchRecordingDates,
  fetchTimeline,
  type MotionTimelineEntry,
  type Timeline as TimelineData,
} from "../api/client";
import { apiUrl } from "../lib/backend";
import {
  formatClock,
  playlistTimeToSecondOfDay,
  presetToWindow,
  secondOfDayToPlaylistTime,
  type TimelinePreset,
  type TimelineScale,
} from "../lib/timelineMath";
import type { Camera, InboxEvent, MotionEvent } from "../types";
import { HistoryPanel, useHistoryCollapsed } from "./HistoryPanel";
import { HistoryToggleButton } from "./Home";
import { RecordingsTimeline } from "./RecordingsTimeline";

const DAY_SECONDS = 86400;

interface RecordingsProps {
  cameras: Camera[];
  onBack: () => void;
  initialCameraId?: string;
  initialStartedAt?: string;
  /** Bumped by useDiscovery when the backend fires recordings_deleted. */
  lastRecordingsDeleted?: { camera_ids: string[]; at: number } | null;
  /** Seeds the shared HistoryPanel so it doesn't flash an empty state
   * on cold start while the 10s poll catches up. */
  initialMotionEvents: MotionEvent[] | null;
}

export function Recordings({
  cameras,
  onBack,
  initialCameraId,
  initialStartedAt,
  lastRecordingsDeleted,
  initialMotionEvents,
}: RecordingsProps) {
  const [historyCollapsed, toggleHistoryCollapsed] = useHistoryCollapsed();
  const [selectedHistoryEventId, setSelectedHistoryEventId] = useState<
    string | null
  >(null);
  const cameraOptions = useMemo(
    () => cameras.filter((c) => c.rtsp_uri),
    [cameras],
  );

  const [selectedCameraId, setSelectedCameraId] = useState<string>(
    initialCameraId || cameraOptions[0]?.id || "",
  );
  const [dates, setDates] = useState<string[]>([]);
  const [selectedDate, setSelectedDate] = useState<string>("");
  const [timeline, setTimeline] = useState<TimelineData | null>(null);
  const [motionEvents, setMotionEvents] = useState<MotionTimelineEntry[]>([]);
  const [currentSecond, setCurrentSecond] = useState(0);
  const [playing, setPlaying] = useState(true);
  const [speed, setSpeed] = useState(1);
  const [preset, setPreset] = useState<TimelinePreset>("today");
  const [scale, setScale] = useState<TimelineScale>("24h");
  const [viewStart, setViewStart] = useState(0);
  const [viewEnd, setViewEnd] = useState(DAY_SECONDS);
  // Surfaced to the user when hls.js reports a fatal error. Until this
  // existed, the <video> element just sat black with only a console
  // message, violating "fail loudly" from docs/product.md.
  const [hlsFatalError, setHlsFatalError] = useState<string | null>(null);
  // Bumped by the retry button to force the engine-init effect to
  // re-run and re-request the playlist.
  const [hlsRetryKey, setHlsRetryKey] = useState(0);
  const handleHlsRetry = () => setHlsRetryKey((k) => k + 1);

  const videoRef = useRef<HTMLVideoElement>(null);
  const hlsRef = useRef<Hls | null>(null);
  // Tracks the (camera, date) pair we've already snapped the playhead
  // to. Lets recordings_deleted refreshes leave the user's scrub
  // position alone instead of yanking it to the latest segment.
  const playheadInitKeyRef = useRef<string>("");

  // Auto-select first camera
  useEffect(() => {
    if (!selectedCameraId && cameraOptions.length > 0) {
      setSelectedCameraId(cameraOptions[0].id);
    }
  }, [cameraOptions, selectedCameraId]);

  // Load available dates when camera changes
  useEffect(() => {
    if (!selectedCameraId) return;
    let cancelled = false;
    fetchRecordingDates(selectedCameraId).then((d) => {
      if (cancelled) return;
      setDates(d);
      if (d.length > 0 && !d.includes(selectedDate)) {
        setSelectedDate(d[0]);
      } else if (d.length === 0) {
        setSelectedDate("");
        setTimeline(null);
      }
    });
    return () => {
      cancelled = true;
    };
    // selectedDate intentionally excluded — we only reload on camera change
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedCameraId]);

  // Load timeline + motion events when camera+date changes
  const loadTimeline = useCallback(() => {
    if (!selectedCameraId || !selectedDate) return;
    fetchTimeline(selectedCameraId, selectedDate).then((tl) => {
      setTimeline(tl);
      if (tl.segments.length === 0) return;
      const key = `${selectedCameraId}::${selectedDate}`;
      const last = tl.segments[tl.segments.length - 1];
      if (playheadInitKeyRef.current !== key) {
        // First load for this camera/date — snap to the latest segment.
        playheadInitKeyRef.current = key;
        setCurrentSecond(last.second_of_day);
        return;
      }
      // Subsequent refresh (e.g. recordings_deleted). Preserve the
      // user's current scrub position UNLESS it now points outside
      // every segment, in which case fall back to the latest.
      setCurrentSecond((cur) => {
        const inside = tl.segments.some(
          (s) => cur >= s.second_of_day && cur < s.second_of_day + s.duration_s,
        );
        return inside ? cur : last.second_of_day;
      });
    });
    fetchMotionTimeline(selectedCameraId, selectedDate)
      .then(setMotionEvents)
      .catch(() => setMotionEvents([]));
  }, [selectedCameraId, selectedDate]);

  useEffect(() => {
    loadTimeline();
  }, [loadTimeline]);

  // Subscribe to recordings_deleted — refetch if the selected camera is
  // implicated. The hook bumps `at` on every event so repeated deletions
  // to the same camera list still trigger this effect.
  useEffect(() => {
    if (!lastRecordingsDeleted) return;
    if (!selectedCameraId) return;
    if (lastRecordingsDeleted.camera_ids.includes(selectedCameraId)) {
      loadTimeline();
    }
  }, [lastRecordingsDeleted, selectedCameraId, loadTimeline]);

  // Jump to motion event start time when arriving from Dashboard/Inbox.
  // We look the timestamp up against the authoritative second_of_day on
  // the matching segment row instead of re-deriving it from the Date
  // object — the backend owns the canonical value in /timeline and
  // if it ever normalizes timezones the two computations would drift.
  const jumpedRef = useRef(false);
  useEffect(() => {
    if (jumpedRef.current) return;
    if (!initialStartedAt || !timeline || timeline.segments.length === 0) return;
    const match = timeline.segments.find((s) => s.started_at === initialStartedAt);
    if (match) {
      setCurrentSecond(match.second_of_day);
    } else {
      // Fallback for the rare case where initialStartedAt doesn't match
      // any segment exactly (e.g. motion event mid-segment): derive from
      // the Date object.
      const d = new Date(initialStartedAt);
      setCurrentSecond(d.getHours() * 3600 + d.getMinutes() * 60 + d.getSeconds());
    }
    jumpedRef.current = true;
  }, [initialStartedAt, timeline]);

  // HLS engine: load a single VOD playlist for the whole day. This is
  // the key fix — no per-segment src switching, so no seek race.
  useEffect(() => {
    const video = videoRef.current;
    if (!video || !selectedCameraId || !selectedDate) return;
    if (!timeline || timeline.segments.length === 0) return;

    // Clear any prior fatal error now that we're re-initializing.
    setHlsFatalError(null);

    let cancelled = false;
    (async () => {
      // Flat HLS VOD playlist built by backend/api/recordings.py
      // `get_hls_index`. It lists each recorded fragmented-MP4
      // segment as a direct /file URL with #EXT-X-DISCONTINUITY
      // between them. hls.js handles per-segment PTS normalization
      // via its built-in timestampOffset logic — no repackaging,
      // no init.mp4, no tfdt rewrite. Verified 2026-04-09.
      const playlistUrl = await apiUrl(
        `/api/recordings/hls/index.m3u8?camera_id=${encodeURIComponent(
          selectedCameraId,
        )}&date=${encodeURIComponent(selectedDate)}`,
      );
      if (cancelled) return;

      // Tear down any prior engine before attaching a new one.
      if (hlsRef.current) {
        hlsRef.current.destroy();
        hlsRef.current = null;
      }

      // Map second-of-day → playlist time using cumulative segment
      // durations. Plain `current - firstStart` arithmetic would
      // desync the moment the day has any gap in it.
      const initialPlaylistTime = secondOfDayToPlaylistTime(
        timeline.segments,
        currentSecond,
      );

      if (Hls.isSupported()) {
        const hls = new Hls({
          maxBufferLength: 60,
          backBufferLength: 30,
        });
        hlsRef.current = hls;
        hls.on(Hls.Events.ERROR, (_evt, data) => {
          // Non-fatal errors are normal (hls.js probing segment
          // types etc.) and don't break playback. Fatal errors do,
          // and need to surface as a user-facing message with a
          // retry — not a silent console.error.
          if (data.fatal) {
            const reason =
              data.reason ||
              data.details ||
              data.type ||
              "Playback engine failed";
            setHlsFatalError(
              `Couldn't play this day's footage (${reason}). Try again in a moment.`,
            );
          }
        });
        hls.loadSource(playlistUrl);
        hls.attachMedia(video);
        hls.on(Hls.Events.MANIFEST_PARSED, () => {
          if (cancelled) return;
          if (initialPlaylistTime > 0) {
            video.currentTime = initialPlaylistTime;
          }
          if (playing) video.play().catch(() => {});
        });
      } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
        // Native HLS (Safari / WebKit Tauri webview)
        video.src = playlistUrl;
        video.addEventListener(
          "loadedmetadata",
          () => {
            if (initialPlaylistTime > 0) {
              video.currentTime = initialPlaylistTime;
            }
            if (playing) video.play().catch(() => {});
          },
          { once: true },
        );
      }
    })();

    return () => {
      cancelled = true;
      if (hlsRef.current) {
        hlsRef.current.destroy();
        hlsRef.current = null;
      }
    };
    // Only reload the engine on camera/date change — NOT on currentSecond
    // or playing. Those drive imperative video control, not re-init.
    // Retry key forces re-init when the user dismisses a fatal error.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedCameraId, selectedDate, timeline?.segments.length, hlsRetryKey]);

  // Sync playhead from video time as it plays. Walk segment durations
  // to map playlist time → second of day, so the displayed clock skips
  // gaps the same way HLS does on playback.
  const handleTimeUpdate = useCallback(() => {
    const video = videoRef.current;
    if (!video || !timeline) return;
    setCurrentSecond(
      playlistTimeToSecondOfDay(timeline.segments, video.currentTime),
    );
  }, [timeline]);

  // Click on timeline → seek. The clock-to-playlist mapping is what
  // kills the seek race; without it, gaps in the day silently
  // mis-align the playhead.
  const handleSeek = useCallback(
    (second: number) => {
      const v = videoRef.current;
      setCurrentSecond(second);
      if (!v || !timeline) return;
      v.currentTime = secondOfDayToPlaylistTime(timeline.segments, second);
    },
    [timeline],
  );

  // Play/pause
  const togglePlay = useCallback(() => {
    const video = videoRef.current;
    if (!video) return;
    if (video.paused) {
      video.play().catch(() => {});
      setPlaying(true);
    } else {
      video.pause();
      setPlaying(false);
    }
  }, []);

  // Speed control
  useEffect(() => {
    const video = videoRef.current;
    if (video) video.playbackRate = speed;
  }, [speed]);

  // Preset → window
  const applyPreset = useCallback(
    (p: TimelinePreset) => {
      setPreset(p);
      const w = presetToWindow(p, new Date());
      if (w.date !== selectedDate && dates.includes(w.date)) {
        setSelectedDate(w.date);
      }
      setViewStart(w.startSecond);
      setViewEnd(w.endSecond);
      setScale(w.scale);
    },
    [selectedDate, dates],
  );

  // Scale button — zooms around playhead
  const handleScaleChange = useCallback(
    (s: TimelineScale) => {
      setScale(s);
      let span: number;
      switch (s) {
        case "1h":
          span = 3600;
          break;
        case "6h":
          span = 6 * 3600;
          break;
        case "24h":
          span = DAY_SECONDS;
          break;
        case "7d":
          span = DAY_SECONDS; // 7d stays 24h for single-date view
          break;
      }
      if (s === "24h" || s === "7d") {
        setViewStart(0);
        setViewEnd(DAY_SECONDS);
        return;
      }
      const half = span / 2;
      const start = Math.max(0, Math.min(DAY_SECONDS - span, currentSecond - half));
      setViewStart(start);
      setViewEnd(start + span);
    },
    [currentSecond],
  );

  // Keyboard shortcuts. `currentSecond` is read via ref inside the
  // handler rather than listed as a dep — otherwise the window keydown
  // listener would be removed + re-added on every onTimeUpdate tick
  // (~4×/s during playback). Same for `timeline`.
  const currentSecondRef = useRef(currentSecond);
  currentSecondRef.current = currentSecond;
  const timelineRef = useRef(timeline);
  timelineRef.current = timeline;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement) return;
      if (e.target instanceof HTMLSelectElement) return;
      if (e.target instanceof HTMLTextAreaElement) return;

      const cur = currentSecondRef.current;
      const tl = timelineRef.current;

      switch (e.key) {
        case " ":
          e.preventDefault();
          togglePlay();
          return;
        case "ArrowRight":
          e.preventDefault();
          handleSeek(
            Math.min(DAY_SECONDS - 1, cur + (e.shiftKey ? 60 : 10)),
          );
          return;
        case "ArrowLeft":
          e.preventDefault();
          handleSeek(Math.max(0, cur - (e.shiftKey ? 60 : 10)));
          return;
        case "Home":
          if (tl?.segments[0]) {
            handleSeek(tl.segments[0].second_of_day);
          }
          return;
        case "End":
          if (tl && tl.segments.length > 0) {
            const last = tl.segments[tl.segments.length - 1];
            handleSeek(last.second_of_day + last.duration_s - 2);
          }
          return;
      }

      if (e.key === "1") setSpeed(1);
      else if (e.key === "2") setSpeed(2);
      else if (e.key === "4") setSpeed(4);
      else if (e.key === "8") setSpeed(8);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [togglePlay, handleSeek]);

  const cameraName = (cam: Camera): string =>
    cam.name ||
    [cam.manufacturer, cam.model].filter(Boolean).join(" ") ||
    cam.ip;

  // Clicking a history event seeks the scrubber: switch camera if
  // needed, set the date to the event's UTC day, and jump currentSecond
  // to the event's second-of-day. The existing loadTimeline effect
  // picks up the (camera, date) change and the HLS engine effect
  // re-inits.
  const handleHistorySelect = useCallback(
    (event: InboxEvent) => {
      setSelectedHistoryEventId(event.id);
      const d = new Date(event.started_at);
      const utcDate = d.toISOString().slice(0, 10);
      const second =
        d.getUTCHours() * 3600 + d.getUTCMinutes() * 60 + d.getUTCSeconds();
      setSelectedCameraId(event.camera_id);
      setSelectedDate(utcDate);
      setCurrentSecond(second);
      setPreset("custom");
      // Reset playhead init key so loadTimeline knows to honor the
      // new currentSecond instead of snapping to last segment.
      playheadInitKeyRef.current = "";
    },
    [],
  );

  const selectedCamera = cameras.find((c) => c.id === selectedCameraId);
  const visibleSegments = timeline?.segments ?? [];
  const motionLike = useMemo(
    () =>
      (motionEvents ?? []).map((m) => ({
        second_of_day: m.second_of_day,
        duration_s: m.duration_s,
      })),
    [motionEvents],
  );

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
          <button
            onClick={onBack}
            className="text-[#888] hover:text-[#ddd] text-sm"
          >
            ← Home
          </button>
          <span className="text-[#ddd] font-bold text-[15px]">Browse footage</span>
        </div>
        <div className="flex items-center gap-2">
          {dates.length > 0 && (
            <select
              value={selectedDate}
              onChange={(e) => {
                setSelectedDate(e.target.value);
                setPreset("custom");
              }}
              className="px-2 py-1 bg-[#222] border border-[#333] rounded text-xs text-[#ddd] outline-none"
            >
              {dates.map((d) => (
                <option key={d} value={d}>
                  {formatDate(d)}
                </option>
              ))}
            </select>
          )}
        </div>
      </div>

      <div className="flex-1 flex min-h-0 overflow-hidden">
        <HistoryPanel
          cameras={cameras}
          initialMotionEvents={initialMotionEvents}
          selectedEventId={selectedHistoryEventId}
          onSelectEvent={handleHistorySelect}
          collapsed={historyCollapsed}
        />

        {/* Camera rail */}
        <div className="w-[240px] border-r border-[#222] bg-[#111] flex flex-col shrink-0">
          <div className="px-4 py-3 text-[11px] uppercase tracking-wide text-[#666] font-semibold">
            Cameras <span className="text-[#444]">· {cameraOptions.length}</span>
          </div>
          <div className="flex-1 overflow-y-auto">
            {cameraOptions.map((cam) => (
              <button
                key={cam.id}
                onClick={() => setSelectedCameraId(cam.id)}
                className={`w-full flex items-center gap-3 px-4 py-2.5 text-left border-l-2 transition-colors ${
                  cam.id === selectedCameraId
                    ? "bg-[#1a1a1a] border-blue-500 text-[#ededed]"
                    : "border-transparent text-[#888] hover:bg-white/[0.03] hover:text-[#ddd]"
                }`}
              >
                <div className="w-8 h-6 rounded bg-[#0a0a0a] border border-[#2a2a2a] shrink-0" />
                <div className="flex-1 min-w-0">
                  <div className="text-[13px] font-medium truncate">
                    {cameraName(cam)}
                  </div>
                  <div
                    className={`text-[10px] flex items-center gap-1 ${
                      cam.status === "online"
                        ? "text-[#4ade80]"
                        : cam.status === "needs_auth"
                          ? "text-amber-400"
                          : cam.status === "asleep"
                            ? "text-blue-400"
                            : "text-red-400"
                    }`}
                  >
                    <span
                      className={`w-1 h-1 rounded-full ${
                        cam.status === "online"
                          ? "bg-[#4ade80]"
                          : cam.status === "needs_auth"
                            ? "bg-amber-400"
                            : cam.status === "asleep"
                              ? "bg-blue-400"
                              : "bg-red-400"
                      }`}
                    />
                    {cam.status === "online"
                      ? "Live"
                      : cam.status === "needs_auth"
                        ? "Needs login"
                        : cam.status === "asleep"
                          ? "Asleep"
                          : "Offline"}
                  </div>
                </div>
              </button>
            ))}
          </div>
        </div>

        {/* Stage */}
        <div className="flex-1 flex flex-col min-w-0">
          {/* Stage head: date pill + presets + toggles */}
          <div className="flex items-center gap-4 px-6 py-3 border-b border-[#1a1a1a] bg-[#0e0e0e]">
            <div className="flex flex-col">
              <span className="text-[18px] font-bold text-[#ededed] leading-tight">
                {formatDate(selectedDate)}
              </span>
              <span className="text-[11px] text-[#777]">
                {selectedDate ? formatLongDate(selectedDate) : "—"}
              </span>
            </div>
            <div className="flex gap-1.5 flex-wrap">
              {(
                [
                  ["today", "Today"],
                  ["yesterday", "Yesterday"],
                  ["last_night", "Last night"],
                  ["this_morning", "This morning"],
                  ["last_12h", "Last 12 hours"],
                  ["last_week", "Last week"],
                ] as Array<[TimelinePreset, string]>
              ).map(([p, label]) => (
                <button
                  key={p}
                  onClick={() => applyPreset(p)}
                  className={
                    preset === p
                      ? "px-3 py-1.5 text-[11px] font-semibold rounded bg-[#2a2a2a] border border-[#444] text-[#ededed]"
                      : "px-3 py-1.5 text-[11px] font-medium rounded border border-[#2a2a2a] text-[#888] hover:text-[#ddd] hover:bg-white/[0.03]"
                  }
                >
                  {label}
                </button>
              ))}
            </div>
            <div className="flex-1" />
          </div>

          {/* Player */}
          <div className="flex-1 relative bg-black min-h-0">
            {timeline === null ? (
              <div className="absolute inset-0 flex items-center justify-center text-[#555] text-sm">
                Loading…
              </div>
            ) : timeline.segments.length === 0 ? (
              <div className="absolute inset-0 flex items-center justify-center text-[#555] text-sm">
                No recordings for{" "}
                {selectedCamera ? cameraName(selectedCamera) : "this camera"} on
                this date
              </div>
            ) : null}

            <video
              ref={videoRef}
              className="w-full h-full object-contain"
              onTimeUpdate={handleTimeUpdate}
              onPlay={() => setPlaying(true)}
              onPause={() => setPlaying(false)}
              autoPlay
              muted
              playsInline
            />

            {/* Fatal HLS error banner — takes over the player area when
                hls.js reports a fatal error, with a retry button that
                forces the engine to re-initialize. */}
            {hlsFatalError && (
              <div className="absolute inset-0 flex flex-col items-center justify-center bg-[#0a0a0a]/95 text-center px-6 z-10">
                <svg
                  className="w-6 h-6 text-red-500 mb-3"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth={2}
                  viewBox="0 0 24 24"
                >
                  <circle cx="12" cy="12" r="10" />
                  <line x1="12" y1="8" x2="12" y2="12" />
                  <line x1="12" y1="16" x2="12.01" y2="16" />
                </svg>
                <p className="text-sm font-semibold text-[#ededed] mb-2">
                  Playback problem
                </p>
                <p className="text-[12px] text-[#888] max-w-md leading-relaxed mb-4">
                  {hlsFatalError}
                </p>
                <button
                  onClick={handleHlsRetry}
                  className="px-4 py-2 bg-[#222] border border-[#333] text-[#ddd] text-xs font-semibold rounded hover:bg-[#2a2a2a] transition-colors"
                >
                  Try again
                </button>
              </div>
            )}

            {/* Overlays */}
            {selectedCamera && timeline && timeline.segments.length > 0 && (
              <>
                <div className="absolute top-4 left-5 px-2.5 py-1 bg-black/60 backdrop-blur rounded text-[12px] font-semibold text-white pointer-events-none">
                  {cameraName(selectedCamera)}
                </div>
                <div className="absolute top-4 right-5 flex items-center gap-2 text-[12px] text-white pointer-events-none">
                  <span className="px-2.5 py-1 bg-black/60 backdrop-blur rounded tabular-nums">
                    {formatClock(currentSecond)}
                  </span>
                </div>
                <div className="absolute bottom-4 right-5 flex items-center gap-2 pointer-events-auto">
                  <button
                    onClick={() => {
                      const next = speed >= 8 ? 1 : speed * 2;
                      setSpeed(next);
                    }}
                    className="px-2 py-1 bg-black/60 backdrop-blur text-white text-xs font-semibold rounded hover:bg-black/80"
                  >
                    {speed}×
                  </button>
                </div>
              </>
            )}
          </div>

          {/* Timeline */}
          <div className="bg-[#0e0e0e] border-t border-[#1a1a1a] px-6 py-4 shrink-0">
            <RecordingsTimeline
              segments={visibleSegments}
              motionEvents={motionLike}
              viewStart={viewStart}
              viewEnd={viewEnd}
              currentSecond={currentSecond}
              scale={scale}
              onSeek={handleSeek}
              onScaleChange={handleScaleChange}
              title={selectedCamera ? cameraName(selectedCamera) : ""}
            />
            <div className="mt-3 flex justify-between text-[10.5px] text-[#555]">
              <span>
                Space to play · arrows to scrub · 1 2 4 8 to change speed
              </span>
              <span className="tabular-nums">{speed}× speed</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function formatDate(iso: string): string {
  if (!iso) return "";
  const d = new Date(iso + "T00:00:00");
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const diff = Math.round((today.getTime() - d.getTime()) / 86400000);
  if (diff === 0) return "Today";
  if (diff === 1) return "Yesterday";
  if (diff < 7) return `${diff} days ago`;
  return d.toLocaleDateString();
}

function formatLongDate(iso: string): string {
  const d = new Date(iso + "T00:00:00");
  return d.toLocaleDateString([], {
    weekday: "short",
    month: "short",
    day: "numeric",
  });
}
