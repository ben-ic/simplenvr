import { useMemo, useRef } from "react";
import type { MotionTimelineEntry, TimelineSegment } from "../api/client";
import {
  bucketMotionEvents,
  computeGapBands,
  formatClock,
  formatClockShort,
  type GapBand,
  type TimelineScale,
} from "../lib/timelineMath";

export interface MultiTimelineRow {
  cameraId: string;
  name: string;
  segments: TimelineSegment[];
  motionEvents: MotionTimelineEntry[];
}

interface MultiTimelineProps {
  rows: MultiTimelineRow[];
  viewStart: number;
  viewEnd: number;
  currentSecond: number;
  scale: TimelineScale;
  onSeek: (second: number) => void;
  onScaleChange: (scale: TimelineScale) => void;
}

const BUCKETS = 100;

/**
 * Per-camera stacked timeline for the synced multi-cam grid. Each row
 * shares the same x-axis; a single playhead line spans all rows so
 * "this is the same instant across all cameras" is unambiguous.
 *
 * Deliberately thinner rows (h-6) than RecordingsTimeline (h-14) — with
 * up to 16 cameras in play, vertical budget is the constraint.
 */
export function MultiTimeline({
  rows,
  viewStart,
  viewEnd,
  currentSecond,
  scale,
  onSeek,
  onScaleChange,
}: MultiTimelineProps) {
  const trackRef = useRef<HTMLDivElement>(null);
  const span = Math.max(1, viewEnd - viewStart);

  const ticks = useMemo(
    () => buildTicks(viewStart, viewEnd),
    [viewStart, viewEnd],
  );

  const toPct = (second: number) => ((second - viewStart) / span) * 100;

  const handleClick = (e: React.MouseEvent) => {
    const rect = trackRef.current?.getBoundingClientRect();
    if (!rect) return;
    const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    onSeek(Math.round(viewStart + ratio * span));
  };

  return (
    <div className="flex flex-col gap-2 select-none">
      {/* Head: title + scale buttons. 7d hidden — grid view is single-day. */}
      <div className="flex items-center justify-between">
        <span className="text-[11px] text-[#888] font-medium tracking-wide uppercase">
          {rows.length} cameras · synced
        </span>
        <div className="flex gap-1">
          {(["1h", "6h", "24h"] as TimelineScale[]).map((s) => (
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

      {/* The whole stacked track block. One wrapper handles clicks +
          hosts the single cross-row playhead line. */}
      <div
        ref={trackRef}
        onClick={handleClick}
        className="relative bg-[#0a0a0a] border border-[#222] rounded cursor-pointer overflow-hidden"
      >
        {/* Stacked rows */}
        <div className="flex flex-col">
          {rows.map((row) => (
            <Row
              key={row.cameraId}
              row={row}
              viewStart={viewStart}
              viewEnd={viewEnd}
              span={span}
              toPct={toPct}
            />
          ))}
        </div>

        {/* Tick labels along the bottom */}
        <div className="relative h-4 bg-[#0a0a0a] border-t border-[#1a1a1a]">
          {ticks.map((t) => (
            <div
              key={t.second}
              className="absolute top-0 bottom-0 border-l border-[#2a2a2a] pointer-events-none"
              style={{ left: `${toPct(t.second)}%` }}
            >
              <div className="absolute bottom-0 left-1 text-[9.5px] text-[#555] tabular-nums whitespace-nowrap">
                {t.label}
              </div>
            </div>
          ))}
        </div>

        {/* Shared playhead — single vertical line across ALL rows. */}
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

interface RowProps {
  row: MultiTimelineRow;
  viewStart: number;
  viewEnd: number;
  span: number;
  toPct: (second: number) => number;
}

function Row({ row, viewStart, viewEnd, span, toPct }: RowProps) {
  const gaps: GapBand[] = useMemo(
    () => computeGapBands(row.segments, viewStart, viewEnd, 10),
    [row.segments, viewStart, viewEnd],
  );
  const buckets = useMemo(
    () =>
      bucketMotionEvents(
        row.motionEvents.map((m) => ({
          second_of_day: m.second_of_day,
          duration_s: m.duration_s,
        })),
        viewStart,
        viewEnd,
        BUCKETS,
      ),
    [row.motionEvents, viewStart, viewEnd],
  );

  return (
    <div className="relative h-6 border-b border-[#141414] last:border-b-0">
      {/* Camera name label, left-inset, low-contrast */}
      <div className="absolute top-0 bottom-0 left-1.5 flex items-center text-[9px] text-[#666] font-medium pointer-events-none z-10 max-w-[120px] truncate">
        {row.name}
      </div>

      {/* Recorded strip (muted blue) */}
      {row.segments.map((seg) => {
        const segStart = Math.max(viewStart, seg.second_of_day);
        const segEnd = Math.min(viewEnd, seg.second_of_day + seg.duration_s);
        if (segEnd <= segStart) return null;
        const left = toPct(segStart);
        const width = Math.max(((segEnd - segStart) / span) * 100, 0.08);
        return (
          <div
            key={seg.id}
            className="absolute top-1 bottom-1 bg-blue-500/25 border-l border-r border-blue-500/50 pointer-events-none"
            style={{ left: `${left}%`, width: `${width}%` }}
          />
        );
      })}

      {/* Motion density blobs (amber) */}
      {buckets.map((b) => {
        const left = (b.index / BUCKETS) * 100;
        const width = 100 / BUCKETS;
        const opacity = Math.min(1, b.count * 0.25);
        return (
          <div
            key={b.index}
            className="absolute top-0 h-1 bg-amber-400 pointer-events-none"
            style={{ left: `${left}%`, width: `${width}%`, opacity }}
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
            className="absolute top-1 bottom-1 pointer-events-none"
            style={{
              left: `${left}%`,
              width: `${width}%`,
              background:
                "repeating-linear-gradient(45deg, rgba(239,68,68,0.35) 0 4px, rgba(239,68,68,0.12) 4px 8px)",
            }}
          />
        );
      })}
    </div>
  );
}

interface Tick {
  second: number;
  label: string;
}

function buildTicks(start: number, end: number): Tick[] {
  const span = end - start;
  let step: number;
  if (span <= 3600) step = 300;
  else if (span <= 6 * 3600) step = 1800;
  else if (span <= 12 * 3600) step = 3600;
  else step = 6 * 3600;
  const ticks: Tick[] = [];
  const first = Math.ceil(start / step) * step;
  for (let s = first; s < end; s += step) {
    ticks.push({ second: s, label: formatClockShort(s) });
  }
  return ticks;
}
