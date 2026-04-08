import { useMemo, useRef } from "react";
import type { TimelineSegment } from "../api/client";
import {
  bucketMotionEvents,
  computeGapBands,
  formatClock,
  formatClockShort,
  type GapBand,
  type MotionEventLike,
  type TimelineScale,
} from "../lib/timelineMath";

interface RecordingsTimelineProps {
  segments: TimelineSegment[];
  motionEvents: MotionEventLike[];
  /** Window start, second-of-day (inclusive). */
  viewStart: number;
  /** Window end, second-of-day (exclusive). */
  viewEnd: number;
  /** Current playhead, second-of-day. */
  currentSecond: number;
  scale: TimelineScale;
  onSeek: (second: number) => void;
  onScaleChange: (scale: TimelineScale) => void;
  title: string;
}

const BUCKETS = 100;

export function RecordingsTimeline({
  segments,
  motionEvents,
  viewStart,
  viewEnd,
  currentSecond,
  scale,
  onSeek,
  onScaleChange,
  title,
}: RecordingsTimelineProps) {
  const trackRef = useRef<HTMLDivElement>(null);

  const span = Math.max(1, viewEnd - viewStart);

  const gaps: GapBand[] = useMemo(
    () => computeGapBands(segments, viewStart, viewEnd, 10),
    [segments, viewStart, viewEnd],
  );

  const motionBuckets = useMemo(
    () => bucketMotionEvents(motionEvents, viewStart, viewEnd, BUCKETS),
    [motionEvents, viewStart, viewEnd],
  );

  const toPct = (second: number): number =>
    ((second - viewStart) / span) * 100;

  const ticks = useMemo(() => buildTicks(viewStart, viewEnd), [viewStart, viewEnd]);

  const handleClick = (e: React.MouseEvent) => {
    const rect = trackRef.current?.getBoundingClientRect();
    if (!rect) return;
    const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    onSeek(Math.round(viewStart + ratio * span));
  };

  const handleGapClick = (e: React.MouseEvent, gap: GapBand) => {
    e.stopPropagation();
    // Seek to the end of the gap — the next available second.
    onSeek(gap.end);
  };

  return (
    <div className="flex flex-col gap-2 select-none">
      {/* Head: title + scale buttons */}
      <div className="flex items-center justify-between">
        <span className="text-[11px] text-[#888] font-medium tracking-wide uppercase">
          {title}
        </span>
        <div className="flex gap-1">
          {(["1h", "6h", "24h", "7d"] as TimelineScale[]).map((s) => (
            <button
              key={s}
              onClick={() => onScaleChange(s)}
              className={
                scale === s
                  ? "px-2.5 py-1 text-[11px] font-semibold rounded bg-[#2a2a2a] border border-[#444] text-[#ededed]"
                  : "px-2.5 py-1 text-[11px] font-medium rounded border border-[#2a2a2a] text-[#888] hover:text-[#ddd] hover:bg-white/[0.03]"
              }
            >
              {s}
            </button>
          ))}
        </div>
      </div>

      {/* Track */}
      <div
        ref={trackRef}
        onClick={handleClick}
        className="relative h-14 bg-[#0a0a0a] border border-[#222] rounded cursor-pointer overflow-hidden"
      >
        {/* Recorded strip (muted blue) */}
        {segments.map((seg) => {
          const segStart = Math.max(viewStart, seg.second_of_day);
          const segEnd = Math.min(viewEnd, seg.second_of_day + seg.duration_s);
          if (segEnd <= segStart) return null;
          const left = toPct(segStart);
          const width = Math.max(((segEnd - segStart) / span) * 100, 0.08);
          return (
            <div
              key={seg.id}
              className="absolute top-[40%] bottom-[12%] bg-blue-500/25 border-l border-r border-blue-500/50"
              style={{ left: `${left}%`, width: `${width}%` }}
            />
          );
        })}

        {/* Motion density blobs (amber) */}
        {motionBuckets.map((b) => {
          const left = (b.index / BUCKETS) * 100;
          const width = 100 / BUCKETS;
          const opacity = Math.min(1, b.count * 0.25);
          return (
            <div
              key={b.index}
              className="absolute top-[18%] bottom-[40%] bg-amber-400 pointer-events-none"
              style={{
                left: `${left}%`,
                width: `${width}%`,
                opacity,
              }}
              title={`${b.count} motion event${b.count === 1 ? "" : "s"}`}
            />
          );
        })}

        {/* Gap bands (red-hatched) */}
        {gaps.map((gap, i) => {
          const left = toPct(gap.start);
          const width = Math.max(((gap.end - gap.start) / span) * 100, 0.3);
          return (
            <div
              key={i}
              onClick={(e) => handleGapClick(e, gap)}
              className="absolute top-[40%] bottom-[12%] cursor-pointer"
              style={{
                left: `${left}%`,
                width: `${width}%`,
                background:
                  "repeating-linear-gradient(45deg, rgba(239,68,68,0.35) 0 4px, rgba(239,68,68,0.12) 4px 8px)",
                borderLeft: "1px solid rgba(239,68,68,0.55)",
                borderRight: "1px solid rgba(239,68,68,0.55)",
              }}
              title="Recording gap — click to skip"
            />
          );
        })}

        {/* Ticks */}
        {ticks.map((t) => (
          <div
            key={t.second}
            className="absolute top-[40%] bottom-0 border-l border-[#2a2a2a] pointer-events-none"
            style={{ left: `${toPct(t.second)}%` }}
          >
            <div className="absolute bottom-0 left-1 text-[9.5px] text-[#555] tabular-nums whitespace-nowrap">
              {t.label}
            </div>
          </div>
        ))}

        {/* Playhead */}
        {currentSecond >= viewStart && currentSecond <= viewEnd && (
          <div
            className="absolute top-0 bottom-0 w-[2px] bg-white pointer-events-none"
            style={{ left: `${toPct(currentSecond)}%` }}
          >
            <div className="absolute top-0 left-[-3px] w-2 h-2 bg-white rounded-full" />
            <div className="absolute top-[-18px] left-[-26px] w-[56px] text-center text-[10px] text-white font-semibold tabular-nums bg-black/75 rounded px-1 py-[1px]">
              {formatClock(currentSecond)}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

interface Tick {
  second: number;
  label: string;
}

/**
 * Build axis ticks for a given window. Uses a human-friendly step
 * derived from the span length.
 */
function buildTicks(start: number, end: number): Tick[] {
  const span = end - start;
  let step: number;
  if (span <= 3600) step = 300; // 5 min
  else if (span <= 6 * 3600) step = 1800; // 30 min
  else if (span <= 12 * 3600) step = 3600; // 1h
  else step = 6 * 3600; // 6h

  const ticks: Tick[] = [];
  const first = Math.ceil(start / step) * step;
  for (let s = first; s < end; s += step) {
    ticks.push({ second: s, label: formatClockShort(s) });
  }
  return ticks;
}
