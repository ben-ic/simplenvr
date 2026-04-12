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

---

## macOS distribution — Apple Silicon vs Intel `.dmg`

The current build produces `SimpleNVR_0.1.0_aarch64.dmg`, which is Apple Silicon only. It will not run on Intel Macs — `aarch64` is ARM64, and Rosetta only translates Intel → Apple Silicon, not the other direction. `README.md` and `product.md` both advertise support for Apple Silicon *and* Intel Macs, so a first public release cannot ship with only an `aarch64.dmg`.

### Scope

Two viable shapes. Option A is recommended for v0.1.0 because it has fewer moving parts.

**Option A — Two separate `.dmg` files (recommended for v0.1.0):**

- Build once per architecture:
  ```bash
  cargo tauri build --bundles dmg --target aarch64-apple-darwin
  cargo tauri build --bundles dmg --target x86_64-apple-darwin
  ```
- Produces `SimpleNVR_0.1.0_aarch64.dmg` and `SimpleNVR_0.1.0_x64.dmg`.
- Requires an actual Intel Mac (or x86_64 CI runner) for the x86_64 build. PyInstaller doesn't cross-compile cleanly — see `architecture.md`, Supported platforms — so cross-compiling from Apple Silicon won't produce a working Python sidecar.
- Add a second macOS row to the README download table. `install.md` needs no changes; it already doesn't distinguish between the two macOS binaries.

**Option B — Universal `.dmg`:**

- One `.app` contains both architecture slices; macOS picks at launch.
  ```bash
  cargo tauri build --bundles dmg --target universal-apple-darwin
  ```
- Same PyInstaller catch as Option A, plus: both architecture slices of the Python sidecar have to exist side-by-side (or be `lipo`'d together) before the Tauri build runs.
- Roughly 2× the file size, but one download link that works on every supported Mac. Standard approach for shipping commercial Mac apps.
- Likely v0.2.0 territory, not the first release — more moving parts to debug before the dual-sidecar workflow is stable.

### Trade-offs to think about

- Option A forces users to know whether their Mac is Intel or Apple Silicon before downloading. Most do (About This Mac shows it prominently), but it's a visible friction point on the download page and adds one decision to an otherwise obvious flow.
- Option B is a single download, but adds a new cross-architecture Python bundling workflow that hasn't been built yet — potentially a fragile first-release risk. A bad universal bundle would break *every* Mac user, whereas a broken Option A affects only the arch whose build broke.
- Securing an Intel Mac (or x86_64 CI runner) for the x86_64 PyInstaller step is a hard dependency for *either* option. No way around it until someone writes a PyInstaller cross-compile workflow, and that's not on anyone's roadmap.

### Out of scope for this work

- Windows ARM64 distribution — that's the primary deployment target and already works via the build pipeline in `build-windows.md`.
- macOS code signing and notarization — tracked separately as a pre-release task; the unsigned-bypass instructions in `install.md` are the current workaround.

---

## ~~Native video overlay for live preview — `tauri-plugin-rtsp-mosaic`~~

**DONE.** Shipped. Plugin at `github.com/ben-ic/tauri-plugin-rtsp-mosaic`, integrated via git dep. libmpv render API (vo=libmpv + OpenGL), native NSView surfaces on macOS, `<rtsp-tile>` custom element, health events, fullscreen/mute/motion APIs. See `architecture.md` "Live preview path" for the full design.

---

## Live view stream quality — main stream with sub-stream fallback

**Status: not started.**

Live camera tiles currently default to `preferSubstream={true}` in `Home.tsx`, which means they always use the sub-stream when available. The intended behavior is: **default to the main (high quality) stream; if it fails or stalls, automatically fall back to the sub-stream.**

### Why

The sub-stream is 640×480 on Reolink and 640×360 on TP-Link — noticeably soft on a desktop monitor, especially when a tile is focused/fullscreen. The main stream is the camera's full resolution and the quality difference is stark. Users should see the best quality by default; the sub-stream exists as a graceful degradation, not the default.

### Scope

- `frontend/src/components/Home.tsx`: Change `preferSubstream={true}` to `preferSubstream={false}`.
- `frontend/src/components/NativeCameraTile.tsx` or the plugin itself: Add fallback logic — if the main stream stalls (no `first_frame` event within N seconds, or a `failed` health event), retry with `_sub` stream ID. The `useTileEvents` hook already surfaces `stalled`, `restarting`, and `failed` states per tile.
- Consider: when multiple cameras are in a dense grid (6+), bandwidth may be a concern with all on main stream. Possible heuristic: use main when ≤4 tiles visible, sub when >4, main always when focused/fullscreen. But start simple — main-first, sub-fallback — and see if bandwidth is actually a problem before adding heuristics.

---

## Hub camera enumeration — Reolink, TP-Link, Eufy HomeBase 2

**Status: not started.** Data model, fingerprints, and RTSP probe patterns exist. Missing: the scanner logic to enumerate cameras behind a hub after authentication.

### Scope

After the scanner discovers and the user authenticates to a hub device:
1. Probe per-channel RTSP paths (brand-specific patterns from `rtsp_probe.py`)
2. Create a `hub_camera` entry for each responding channel with `parent_hub_id` set
3. Register each hub-camera's RTSP path with go2rtc for loopback fan-out
4. Handle sleep/wake lifecycle for battery cameras (mark `"asleep"` not `"offline"`)

Build order: Reolink hubs first (ONVIF + clean RTSP, largest user base) → TP-Link → Eufy HomeBase 2 (requires manual RTSP enable in the Eufy app).

See `architecture.md` "Hub devices" for the per-brand RTSP support table.
