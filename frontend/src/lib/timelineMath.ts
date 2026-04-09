// Pure helpers for the Browse-footage timeline. No React, no imports
// from app code. Each function is unit-testable in isolation.

export interface TimelineSegmentLike {
  second_of_day: number;
  duration_s: number;
}

export interface MotionEventLike {
  second_of_day: number;
  duration_s: number;
}

export interface GapBand {
  /** Start second (inclusive). */
  start: number;
  /** End second (exclusive). */
  end: number;
}

export interface MotionBucket {
  /** Bucket index (0..bucketCount-1). */
  index: number;
  /** Count of motion events falling in this bucket. */
  count: number;
}

export interface TimeWindow {
  /** ISO date YYYY-MM-DD the window is anchored to. */
  date: string;
  /** Start second-of-day (inclusive). */
  startSecond: number;
  /** End second-of-day (exclusive). */
  endSecond: number;
  /** Suggested scale preset. */
  scale: TimelineScale;
}

export type TimelineScale = "1h" | "6h" | "24h" | "7d";

export type TimelinePreset =
  | "today"
  | "yesterday"
  | "last_night"
  | "this_morning"
  | "last_12h"
  | "last_week"
  | "custom";

const DAY_SECONDS = 86400;

/**
 * Compute gap bands: windows ≥ minGapSec inside [start, end) with no
 * segment overlap. Segments do not need to be pre-sorted.
 */
export function computeGapBands(
  segments: TimelineSegmentLike[],
  start: number,
  end: number,
  minGapSec = 10,
): GapBand[] {
  if (end <= start) return [];

  // Clip each segment to the [start, end) window, drop empties, sort.
  const clipped = segments
    .map((s) => ({
      a: Math.max(start, s.second_of_day),
      b: Math.min(end, s.second_of_day + s.duration_s),
    }))
    .filter((s) => s.b > s.a)
    .sort((x, y) => x.a - y.a);

  // Merge overlapping clipped ranges.
  const merged: Array<{ a: number; b: number }> = [];
  for (const c of clipped) {
    const last = merged[merged.length - 1];
    if (last && c.a <= last.b) {
      last.b = Math.max(last.b, c.b);
    } else {
      merged.push({ ...c });
    }
  }

  // Gaps = complement of merged inside [start, end).
  const gaps: GapBand[] = [];
  let cursor = start;
  for (const m of merged) {
    if (m.a - cursor >= minGapSec) {
      gaps.push({ start: cursor, end: m.a });
    }
    cursor = Math.max(cursor, m.b);
  }
  if (end - cursor >= minGapSec) {
    gaps.push({ start: cursor, end });
  }
  return gaps;
}

/**
 * Convert a second-of-day clock position into the corresponding HLS
 * playlist time, in seconds, for a chronologically-sorted segment list.
 *
 * The HLS VOD playlist concatenates segment durations end-to-end with a
 * #EXT-X-DISCONTINUITY between each. So playlist time at a given clock
 * second is `sum(durations of completed segments before it) + offset
 * into the segment containing it`. Clock seconds that fall in gaps
 * (between segments) clamp to the start of the next segment, matching
 * what the gap-band click handler does.
 *
 * Segments MUST be sorted by `second_of_day` ascending. (The backend's
 * db.get_recordings_for_date already sorts by started_at ASC.)
 */
export function secondOfDayToPlaylistTime(
  segments: TimelineSegmentLike[],
  secondOfDay: number,
): number {
  let acc = 0;
  for (const s of segments) {
    if (secondOfDay < s.second_of_day) {
      // Target lands in a gap before this segment — clamp to the
      // beginning of this segment in playlist time.
      return acc;
    }
    if (secondOfDay < s.second_of_day + s.duration_s) {
      return acc + (secondOfDay - s.second_of_day);
    }
    acc += s.duration_s;
  }
  // Past the end of the last segment — clamp to playlist end.
  return acc;
}

/**
 * Inverse of secondOfDayToPlaylistTime. Given a playlist position
 * reported by `<video>.currentTime`, return the wall-clock second of
 * day it corresponds to. Used to drive the timeline playhead from
 * playback progress.
 */
export function playlistTimeToSecondOfDay(
  segments: TimelineSegmentLike[],
  playlistTime: number,
): number {
  if (segments.length === 0) return 0;
  let acc = 0;
  for (const s of segments) {
    if (playlistTime < acc + s.duration_s) {
      return s.second_of_day + Math.max(0, playlistTime - acc);
    }
    acc += s.duration_s;
  }
  // Past the playlist end — pin to the last segment's tail.
  const last = segments[segments.length - 1];
  return last.second_of_day + last.duration_s;
}

/**
 * Bucket motion events across [start, end) into `bucketCount` evenly-
 * spaced bins. Each event contributes 1 to every bucket its range
 * overlaps (min 1). Returns only non-empty buckets.
 */
export function bucketMotionEvents(
  events: MotionEventLike[],
  start: number,
  end: number,
  bucketCount: number,
): MotionBucket[] {
  if (bucketCount <= 0 || end <= start) return [];
  const counts = new Array<number>(bucketCount).fill(0);
  const span = end - start;
  const bucketSize = span / bucketCount;

  for (const ev of events) {
    const a = Math.max(start, ev.second_of_day);
    const b = Math.min(end, ev.second_of_day + Math.max(1, ev.duration_s));
    if (b <= a) continue;
    const firstBucket = Math.max(
      0,
      Math.min(bucketCount - 1, Math.floor((a - start) / bucketSize)),
    );
    const lastBucket = Math.max(
      0,
      Math.min(bucketCount - 1, Math.floor((b - start - 1) / bucketSize)),
    );
    for (let i = firstBucket; i <= lastBucket; i++) {
      counts[i]++;
    }
  }

  const out: MotionBucket[] = [];
  for (let i = 0; i < bucketCount; i++) {
    if (counts[i] > 0) out.push({ index: i, count: counts[i] });
  }
  return out;
}

/**
 * Turn a preset into a concrete (date, startSecond, endSecond, scale).
 * `now` is injected for testability.
 */
export function presetToWindow(preset: TimelinePreset, now: Date): TimeWindow {
  const today = isoDate(now);
  const yesterday = isoDate(addDays(now, -1));

  switch (preset) {
    case "today":
      return { date: today, startSecond: 0, endSecond: DAY_SECONDS, scale: "24h" };
    case "yesterday":
      return { date: yesterday, startSecond: 0, endSecond: DAY_SECONDS, scale: "24h" };
    case "last_night":
      // yesterday 20:00 → 06:00. We anchor to yesterday's date and let
      // the timeline render a 12h window starting at 20:00. The wrap
      // past midnight is left to the caller to stitch if needed; for
      // v1 we just show yesterday evening from 20:00 → 23:59.
      return {
        date: yesterday,
        startSecond: 20 * 3600,
        endSecond: DAY_SECONDS,
        scale: "6h",
      };
    case "this_morning":
      return {
        date: today,
        startSecond: 0,
        endSecond: 12 * 3600,
        scale: "6h",
      };
    case "last_12h": {
      const nowSec =
        now.getHours() * 3600 + now.getMinutes() * 60 + now.getSeconds();
      const start = Math.max(0, nowSec - 12 * 3600);
      return { date: today, startSecond: start, endSecond: nowSec, scale: "6h" };
    }
    case "last_week":
      return { date: today, startSecond: 0, endSecond: DAY_SECONDS, scale: "7d" };
    case "custom":
      return { date: today, startSecond: 0, endSecond: DAY_SECONDS, scale: "24h" };
  }
}

function isoDate(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function addDays(d: Date, days: number): Date {
  const x = new Date(d);
  x.setDate(x.getDate() + days);
  return x;
}

/** Format a second-of-day as HH:MM:SS. */
export function formatClock(second: number): string {
  const clamped = Math.max(0, Math.floor(second));
  const h = Math.floor(clamped / 3600);
  const m = Math.floor((clamped % 3600) / 60);
  const s = clamped % 60;
  return `${pad2(h)}:${pad2(m)}:${pad2(s)}`;
}

/** Format a second-of-day as HH:MM (no seconds). */
export function formatClockShort(second: number): string {
  const clamped = Math.max(0, Math.floor(second));
  const h = Math.floor(clamped / 3600);
  const m = Math.floor((clamped % 3600) / 60);
  return `${pad2(h)}:${pad2(m)}`;
}

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}
