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

## Native video overlay for live preview — `tauri-plugin-rtsp-mosaic`

**Status: in progress.** Standalone open-source plugin at `/Users/benjamincates/Dev/tauri-plugin-rtsp-mosaic/` (will be `github.com/ben-ic/tauri-plugin-rtsp-mosaic`). Scaffold compiles, macOS NSView platform implemented, mpv --wid spike pending.

The live camera grid renders through go2rtc's WebRTC/MSE pipeline into the Tauri webview. WebRTC delivery adds overhead that causes visible frame drops — the same issue Frigate and every other webview-based NVR has. The plugin bypasses the browser entirely: mpv subprocesses decode RTSP and render into native child surfaces (NSView, HWND, X11 window) positioned within the Tauri window.

### Architecture

- One mpv subprocess per tile, controlled via JSON IPC
- mpv built as LGPL 2.1+ (`-Dgpl=false`), bundled like ffmpeg/go2rtc
- Platform layer: NSView (macOS), child HWND (Windows), X11 child window (Linux)
- Frontend sends tile rects via Tauri commands; `ResizeObserver` tracks layout changes
- WebRTC path stays as fallback (dev mode, platforms without mpv)

### Integration into SimpleNVR

- `src-tauri/Cargo.toml` depends on the plugin via local path (dev) or git URL (CI)
- **TODO**: Before CI builds work, push the plugin repo to GitHub and switch to `tauri-plugin-rtsp-mosaic = { git = "https://github.com/ben-ic/tauri-plugin-rtsp-mosaic" }` in `src-tauri/Cargo.toml`. The current `path = "../../tauri-plugin-rtsp-mosaic"` is dev-only.
- Plugin registered in `src-tauri/src/lib.rs` via `.plugin(tauri_plugin_rtsp_mosaic::init())`
- Permissions added to `src-tauri/capabilities/default.json`
- `CameraTile.tsx` gains a native-tile branch that calls `createTile()` when the plugin is available

### Remaining work

1. **macOS spike** — prove mpv `--wid` renders into our NSView with a real RTSP stream
2. **IPC wiring** — mute/unmute, first-frame detection, stall detection, stats
3. **Windows platform** — child HWND + named pipe IPC (deployment target)
4. **Linux platform** — X11 child window
5. **`scripts/fetch-mpv.sh`** — download LGPL mpv binary per platform at build time
6. **Frontend integration** — `CameraTile.tsx` native-tile branch + `ResizeObserver`
7. **Docs** — README, SECURITY.md, example app
8. **Publish** — push to GitHub, crates.io, npm
