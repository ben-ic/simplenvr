import Hls from "hls.js";
import { useEffect, useMemo, useRef, useState } from "react";
import { fetchTimeline, type Timeline as TimelineData } from "../api/client";
import { apiUrl } from "../lib/backend";
import { secondOfDayToPlaylistTime } from "../lib/timelineMath";
import type { Camera } from "../types";

interface GridTileProps {
  camera: Camera;
  date: string;
  /** Shared wall-clock second-of-day. Drives this tile's video.currentTime. */
  currentSecond: number;
  /** Whether the shared clock is running. */
  playing: boolean;
  /** Shared playback rate. */
  speed: number;
  /** Fires when the parent needs this tile's segments for the MultiTimeline. */
  onTimelineLoaded: (cameraId: string, timeline: TimelineData) => void;
  /** Click to promote this tile to single-cam focus. */
  onFocus?: () => void;
}

/**
 * A single tile in the synced multi-camera grid. Owns:
 *  - its own `<video>` + hls.js instance
 *  - its own per-camera, per-day timeline fetch
 *  - a "no footage at the shared playhead" overlay
 *
 * It does NOT drive the shared clock — the parent owns that. The tile is
 * a pure follower: when `currentSecond` changes, it seeks; when `playing`
 * flips, it plays/pauses; when `speed` changes, it retargets playbackRate.
 */
export function GridTile({
  camera,
  date,
  currentSecond,
  playing,
  speed,
  onTimelineLoaded,
  onFocus,
}: GridTileProps) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const hlsRef = useRef<Hls | null>(null);
  const [timeline, setTimeline] = useState<TimelineData | null>(null);
  const [ready, setReady] = useState(false);

  // Fetch this camera's timeline for the selected date.
  useEffect(() => {
    if (!date) return;
    let cancelled = false;
    setTimeline(null);
    setReady(false);
    fetchTimeline(camera.id, date)
      .then((tl) => {
        if (cancelled) return;
        setTimeline(tl);
        onTimelineLoaded(camera.id, tl);
      })
      .catch(() => {
        if (cancelled) return;
        setTimeline({
          date,
          camera_id: camera.id,
          segments: [],
          total_duration_s: 0,
        });
      });
    return () => {
      cancelled = true;
    };
    // onTimelineLoaded intentionally excluded — identity changes on every
    // parent render and we only need to fire once per camera/date load.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [camera.id, date]);

  // Boot hls.js once we have segments to point at.
  useEffect(() => {
    const video = videoRef.current;
    if (!video || !timeline || timeline.segments.length === 0) return;

    let cancelled = false;
    (async () => {
      const playlistUrl = await apiUrl(
        `/api/recordings/hls/index.m3u8?camera_id=${encodeURIComponent(
          camera.id,
        )}&date=${encodeURIComponent(date)}`,
      );
      if (cancelled) return;

      if (hlsRef.current) {
        hlsRef.current.destroy();
        hlsRef.current = null;
      }

      const initialPlaylistTime = secondOfDayToPlaylistTime(
        timeline.segments,
        currentSecond,
      );

      if (Hls.isSupported()) {
        const hls = new Hls({ maxBufferLength: 30, backBufferLength: 10 });
        hlsRef.current = hls;
        hls.loadSource(playlistUrl);
        hls.attachMedia(video);
        hls.on(Hls.Events.MANIFEST_PARSED, () => {
          if (cancelled) return;
          video.currentTime = initialPlaylistTime;
          setReady(true);
          if (playing) video.play().catch(() => {});
        });
      } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
        video.src = playlistUrl;
        video.addEventListener(
          "loadedmetadata",
          () => {
            video.currentTime = initialPlaylistTime;
            setReady(true);
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
    // Re-init only on camera/date/segment-count changes — the same
    // guardrail Recordings.tsx uses for its single-cam engine.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [camera.id, date, timeline?.segments.length]);

  // Does the shared playhead land inside one of this camera's segments?
  const hasFootageNow = useMemo(() => {
    if (!timeline) return false;
    return timeline.segments.some(
      (s) =>
        currentSecond >= s.second_of_day &&
        currentSecond < s.second_of_day + s.duration_s,
    );
  }, [timeline, currentSecond]);

  // Drift correction: keep the tile's <video>.currentTime aligned with
  // the shared clock. Threshold scales with playback speed — at 1×
  // you're studying a moment and tight sync matters; at 8× you're
  // scanning and the engine-seek cost dominates, so we let tiles run
  // freer. Values approved by Ben 2026-04-09.
  const driftThreshold = 0.5 * speed;
  useEffect(() => {
    const video = videoRef.current;
    if (!video || !ready || !timeline) return;
    if (!hasFootageNow) {
      // Nothing to play; pause so the tile doesn't silently advance past
      // its own gaps while other tiles have footage.
      if (!video.paused) video.pause();
      return;
    }
    const target = secondOfDayToPlaylistTime(
      timeline.segments,
      currentSecond,
    );
    const drift = Math.abs(video.currentTime - target);
    if (drift > driftThreshold) {
      video.currentTime = target;
    }
  }, [currentSecond, hasFootageNow, ready, timeline, driftThreshold]);

  // Speed-change hard resync: bypass the drift threshold entirely when
  // `speed` flips, otherwise switching 1×→8× lets every tile accumulate
  // a full second of drift before the new (wider) threshold trips.
  // Separate from the drift effect so it only fires on actual speed
  // transitions, not on every currentSecond tick.
  useEffect(() => {
    const video = videoRef.current;
    if (!video || !ready || !timeline || !hasFootageNow) return;
    video.currentTime = secondOfDayToPlaylistTime(
      timeline.segments,
      currentSecond,
    );
    // currentSecond intentionally excluded — only resync on speed change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [speed]);

  // Play/pause follow.
  useEffect(() => {
    const video = videoRef.current;
    if (!video || !ready) return;
    if (playing && hasFootageNow) {
      video.play().catch(() => {});
    } else {
      video.pause();
    }
  }, [playing, hasFootageNow, ready]);

  // Speed follow.
  useEffect(() => {
    const video = videoRef.current;
    if (video) video.playbackRate = speed;
  }, [speed]);

  const camLabel = camera.name || camera.ip;
  const noFootage = timeline !== null && !hasFootageNow;
  const empty = timeline !== null && timeline.segments.length === 0;

  return (
    <div
      className="relative bg-black overflow-hidden cursor-pointer group"
      onDoubleClick={onFocus}
      title="Double-click to focus this camera"
    >
      <video
        ref={videoRef}
        className="w-full h-full object-contain"
        muted
        playsInline
      />

      {/* No-footage overlay — reuses the red-hatched gap-band visual
          language from RecordingsTimeline so "no footage here" reads
          the same in the player and in the timeline. */}
      {(noFootage || empty) && (
        <div
          className="absolute inset-0 flex items-center justify-center pointer-events-none"
          style={{
            background:
              "repeating-linear-gradient(45deg, rgba(239,68,68,0.18) 0 8px, rgba(10,10,10,0.92) 8px 16px)",
          }}
        >
          <span className="text-[11px] font-semibold text-white/85 bg-black/60 backdrop-blur px-2 py-1 rounded">
            {empty ? "No footage today" : "No footage at this time"}
          </span>
        </div>
      )}

      {/* Loading state — tile has no timeline yet. */}
      {timeline === null && (
        <div className="absolute inset-0 flex items-center justify-center text-[10px] text-[#555]">
          Loading…
        </div>
      )}

      {/* Camera label — small because N of these tile up. */}
      <div className="absolute top-1.5 left-1.5 px-1.5 py-[2px] bg-black/60 backdrop-blur rounded text-[10px] font-semibold text-white pointer-events-none">
        {camLabel}
      </div>
    </div>
  );
}
