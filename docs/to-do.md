# SimpleNVR — To-do

Scoped work that's been deferred. Items here are commitments — speculative ideas don't belong on this list.

---

## Browse footage — multi-day window support

The Browse-footage preset bar is locked to a single ISO date because `TimeWindow` carries one `date: string` field and the playlist loader fetches one HLS playlist per camera per date. Any preset that wants to span midnight is forced to truncate or lie. Three presets are currently affected:

- **"Yesterday evening"** (formerly "Last night") only renders yesterday 20:00 → 24:00. The full overnight window into today's small hours is unreachable.
- **"Today so far"** (formerly "Last 12 hours") clamps the window start to midnight today. Before noon it shows fewer than 12 hours; the missing hours from yesterday are unreachable.
- **"Last week"** was removed entirely — there is no rendering path that paints multiple days on one timeline.

Users will reach for "what happened overnight" or "what happened in the last 12 hours" without thinking about day boundaries. Truncating at midnight is a UX lie: events that happened are silently invisible.

### Scope

- `frontend/src/lib/timelineMath.ts`: extend `TimeWindow` to carry an absolute `start`/`end` pair (e.g. `Date` or epoch ms) instead of `date` + `secondOfDay`. Update `presetToWindow` to return real ranges.
- `frontend/src/api/client.ts` and the backend recordings router: `fetchTimeline` and the HLS playlist endpoint need to accept a date *range* and return concatenated segments + a single VOD playlist that bridges day boundaries with `#EXT-X-DISCONTINUITY` between days.
- `frontend/src/components/Recordings.tsx`: drive `viewStart`/`viewEnd` from absolute timestamps rather than seconds-of-day; update the date pill to render ranges like `Apr 11 20:00 → Apr 12 06:00`.
- `frontend/src/components/RecordingsTimeline.tsx`: tick rendering, click-to-seek math, and the playhead position all assume a single-day window. All three need to handle ranges that cross midnight.
- `MultiTimeline` (grid mode) needs the same treatment.

### Once shipped, restore

- **"Last night"** preset (yesterday 20:00 → today 06:00) — replaces the truncated "Yesterday evening".
- **"Last 12 hours"** preset (rolling 12h ending now, even before noon) — replaces "Today so far".
- **"Last week"** preset and the corresponding `7d` scale in `RecordingsTimeline`'s scale row. The `7d` scale currently exists in `TimelineScale` but is silently clamped back to 24h (`Recordings.tsx:521`, `:959`) — drop the dead clamp once 7d actually renders.

### Trade-offs to think about

- Concatenated VOD playlists across midnight may break some HLS players that don't handle `#EXT-X-DISCONTINUITY` cleanly. Test on both native HLS (Safari/WKWebView on macOS) and hls.js (Chromium/Tauri on Windows). Both paths are exercised today — see the dual-path note in `architecture.md`.
- Storage layout stays per-day on disk. Don't refactor storage for this — only the *presented* window spans days; the loader still fetches per-day under the hood and stitches in memory.
- Timezone handling: all current code assumes the local TZ of the host. Multi-day windows make TZ bugs more visible (e.g. "yesterday 20:00" the day after a DST shift). Pin the assumption explicitly when extending `TimeWindow`.

### Out of scope for this work

- Multi-day rendering in the Today view — that surface uses event aggregation, not a timeline strip, and its day-boundary story is independent.
- Anything beyond 7 days. If a user needs to look back further they pick a date from the date selector.
