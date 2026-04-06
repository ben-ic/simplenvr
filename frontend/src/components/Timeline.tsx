import { useRef } from "react";
import type { TimelineSegment } from "../api/client";

interface MotionMark {
  second_of_day: number;
  duration_s: number;
}

interface TimelineProps {
  segments: TimelineSegment[];
  currentSecond: number; // 0..86400
  onSeek: (second: number) => void;
  motionEvents?: MotionMark[];
}

const DAY_SECONDS = 86400;

export function Timeline({
  segments,
  currentSecond,
  onSeek,
  motionEvents = [],
}: TimelineProps) {
  const containerRef = useRef<HTMLDivElement>(null);

  const handleClick = (e: React.MouseEvent) => {
    const rect = containerRef.current?.getBoundingClientRect();
    if (!rect) return;
    const ratio = (e.clientX - rect.left) / rect.width;
    const second = Math.round(Math.max(0, Math.min(DAY_SECONDS, ratio * DAY_SECONDS)));
    onSeek(second);
  };

  return (
    <div className="flex flex-col gap-1.5 select-none">
      {/* Track */}
      <div
        ref={containerRef}
        onClick={handleClick}
        className="relative h-8 bg-[#0a0a0a] border border-[#222] rounded cursor-pointer overflow-hidden"
      >
        {/* Recording segments */}
        {segments.map((seg) => {
          const left = (seg.second_of_day / DAY_SECONDS) * 100;
          const width = Math.max(
            (seg.duration_s / DAY_SECONDS) * 100,
            0.05
          );
          return (
            <div
              key={seg.id}
              className="absolute top-0 bottom-0 bg-blue-500/30 border-l border-r border-blue-500/40"
              style={{ left: `${left}%`, width: `${width}%` }}
            />
          );
        })}

        {/* Hour grid lines (every 3h, slightly brighter) */}
        {Array.from({ length: 8 }, (_, i) => i * 3).map((h) => (
          <div
            key={h}
            className="absolute top-0 bottom-0 border-l border-[#333]"
            style={{ left: `${(h / 24) * 100}%` }}
          />
        ))}

        {/* Minor hour ticks (every hour) */}
        {Array.from({ length: 24 }, (_, h) => h).map((h) =>
          h % 3 === 0 ? null : (
            <div
              key={h}
              className="absolute bottom-0 h-1.5 border-l border-[#222]"
              style={{ left: `${(h / 24) * 100}%` }}
            />
          )
        )}

        {/* Motion marks (red, bottom strip — visible without obscuring blue regions) */}
        {motionEvents.map((m, i) => {
          const left = (m.second_of_day / DAY_SECONDS) * 100;
          const width = Math.max(
            (m.duration_s / DAY_SECONDS) * 100,
            0.15
          );
          return (
            <div
              key={i}
              className="absolute bottom-0 h-1.5 bg-red-500"
              style={{ left: `${left}%`, width: `${width}%` }}
              title="Motion event"
            />
          );
        })}

        {/* Playhead */}
        <div
          className="absolute top-[-2px] bottom-[-2px] w-[2px] bg-white pointer-events-none"
          style={{ left: `${(currentSecond / DAY_SECONDS) * 100}%` }}
        >
          <div className="absolute top-[-3px] left-[-3px] w-2 h-2 bg-white rounded-full" />
        </div>
      </div>

      {/* Hour labels */}
      <div className="relative h-3">
        {Array.from({ length: 9 }, (_, i) => i * 3).map((h) => (
          <span
            key={h}
            className="absolute text-[10px] text-[#555] tabular-nums"
            style={{
              left: `${(h / 24) * 100}%`,
              transform: h === 0 ? "none" : h === 24 ? "translateX(-100%)" : "translateX(-50%)",
            }}
          >
            {String(h).padStart(2, "0")}
          </span>
        ))}
      </div>
    </div>
  );
}

export function formatClock(secondOfDay: number): string {
  const h = Math.floor(secondOfDay / 3600);
  const m = Math.floor((secondOfDay % 3600) / 60);
  const s = Math.floor(secondOfDay % 60);
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(
    s
  ).padStart(2, "0")}`;
}
