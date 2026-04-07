import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  fetchMotionTimeline,
  fetchRecordingDates,
  fetchTimeline,
  recordingFileUrl,
  type MotionTimelineEntry,
  type Timeline as TimelineData,
  type TimelineSegment,
} from "../api/client";
import type { Camera } from "../types";
import { Timeline, formatClock } from "./Timeline";

const DAY_SECONDS = 86400;

export function Playback({
  cameras,
  onBack,
  initialCameraId,
  initialStartedAt,
}: {
  cameras: Camera[];
  onBack: () => void;
  initialCameraId?: string;
  initialStartedAt?: string;
}) {
  const cameraOptions = useMemo(
    () => cameras.filter((c) => c.rtsp_uri),
    [cameras]
  );

  const [selectedCameraId, setSelectedCameraId] = useState<string>(
    initialCameraId || cameraOptions[0]?.id || ""
  );
  const [dates, setDates] = useState<string[]>([]);
  const [selectedDate, setSelectedDate] = useState<string>("");
  const [timeline, setTimeline] = useState<TimelineData | null>(null);
  const [motionEvents, setMotionEvents] = useState<MotionTimelineEntry[]>([]);
  const [currentSecond, setCurrentSecond] = useState(0);
  const [playing, setPlaying] = useState(true);
  const [speed, setSpeed] = useState(1);
  const videoRef = useRef<HTMLVideoElement>(null);

  // Auto-select first camera
  useEffect(() => {
    if (!selectedCameraId && cameraOptions.length > 0) {
      setSelectedCameraId(cameraOptions[0].id);
    }
  }, [cameraOptions, selectedCameraId]);

  // Load dates when camera changes
  useEffect(() => {
    if (!selectedCameraId) return;
    fetchRecordingDates(selectedCameraId).then((d) => {
      setDates(d);
      if (d.length > 0 && !d.includes(selectedDate)) {
        setSelectedDate(d[0]);
      } else if (d.length === 0) {
        setSelectedDate("");
        setTimeline(null);
      }
    });
  }, [selectedCameraId]);

  // Load timeline when camera+date changes
  useEffect(() => {
    if (!selectedCameraId || !selectedDate) return;
    fetchTimeline(selectedCameraId, selectedDate).then((tl) => {
      setTimeline(tl);
      // Jump to last segment by default
      if (tl.segments.length > 0) {
        const last = tl.segments[tl.segments.length - 1];
        setCurrentSecond(last.second_of_day);
      }
    });
    fetchMotionTimeline(selectedCameraId, selectedDate)
      .then(setMotionEvents)
      .catch(() => setMotionEvents([]));
  }, [selectedCameraId, selectedDate]);

  // Jump to motion event start time when arriving from MotionPanel
  const jumpedRef = useRef(false);
  useEffect(() => {
    if (jumpedRef.current) return;
    if (!initialStartedAt || !timeline || timeline.segments.length === 0) return;
    const d = new Date(initialStartedAt);
    const second = d.getHours() * 3600 + d.getMinutes() * 60 + d.getSeconds();
    setCurrentSecond(second);
    jumpedRef.current = true;
  }, [initialStartedAt, timeline]);

  // Find which segment covers a given second
  const segmentAt = useCallback(
    (second: number): TimelineSegment | null => {
      if (!timeline) return null;
      for (const seg of timeline.segments) {
        if (
          second >= seg.second_of_day &&
          second < seg.second_of_day + seg.duration_s
        ) {
          return seg;
        }
      }
      return null;
    },
    [timeline]
  );

  // Find next segment after a given second (for skipping gaps)
  const nextSegmentAfter = useCallback(
    (second: number): TimelineSegment | null => {
      if (!timeline) return null;
      for (const seg of timeline.segments) {
        if (seg.second_of_day > second) return seg;
      }
      return null;
    },
    [timeline]
  );

  const currentSegment = segmentAt(currentSecond);

  // Load the video for the current segment whenever it changes
  useEffect(() => {
    const video = videoRef.current;
    if (!video || !currentSegment) return;

    if (video.src.endsWith(currentSegment.id + "/file")) return;

    let cancelled = false;
    const offset = currentSecond - currentSegment.second_of_day;
    recordingFileUrl(currentSegment.id).then((expectedSrc) => {
      if (cancelled || !videoRef.current) return;
      const v = videoRef.current;
      v.src = expectedSrc;
      const onLoaded = () => {
        v.currentTime = Math.max(0, offset);
        if (playing) v.play().catch(() => {});
      };
      v.addEventListener("loadedmetadata", onLoaded, { once: true });
    });
    return () => {
      cancelled = true;
    };
  }, [currentSegment?.id]);

  // Sync playhead from video time as it plays
  const handleTimeUpdate = useCallback(() => {
    const video = videoRef.current;
    if (!video || !currentSegment) return;
    setCurrentSecond(currentSegment.second_of_day + video.currentTime);
  }, [currentSegment]);

  // Auto-advance to next segment when current ends
  const handleEnded = useCallback(() => {
    if (!currentSegment) return;
    const next = nextSegmentAfter(currentSegment.second_of_day);
    if (next) {
      setCurrentSecond(next.second_of_day);
    } else {
      setPlaying(false);
    }
  }, [currentSegment, nextSegmentAfter]);

  // Click on timeline → seek
  const handleSeek = useCallback(
    (second: number) => {
      const seg = segmentAt(second);
      if (seg) {
        setCurrentSecond(second);
        const video = videoRef.current;
        if (video) {
          if (video.src.endsWith(seg.id + "/file")) {
            video.currentTime = second - seg.second_of_day;
          }
          // else useEffect will load the new segment
        }
      } else {
        // Click was on a gap — jump to next available segment
        const next = nextSegmentAfter(second);
        if (next) setCurrentSecond(next.second_of_day);
      }
    },
    [segmentAt, nextSegmentAfter]
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

  // Jump to live (last available second)
  const jumpToLive = useCallback(() => {
    if (!timeline || timeline.segments.length === 0) return;
    const last = timeline.segments[timeline.segments.length - 1];
    setCurrentSecond(last.second_of_day + last.duration_s - 2);
  }, [timeline]);

  // Keyboard shortcuts
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement) return;
      if (e.target instanceof HTMLSelectElement) return;
      if (e.target instanceof HTMLTextAreaElement) return;

      switch (e.key) {
        case " ":
          e.preventDefault();
          togglePlay();
          break;
        case "ArrowRight":
          e.preventDefault();
          handleSeek(Math.min(DAY_SECONDS - 1, currentSecond + (e.shiftKey ? 60 : 10)));
          break;
        case "ArrowLeft":
          e.preventDefault();
          handleSeek(Math.max(0, currentSecond - (e.shiftKey ? 60 : 10)));
          break;
        case "Home":
          if (timeline?.segments[0])
            setCurrentSecond(timeline.segments[0].second_of_day);
          break;
        case "End":
          jumpToLive();
          break;
        case "j":
        case "J":
          setSpeed((s) => Math.max(0.25, s / 2));
          break;
        case "l":
        case "L":
          setSpeed((s) => Math.min(8, s * 2));
          break;
        case "k":
        case "K":
          togglePlay();
          break;
      }

      // Number keys 1-9 → switch camera
      const num = parseInt(e.key);
      if (!isNaN(num) && num >= 1 && num <= cameraOptions.length) {
        setSelectedCameraId(cameraOptions[num - 1].id);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [togglePlay, handleSeek, currentSecond, jumpToLive, timeline, cameraOptions]);

  const cameraName = (cam: Camera) =>
    cam.name ||
    [cam.manufacturer, cam.model].filter(Boolean).join(" ") ||
    cam.ip;

  const selectedCamera = cameras.find((c) => c.id === selectedCameraId);

  return (
    <div className="flex-1 flex flex-col bg-black">
      {/* Topbar */}
      <div className="flex items-center justify-between px-5 h-12 bg-[#1a1a1a] border-b border-[#333] shrink-0">
        <div className="flex items-center gap-3">
          <button
            onClick={onBack}
            className="text-[#888] hover:text-[#ddd] text-sm"
          >
            ← Live
          </button>
          <span className="text-[#ddd] font-bold text-[15px]">Recordings</span>
        </div>

        {/* Camera tabs */}
        <div className="flex gap-1 overflow-x-auto">
          {cameraOptions.map((cam, i) => (
            <button
              key={cam.id}
              onClick={() => setSelectedCameraId(cam.id)}
              className={`px-3 py-1 rounded text-xs font-medium border whitespace-nowrap transition-colors ${
                cam.id === selectedCameraId
                  ? "bg-blue-500 border-blue-500 text-white"
                  : "bg-[#222] border-[#333] text-[#888] hover:bg-[#2a2a2a]"
              }`}
              title={`Press ${i + 1}`}
            >
              {cameraName(cam)}
            </button>
          ))}
        </div>

        <div className="flex items-center gap-2">
          {dates.length > 0 && (
            <select
              value={selectedDate}
              onChange={(e) => setSelectedDate(e.target.value)}
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

      {/* Video area */}
      <div className="flex-1 relative bg-black min-h-0">
        {timeline === null ? (
          <div className="absolute inset-0 flex items-center justify-center text-[#555] text-sm">
            Loading...
          </div>
        ) : timeline.segments.length === 0 ? (
          <div className="absolute inset-0 flex items-center justify-center text-[#555] text-sm">
            No recordings for {selectedCamera ? cameraName(selectedCamera) : "this camera"} on this date
          </div>
        ) : !currentSegment ? (
          <div className="absolute inset-0 flex items-center justify-center text-[#555] text-sm">
            Gap in recording — click a blue region on the timeline
          </div>
        ) : null}

        <video
          ref={videoRef}
          className="w-full h-full object-contain"
          onTimeUpdate={handleTimeUpdate}
          onEnded={handleEnded}
          autoPlay
          muted
          playsInline
        />

        {/* Overlay info */}
        {currentSegment && (
          <div className="absolute top-4 left-5 right-5 flex justify-between items-start pointer-events-none">
            <div className="flex flex-col gap-1">
              <div className="text-2xl font-bold text-white tabular-nums drop-shadow-lg">
                {formatClock(currentSecond)}
              </div>
              <div className="text-xs text-white/70 drop-shadow">
                {selectedCamera && cameraName(selectedCamera)} · {formatDate(selectedDate)}
              </div>
            </div>
            <div className="flex items-center gap-2 pointer-events-auto">
              <button
                onClick={() => setSpeed((s) => (s >= 8 ? 0.5 : s * 2))}
                className="px-2 py-1 bg-black/50 backdrop-blur text-white text-xs font-semibold rounded hover:bg-black/70"
              >
                {speed}x
              </button>
              <button
                onClick={jumpToLive}
                className="px-2 py-1 bg-red-600 text-white text-xs font-semibold rounded hover:bg-red-500"
              >
                LIVE
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Timeline ruler */}
      <div className="bg-[#1a1a1a] border-t border-[#333] px-5 py-3 shrink-0">
        {timeline && timeline.segments.length > 0 ? (
          <Timeline
            segments={timeline.segments}
            currentSecond={currentSecond}
            onSeek={handleSeek}
            motionEvents={motionEvents}
          />
        ) : (
          <div className="h-12 flex items-center text-xs text-[#555]">
            {timeline ? "No recordings on this date" : "Loading timeline..."}
          </div>
        )}
        <div className="mt-1.5 text-[10px] text-[#555] flex justify-between">
          <span>Space: play/pause · ← →: seek 10s · Shift+arrows: 1m · 1-{cameraOptions.length}: cameras · J/L: speed</span>
          <span className="tabular-nums">
            {timeline?.segments.length ?? 0} segments
          </span>
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
