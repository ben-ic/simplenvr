# Phase 2A — Matter spike (Node sidecar, non-Camera device type)

> **Picking this up cold?** Read this file top-to-bottom, then read `plans/phase-0-spike/matter-library-evaluation.md` and skim `plans/homekit-matter-integration-plan.md` §Phase 2. The strategy doc's Phase-2 section was written before the Phase 0 spike — **three of its assumptions are now wrong** and are corrected here. This doc is the authoritative execution plan for the 1-week Matter spike.

## Context

Phase 2A validates the **architecture**, not the Camera feature. We are answering three empirical questions in one week:

1. **Does a Node.js `matter.js` sidecar work cleanly inside SimpleNVR's bundle and Tauri process tree on Windows ARM64 and macOS arm64?**
2. **Can a synthetic Matter device that we control from Python be commissioned into Google Home, Alexa, and SmartThings end-to-end?**
3. **What's the correct IPC shape between our Python sidecar and the Node matter.js sidecar?**

We are deliberately **not** validating Camera endpoint support in 2A. Per the Phase 0 evaluation, matter.js's high-level Camera helpers aren't shipped as of early 2026, and the Matter 1.5 Camera cluster work (Camera AV Stream Management + WebRTC Transport Provider) is multi-week cluster-level implementation regardless of library choice. Camera endpoints are Phase 2B Increment 2.

What Phase 2A produces is enough information to write the Phase 2B plan responsibly. If 2A answers come back badly (Node runtime won't bundle on Windows ARM64; Google Home refuses to commission; IPC story is ugly), we re-scope 2B — or pause the Matter track entirely — with real data instead of guesses.

## Phase 0 findings baked in

Four corrections to the strategy doc, all locked here:

1. **Matter Camera is spec version 1.5, not 1.2.** Matter 1.2 (Oct 2023) added appliances. Matter 1.5 (Nov 2025) introduced the Camera device type, Camera AV Stream Management cluster, and WebRTC Transport Provider / Requestor clusters. The strategy doc's Phase-2 section references "Matter 1.2" — that's a factual error, corrected surgically in a separate edit (tracked). Phase 2 targets **1.5 (or 1.5.1, released 2026-03-31)**.

2. **Neither Python library in the strategy doc works.** `python-matter-server` is controller-only and officially in maintenance mode as Home Assistant migrates its backend to `matter.js`. CHIP Python bindings (`home-assistant-chip-core`) publish **no Windows wheel of any architecture** — a hard blocker since Windows ARM64 is SimpleNVR's primary target. Adafruit's CircuitMatter is hobby-tier, no Camera, not commercial. The Python accessory-side path is a dead end.

3. **Architecture pivots to Node.js sidecar.** `matter.js` (TypeScript, CSA-hosted, Apache 2.0) is the correct implementation. Matterbridge (production plugin manager built on matter.js) is the reference for what a bridge-shaped accessory looks like in this ecosystem. Home Assistant's 2026 Matter rewrite targets matter.js. SimpleNVR already bundles go2rtc as a non-Python sidecar, so the precedent exists.

4. **Controller maturity is uneven.** As of early 2026: **SmartThings** ships Matter-Camera today; **Google Home** supports it in the Preview app (Q1 2026 rollout, iOS-first to dodge an Android country-code bug); **Alexa** mid-2026 target, announced not shipped. Phase 2B's honest ship copy will need to reflect this — not "Works with Alexa today," but "Supports Google Home + SmartThings today; Alexa when Amazon's firmware ships." 2A does not fix this; 2A just validates that we talk to the controllers that do exist.

Plus the decision recorded during Phase 0 discussion:

5. **Alexa Smart Home Skill stays out of scope.** It would require SimpleNVR to operate cloud infrastructure (user accounts, OAuth, AWS Lambda, persistent home-to-cloud tunnels) that fundamentally breaks the local-first product promise. Matter reaches Alexa when Amazon's firmware rolls; that's the whole path. No separate Alexa track is planned — now or in 2B.

## Decisions (locked — do not re-litigate)

| Decision | Value | Why |
|---|---|---|
| Library | `matter.js` (TypeScript) via Node.js sidecar | Python Matter stack is DOA per §Phase 0 findings |
| Runtime | Node.js bundled alongside SimpleNVR | Matches go2rtc sidecar precedent |
| Spike device type | **OnOff Plug or Motion Sensor** — **NOT Camera** | matter.js Camera helpers not shipped; Camera is 2B+ |
| Target spec | Matter 1.5 / 1.5.1 | Camera device type requires 1.5; don't scaffold on 1.4 |
| Test VID | `0xFFF1` | Google Home Dev Console accepts `0xFFF1` + PID `0x8000–0x801F` for test devices |
| Production VID | Deferred to 2B | CSA registration has $ + review; not needed to validate architecture |
| IPC | **To be decided Day 4**; candidates: local WebSocket, JSON-RPC over Unix domain socket / Named Pipe, localhost HTTP | Prototype one winner; document why |
| Primary commission target | **Google Home Preview on iOS** | iOS dodges the Android country-code commissioning bug |
| Secondary commission targets | Alexa app (iOS or Android) + SmartThings app (iOS or Android) | Both commission via Matter today |
| Matterbridge reference | Read as implementation reference; don't fork | matter.js directly is the API we target; Matterbridge is an integration pattern |
| Spike duration | 5 working days | Time-box; if blocked after Day 2, bail early |
| Bundle expectation | ≤ 100 MB extra on each platform | Precedent: go2rtc is ~6 MB. Node + matter.js is heavier. Measure Day 5. |

## Architecture snapshot

```
Tauri Rust shell
      │
      ├──► tether ──► go2rtc              (existing — Go sidecar)
      │
      ├──► tether ──► simplenvr-backend   (existing — Python sidecar)
      │                    │
      │                    └──► IPC ──────┐
      │                                   │
      └──► tether ──► matter-bridge       │  (NEW — Node sidecar, matter.js)
                           ◄──────────────┘
```

**Process-tree invariants** (match existing go2rtc pattern):
- Spawned through `tether` — dies with parent on SIGKILL, own process group
- Own log file, own PID file, own data directory
- JSON-line startup messages on stdout for Rust-shell health gating (how go2rtc reports admin URL today)
- DATA_DIR/ecosystem/matter/ for fabric state + commissioning state

**Load-bearing**: the matter-bridge sidecar does NOT talk to cameras, go2rtc, ffmpeg, or SQLite directly. It is a pure Matter protocol endpoint. All camera/motion state comes from the Python sidecar over IPC. This is the additive-not-subtractive rule — Matter failures don't touch recording or detection.

## Spike milestones (5 working days)

Each day's exit criteria are binary and measurable. If any day's criteria fail decisively, **bail to Writeup on Day 5** with the failure documented — that's a valid Phase 2A outcome.

### Day 1 — Node sidecar scaffolds, matter.js "hello world"

**Goal**: Stand up a minimal Node process that advertises itself as a Matter device on mDNS, using `matter.js`. No SimpleNVR wiring yet.

**What changes**:
- New directory (path TBD Day 1 — candidate: `src-tauri/sidecars/matter-bridge/` or `matter-bridge/` at repo root, matching how go2rtc lives today)
- `package.json` pinning `matter.js` at the latest stable (0.16.x as of 2026-04)
- `src/main.ts` — ~50-80 lines advertising an OnOff Plug with VID `0xFFF1`, PID `0x8001`

**Exit criteria**:
- [ ] `npm install && npm run dev` starts the sidecar on macOS arm64 without error
- [ ] `dns-sd -B _matter._tcp local.` (or `avahi-browse -r _matter._tcp`) shows the device within 5s of start
- [ ] The sidecar writes its setup QR payload + 11-digit pairing code to stdout as JSON (precedent: go2rtc writes admin URL on startup)
- [ ] Same works on Windows ARM64 — confirm Node.js arm64 build is available and matter.js loads (**hard go/no-go**; if Node arm64 + matter.js doesn't work on Snapdragon hardware, the entire 2B path is in doubt and we re-scope)

### Day 2 — Google Home commission (iOS)

**Goal**: Ben's iPhone running Google Home Preview commissions the synthetic OnOff Plug from Day 1. State toggle works via voice + app.

**What changes**:
- `src/main.ts` refinements — stable device name, meaningful manufacturer/model strings
- Minimal state machine — OnOff plug can be toggled, state reflects back

**Exit criteria**:
- [ ] Ben scans the QR in Google Home Preview app on iPhone → device commissions within 60s
- [ ] Device appears in Google Home room list with the advertised name
- [ ] "Hey Google, turn off [device name]" flips the state; matter.js logs the command
- [ ] App-side toggle flips state; sidecar reflects via attribute change
- [ ] Commission survives a sidecar restart (fabric state persists in `DATA_DIR/ecosystem/matter/`)
- [ ] Known gotchas documented: Android country-code workaround, any ecosystem-specific surprises

### Day 3 — Matter Bridge with multi-endpoint + Alexa + SmartThings

**Goal**: Swap the single OnOff device for a Matter Bridge with 2–3 synthetic motion-sensor endpoints. Commission into Alexa and SmartThings in addition to Google Home.

**What changes**:
- `src/main.ts` — switch to Matter Bridge topology (aggregator + children)
- 2–3 synthetic MotionSensor endpoints with scriptable `OccupancyDetected` attribute

**Exit criteria**:
- [ ] Three motion-sensor endpoints advertised under a single bridge device
- [ ] Google Home Preview (iOS) commissions the bridge → all three sensors appear as individual devices
- [ ] Alexa app commissions the same bridge → all three sensors appear (requires a local Alexa hub on-LAN; document which Echo Ben used)
- [ ] SmartThings app commissions the same bridge → all three sensors appear
- [ ] Scripted `OccupancyDetected=true` → each ecosystem reflects the change within 10s
- [ ] Per-ecosystem quirks documented in `plans/phase-0-spike/matter-phase-2a-findings.md`

### Day 4 — Python ↔ Node IPC

**Goal**: The Python sidecar drives the Node matter.js sidecar. Adding a camera in SimpleNVR advertises a new motion-sensor endpoint; firing a synthetic `motion_started` from Python flips the endpoint's state in Google Home within seconds.

**What changes**:
- Python side: new module `backend/ecosystem/matter/client.py` — opens IPC to matter-bridge sidecar, sends "advertise endpoint" / "fire event" messages
- Node side: `src/ipc.ts` — accepts the chosen IPC transport, maps messages to matter.js endpoint operations
- `backend/main.py` — spawn/supervise the matter-bridge sidecar only if `SIMPLENVR_MATTER_SPIKE=1` (opt-in for 2A; 2B decides on production behavior)

**IPC decision protocol** (morning of Day 4):
1. Prototype three candidates: WebSocket (text JSON), Unix domain socket / Named Pipe (JSON-RPC 2.0), localhost HTTP (request/response).
2. Score on: ergonomics (Python + Node client both pleasant), cross-platform (Named Pipe vs. Unix socket), pushability (server → client events need to flow), bundle cost (runtime deps), debuggability.
3. Lock the winner by lunch; implement through afternoon.

**Exit criteria**:
- [ ] Adding a camera in SimpleNVR UI advertises a new motion-sensor endpoint in Google Home within **10s**
- [ ] Firing synthetic `motion_started` from Python flips the endpoint's `OccupancyDetected` attribute in Google Home within **5s**
- [ ] Firing synthetic `motion_ended` flips it back
- [ ] Killing either process (Python or Node) is recoverable when that process restarts — no stuck state, no re-commission required
- [ ] IPC winner is documented with the loser-rationale recorded (for future engineers)

### Day 5 — Writeup + 2B decisions locked

**Goal**: Produce `plans/phase-0-spike/matter-phase-2a-findings.md` with everything Phase 2B needs.

**Findings doc contents**:
- Empirical bundle size with Node + matter.js on macOS arm64, Windows ARM64, Linux x86_64
- Startup cost: measured time from process spawn to first mDNS advertisement
- IPC winner + loser + why
- Per-ecosystem commissioning quirks table (Google Home iOS vs Android, Alexa, SmartThings — which steps needed workarounds)
- matter.js API surface assessment: what's shipped vs. what we'll have to implement ourselves for Camera in 2B (cluster-level deltas)
- Go / no-go for Phase 2B — does the architecture work? Is Node sidecar bundling viable on Windows ARM64?
- Recommended 2B phase split (2B.1 = bridge + motion-sensor endpoints wired to real events; 2B.2 = Camera endpoint)

**Exit criteria**:
- [ ] Findings doc exists, reviewed by Ben
- [ ] Phase 2B plan authoring either starts or is deferred with a concrete reason
- [ ] 2A branch (or subdirectory of `homekit-integration`) has a clean single-commit-or-linear-history state
- [ ] Sidecar prototype code preserved as-is; not merged to `main` (2A is a spike, not a ship)

## IPC design (prototype space)

Three candidates, to be raced on Day 4 morning:

| Candidate | Pro | Con |
|---|---|---|
| **Local WebSocket** (e.g., `ws://127.0.0.1:PORT`) | Bidirectional; Python has `websockets`, Node has built-in; matter.js ecosystem uses it (python-matter-server talks WS) | Port-binding friction; another port to allocate like `port_finder.py` does today |
| **JSON-RPC over Unix socket / Named Pipe** | Path-based, no port conflict; strongly typed if we use a JSON-RPC lib | Cross-platform code-path divergence (Unix socket vs Named Pipe); Windows Python support is weaker than Node's |
| **Localhost HTTP** | Request-only; simplest server and client | No server→client push without SSE / long-poll; motion events would poll-wait; latency gets worse |

My current lean: **WebSocket**. Matches the ecosystem direction (python-matter-server ↔ HA via WS), cross-platform is trivial, Python side can reuse the `httpx`-adjacent async story we already have. But Day 4 morning is the decision point, not this plan.

## Dependencies to add

```
# NEW (Node sidecar — separate from backend/requirements.txt)
matter.js  (pinned in src-tauri/sidecars/matter-bridge/package.json at 0.16.x)

# NEW (Python client for IPC — subject to Day 4 IPC decision)
websockets>=12.0          # IF WebSocket wins
# — or — jsonrpcserver + uds transport  IF Unix socket / Named Pipe wins
```

Python-side deps land in `backend/requirements.txt` only when Phase 2B starts. Phase 2A keeps them under `plans/phase-0-spike/` or a local dev dependency — no production manifests touched.

## Bundle concerns (to validate Day 5)

- Node runtime size: ~40 MB per platform minimum
- matter.js + transitive deps: ~10-30 MB
- Native deps (node-mdns-like crypto): check for native code compilation on Windows ARM64
- First-run cost of Node's module resolution at startup (should be < 1s)
- Compared to go2rtc's ~6 MB Go binary, Node is heavier — document the delta honestly

Total bundle-cost delta from Phase 2 is a key 2B decision input. If it's > 100 MB per platform, revisit whether Matter's value justifies it vs. delivering to Google Home only (say) via Matter while dropping Alexa / SmartThings until they're native-worthy.

## Out of scope for 2A

- **Matter Camera device type** — 2B Increment 2 minimum
- **StreamProvider / WebRTC Transport Provider / Push AV Stream** — 2B+
- **CSA registration / production VID** — 2B when we have a shipping target
- **Settings UI for Matter** — 2B; 2A uses env var `SIMPLENVR_MATTER_SPIKE=1` to opt in
- **Real camera-event wiring** — 2A uses synthetic events triggered from Python; real `motion_started` / `motion_ended` wiring is 2B.1
- **Fabric multi-admin edge cases** — 2A commissions from three ecosystems serially; multi-admin churn testing is 2B
- **PyInstaller bundle integration** — 2A runs Node via `npm run dev`; bundle work is 2B
- **Alexa Smart Home Skill** — permanently out of scope per Phase 0 architecture finding
- **Apple Home as a Matter target** — permanently excluded per strategy doc; Apple users use HomeKit (Phase 1A)

## Decisions to lock at 2A close

1. **Go / no-go on Phase 2B**. Hard gate: does Node + matter.js run on Windows ARM64, and did commissioning succeed in at least Google Home?
2. **IPC transport** (Day 4 winner)
3. **Bundling approach**: inline Node runtime in the `.app` / `.exe`, or require system Node (strongly prefer inline per zero-config principle)
4. **matter.js Bridge pattern vs. per-camera top-level device**. Matterbridge uses Bridge; we likely follow. Confirm.
5. **Phase 2B start timing**. Before or after Phase 1A ships? Argument for *after*: Phase 1A delivers real Apple-side value; shipping 1A first de-risks. Argument for *before*: start long-running Matter work while 1A is being QA'd. No default here — Ben decides.

## Risks & mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| Node.js arm64 + matter.js won't run on Snapdragon X Elite | Low (Node publishes Windows ARM64 builds) but unvalidated | Day 1 bails early if broken; re-plan rather than continue wasted spike |
| Google Home Preview refuses test VID | Low (docs say 0xFFF1 + PID 0x8000-0x801F works) | Have backup test-VID / PID; fall back to SmartThings as primary if Google refuses |
| matter.js API churn mid-spike | Medium (project pre-1.0) | Pin to a single release; don't chase master |
| Alexa commission requires Amazon developer account we don't have | Low-Medium | Workaround: commission from local Echo hub; don't route through Alexa's dev-console |
| IPC over localhost gets flagged by Windows Defender | Low | Localhost-only; no external listen; standard Windows-sidecar pattern |
| Sidecar's mDNS advertisement collides with SimpleNVR's other sidecars (go2rtc, HomeKit bridge from Phase 1A) | Low — different service names | Document service name table in 2B |
| 5-day time-box too tight | Medium | Day 5 is writeup, not code; allow Day 1–4 to slip one day total; cap at 6 working days |

## Phase-close definition

Phase 2A is done when:

1. `plans/phase-0-spike/matter-phase-2a-findings.md` exists and is reviewed
2. Go / no-go call is made on Phase 2B
3. If go: Phase 2B plan authoring begins (separate doc, scoped to motion-sensor + bridge work only)
4. If no-go: the Matter track is paused with a written rationale; HomeKit-only shipping is the 2026 plan
5. Spike code is preserved in the branch for future reference, not merged to `main`
