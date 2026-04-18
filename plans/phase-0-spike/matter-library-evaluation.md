# Matter Library Evaluation for SimpleNVR Accessory-Side Bridge

**Audience**: SimpleNVR Phase 2A planning
**Target**: Expose N IP cameras as a Matter Bridge (one endpoint per camera) to Google Home / Alexa / SmartThings controllers
**Out of scope for Matter path**: Apple Home (separate HomeKit bridge under evaluation)
**Deployment**: PyInstaller-bundled Python sidecar, Windows ARM64 + macOS arm64 + Linux
**License floor**: Apache/MIT/BSD in-bundle; LGPL subprocess-only; GPL/AGPL banned
**Research date**: 2026-04-18

---

## 1. Recommendation

**Do not use a Python library for the Matter accessory path.** Both candidates named in the brief are unfit:

- **python-matter-server (home-assistant-libs)** is a **controller-only** library ([GitHub](https://github.com/home-assistant-libs/python-matter-server), [PyPI 2.0.2 metadata](https://pypi.org/project/python-matter-server/2.0.2/)) — it commissions other devices, it does not *advertise* itself as one. It has also officially entered **maintenance mode** as Home Assistant migrates its backend to matter.js ([matter-js/python-matter-server discussions](https://github.com/matter-js/python-matter-server/discussions)). Hard eliminator for accessory work.
- **CHIP Python bindings (project-chip/connectedhomeip)** technically expose server-side primitives — the `matter.server` module exists and the in-tree [`examples/lighting-app/python`](https://github.com/project-chip/connectedhomeip/tree/master/examples/lighting-app/python) is a working Python *accessory* — but (a) **no Python camera-app ships in-tree** ([`examples/camera-app/`](https://github.com/project-chip/connectedhomeip/tree/master/examples/camera-app) contains only `camera-common/` and `linux/` with C++/GStreamer), (b) the native `chip-core` wheel has **no Windows wheel of any architecture** ([chip-wheels README](https://github.com/home-assistant-libs/chip-wheels/blob/main/README.md) — Linux x86_64/arm64 and macOS arm64 only), and (c) bundling the 500+ MB CHIP SDK through PyInstaller on Windows ARM64 is unproven.

**Recommended instead: matter.js + Node.js sidecar, Matterbridge as reference.**

- **matter.js** ([project-chip/matter.js](https://github.com/project-chip/matter.js/)) is the CSA-hosted, permissive-licensed (Apache 2.0) TypeScript implementation with full **accessory + controller + bridge** support. Home Assistant's 2026 rewrite targets it ([matteralpha article](https://www.matteralpha.com/industry-news/home-assistant-s-migrating-to-matter-js-for-device-integration)). Release 0.16 (2026-01-12) added Electron and React Native runtimes ([discussion #2976](https://github.com/matter-js/matter.js/discussions/2976)).
- **Matterbridge** ([Luligu/matterbridge](https://github.com/Luligu/matterbridge)) is a production-grade plugin manager built on matter.js that already advertises a `bridge` device type and has working Google Home / Alexa / SmartThings commissioning documented — including known Android country-code workarounds ([README](https://github.com/Luligu/matterbridge/blob/main/README.md)).
- SimpleNVR already bundles a Node runtime for go2rtc; a second Node sidecar for matter.js is architecturally consistent and avoids CHIP's C++ dependency hell entirely.

**If Python must be the sidecar**, the least-bad option is a **Node-based Matter bridge as a subprocess** that SimpleNVR's Python sidecar communicates with over a local JSON-RPC/WebSocket — essentially Matterbridge-as-library. This keeps CHIP's LGPL-linked native deps out of the Python process and sidesteps the Windows ARM64 wheel gap.

**Phase 2A spike scope adjustment**: The brief's plan to "prototype commissioning one synthetic accessory from Google Home on Android in a 1-week spike" is **feasible using matter.js, NOT using either named Python candidate**. Detailed verdict in §4.

Critical ecosystem reality check: **Matter Camera is a Matter 1.5 device type, released 2025-11-20 — not Matter 1.2.** The brief's premise needs correction. Matter 1.2 (Oct 2023) added appliances; Matter 1.5 introduced the Camera device type, Camera AV Stream Management cluster, and WebRTC Transport Provider/Requestor clusters ([CSA announcement](https://csa-iot.org/newsroom/matter-1-5-introduces-cameras-closures-and-enhanced-energy-management-capabilities/)). SimpleNVR's Matter-Camera strategy must target Matter 1.5 (or 1.5.1, released 2026-03-31 per [v1.5.1.0 release notes](https://github.com/project-chip/connectedhomeip/releases/tag/v1.5.1.0)).

---

## 2. Per-Library Scorecard

Scoring key: 1 = blocker / unusable. 3 = workable with significant effort. 5 = drop-in fit.

### 2.1 python-matter-server

| Dimension | Score | Notes |
|---|---|---|
| **Server-side Camera device type** | **1 / 5** | Controller-only library. Zero accessory-side code. No Camera device type. |
| **Commissioning compatibility (GH/Alexa/ST)** | **1 / 5** | N/A — it initiates commissioning *onto* other devices. Can't be commissioned. |
| **PyInstaller bundling** | **2 / 5** | Uses `home-assistant-chip-core` native wheel underneath. No Windows wheel published anywhere. On macOS arm64 and Linux, the wheel works but carries 100+ MB of CHIP SDK artifacts, OpenSSL, pigweed, avahi glue. |
| **Licensing** | **3 / 5** | Apache 2.0 top level. Transitive `chip-core` links OpenSSL (Apache 2.0 / SSLeay — compatible), glib/dbus (**LGPL** — in-process linking is a compliance issue under SimpleNVR's "LGPL subprocess-only" rule). `zeroconf` pure Python, fine. No GPL. |
| **Active maintenance** | **2 / 5** | v8.1.2 on 2025-12-15 was explicitly announced as transition-to-maintenance-mode ([release notes](https://github.com/matter-js/python-matter-server/releases)). All new features land in matter.js. |
| **Vendor/product ID story** | **N/A** | Controller doesn't advertise a VID/PID. |

**Verdict: eliminated. Wrong tool category.** Keep on the radar only as the *future controller-side* consumer when SimpleNVR one day ingests Matter cameras from third parties.

### 2.2 CHIP Python bindings (project-chip/connectedhomeip via home-assistant-chip-core)

The "CHIP Python bindings" available to end users are distributed through three PyPI packages maintained by the Home Assistant team: [`home-assistant-chip-core`](https://pypi.org/project/home-assistant-chip-core/) (native ctypes binding), [`home-assistant-chip-clusters`](https://pypi.org/project/home-assistant-chip-clusters/) (pure-Python cluster models), and [`home-assistant-chip-repl`](https://pypi.org/project/home-assistant-chip-repl/). Source: [home-assistant-libs/chip-wheels](https://github.com/home-assistant-libs/chip-wheels).

| Dimension | Score | Notes |
|---|---|---|
| **Server-side Camera device type** | **2 / 5** | The C++ SDK has a working `chip-camera-app` ([camera-app/linux](https://github.com/project-chip/connectedhomeip/blob/master/examples/camera-app/linux/README.md)) — but it's C++ with a hard dependency on GStreamer 1.0 + FFmpeg dev headers. There is **no** Python camera-app example. The only Python accessory example is `lighting-app/python/lighting.py` ([source](https://github.com/project-chip/connectedhomeip/blob/master/examples/lighting-app/python/lighting.py)), which imports `from matter.server import GetLibraryHandle, PostAttributeChangeCallback` and drives a DALI bridge. That proves *some* Python server-side surface exists, but building a Camera endpoint would require (a) writing ctypes glue for all Matter 1.5 Camera clusters — Camera AV Stream Management, WebRTC Transport Provider, WebRTC Transport Requestor, Camera AV Settings User Level Management, possibly Push AV Stream Transport ([cluster list per matteralpha](https://www.matteralpha.com/explainer/what-is-a-matter-camera-and-how-does-it-work)) — because the published `home-assistant-chip-clusters` wheel is controller-oriented, and (b) bridging Python's GStreamer bindings to CHIP's WebRTC pipeline. Effectively a multi-month integration project against an evolving API. |
| **Commissioning compatibility (GH/Alexa/ST)** | **3 / 5** | The **underlying C++ SDK** has documented commissioning success against all three. Alexa explicitly supports chip-tool prototype devices ([developer docs](https://developer.amazon.com/en-US/docs/alexa/ack/matter-provision-device.html)). Google Home supports VIDs 0xFFF1 + PIDs 0x8000-0x801F for test ([Google dev console docs](https://developers.home.google.com/matter/troubleshooting)). SmartThings is the first platform with Matter-Camera support ([Samsung announcement, Dec 2025](https://news.samsung.com/global/samsung-smartthings-becomes-the-industrys-first-to-support-matter-cameras)). **But** from **Python** specifically, the only commissioning report we can find is for `lighting-app/python` as an OnOff Light via chip-tool (local); no public write-ups of a Python CHIP accessory commissioning to Google Home. The Linux-only `chip-camera-app` has documented Google Home / SmartThings commissioning in Aqara forum threads ([aqara forum](https://forum.aqara.com/t/new-google-home-preview-for-matter-1-5/164308)). |
| **PyInstaller bundling** | **1 / 5** | **Eliminator-grade.** The `home-assistant-chip-core` wheel publishes Linux x86_64, Linux aarch64, and macOS arm64 **only** ([chip-wheels README](https://github.com/home-assistant-libs/chip-wheels/blob/main/README.md)). No Windows wheel of any flavor. Windows ARM64 is a SimpleNVR primary target — this is a hard blocker. Even where wheels exist, the wheel is ~100 MB and PyInstaller's hook system would need custom work to pull in all the `libCHIP*.so` / `.dylib` transitive ctypes libraries, pigweed assets, and OTA cert blobs. No public PyInstaller spec for CHIP exists. |
| **Licensing** | **2 / 5** | SDK itself is Apache 2.0. Transitive native deps per `chip-wheels/Dockerfile`: OpenSSL (fine), **glib + dbus (LGPL)** via avahi (linux), **avahi-client (LGPL)**. On Linux these are dynamically linked to system libs (acceptable if bundled as `.so` with relink rights); on macOS, CHIP's own mDNSResponder shim is used. **WebRTC stack** (needed for Matter 1.5 Camera) pulls in libwebrtc (BSD — fine) and optionally libsrtp (BSD). GStreamer (required by `chip-camera-app`) is **LGPL** and its "good/bad/ugly" plugins include **GPL** components — *this is a blocker for SimpleNVR's in-bundle rule* unless GStreamer runs as a subprocess (as the C++ `chip-camera-app` already does, but that's compiled, not Python). |
| **Active maintenance** | **5 / 5** | Absolutely active. v1.5.1.0 shipped 2026-04-13 ([release notes](https://github.com/project-chip/connectedhomeip/releases/tag/v1.5.1.0)) with significant Camera SDK work (Push AV XML updates, WebRTC crash fixes, Camera AVSM test coverage). The repo is the canonical Matter reference implementation and will remain so. |
| **Vendor/product ID story** | **4 / 5** | Well-documented. Test VIDs 0xFFF1-0xFFF4 built in; production VID configured via `DeviceInstanceInfoProvider` at init. Google Home Dev Console supports 0xFFF1 + PID 0x8000-0x801F for dev testing ([Google Home Matter troubleshooting](https://developers.home.google.com/matter/troubleshooting)). Same swap story for Alexa/SmartThings. |

**Verdict: technically the right category, practically a dead end for SimpleNVR's constraints.** The Windows ARM64 wheel gap alone is disqualifying. Add the "no Python Camera example, write your own Camera cluster bindings" cost, the LGPL/GPL GStreamer tangle, and the 100 MB bundle bloat, and this is not a 1-week spike candidate — it's a multi-quarter research project.

### 2.3 CircuitMatter (Adafruit) — *third candidate surfaced during research*

Pure-Python implementation. [adafruit/CircuitMatter](https://github.com/adafruit/CircuitMatter).

| Dimension | Score | Notes |
|---|---|---|
| **Server-side Camera device type** | **1 / 5** | Supports only OnOffLight, ExtendedColorLight, and a sensor skeleton per [device_types/ tree](https://github.com/adafruit/CircuitMatter/tree/main/circuitmatter/device_types). No Camera, no WebRTC clusters. Adafruit explicitly positions it as **hobby use, not commercial, not certified** ([repo README](https://github.com/adafruit/CircuitMatter)). |
| **Commissioning compatibility** | **2 / 5** | README admits "CircuitMatter currently doesn't fully commission so it can't act as any specific type of device yet." Early v0.1 release notes say "kinda works on a linux box with Apple Home." |
| **PyInstaller bundling** | **5 / 5** | Pure Python, zero native deps. Would bundle trivially. |
| **Licensing** | **5 / 5** | MIT. Clean. |
| **Active maintenance** | **2 / 5** | Last tagged release 0.4.1 on 2024-10-25. Minimal activity since. |
| **Vendor/product ID story** | **3 / 5** | VID hardcoded in `BasicInformation` cluster; easy to edit but no documented test/prod swap mechanism. |

**Verdict: not production-viable even for OnOff switches, and nowhere near a Matter 1.5 Camera implementation. Interesting as a learning reference; not the answer.**

### 2.4 matter.js + Matterbridge (recommended alternative)

Not in the original brief, but the clear winner if the sidecar-language constraint can flex.

| Dimension | Score | Notes |
|---|---|---|
| **Server-side Camera device type** | **3 / 5 (improving)** | matter.js 0.16 (2026-01-12) "about to complete the basic certifiable feature set, with all clusters supported at low-level APIs." Matter-Camera high-level device helpers are NOT yet in matter.js as of early 2026 — you'd wire Camera AV Stream Management + WebRTC Transport Provider at the cluster-level API. Still dramatically less work than CHIP-Python because the ergonomics are solid. |
| **Commissioning compatibility** | **4 / 5** | Matterbridge has documented working commissioning to Apple Home, Google Home, Alexa, SmartThings, Home Assistant ([Matterbridge README](https://github.com/Luligu/matterbridge)). Known issue: Google Home on Android fails country-code check; workaround is iOS-only first commission, then Android sees it fine. Alexa requires a local Alexa hub. |
| **PyInstaller bundling** | **N/A — runs as separate Node sidecar** | Not a Python problem. Ship Node + `matter.js` bundle (~50 MB) alongside the Python sidecar. SimpleNVR already bundles go2rtc (Go binary), so precedent exists. |
| **Licensing** | **5 / 5** | Apache 2.0, dependency graph checked — no GPL/LGPL in the transitive tree for the core matter.js + nodejs packages. |
| **Active maintenance** | **5 / 5** | Ingo Fischer is full-time on matter.js via Open Home Foundation. Two major releases in 6 months. Home Assistant is the biggest consumer. |
| **Vendor/product ID story** | **5 / 5** | Clean API: `new MatterServer({ vendorId, productId })`. Test VIDs documented in [ECOSYSTEMS.md](https://github.com/matter-js/matter.js/blob/main/docs/ECOSYSTEMS.md). |

---

## 3. Controller Maturity Snapshot (early 2026)

Can the three targeted controllers actually render live video from a Matter Camera today?

| Controller | Commission Matter Camera? | Live video? | Two-way audio? | Motion/events? | Notes |
|---|---|---|---|---|---|
| **SmartThings** | **Yes, shipping** | **Yes** | **Yes** | **Yes** | First to ship Matter 1.5 Camera support, announced 2025-12 ([Samsung Newsroom](https://news.samsung.com/global/samsung-smartthings-becomes-the-industrys-first-to-support-matter-cameras), [SmartThings blog](https://blog.smartthings.com/smartthings-updates/smartthings-expands-camera-support-with-introduction-of-matter-1-5/)). Launch partners: Aqara G350, Eve, Xthings. Cameras started rolling out March 2026. |
| **Google Home** | **Yes, preview app in Q1 2026** | **Yes, in Preview app** | **Yes** | **Yes** | Matter 1.5 Camera support is rolling out via the Google Home Preview program in Q1 2026. Aqara G100 + G3 shown streaming in Preview ([Aqara forum](https://forum.aqara.com/t/new-google-home-preview-for-matter-1-5/164308), [9to5google](https://9to5google.com/2025/11/20/matter-1-5-update-brings-support-for-smart-home-cameras/)). Home APIs for Android expose `WebRtcLiveView` trait as of Feb 2026 ([Google Home Developers – Camera device guide](https://developers.home.google.com/apis/android/device/camera)). Stable rollout not yet announced. |
| **Amazon Alexa** | **Announced, not shipped** | **Mid-2026 target** | Unknown | Likely yes | Amazon has announced Matter 1.5 Camera compatibility for Echo Show devices in a 2026 firmware update, with Alexa app camera streaming from Matter devices expected **mid-2026** ([smarthomeexplorer.com 2026 roundup](https://www.smarthomeexplorer.com/guides/best-matter-compatible-devices-2026)). Commissioning a Matter bridge itself with a non-Camera child device works today ([Alexa Smart Home Matter docs](https://developer.amazon.com/en-US/docs/alexa/smarthome/matter-support.html)). Cannot confirm Camera endpoint will render live video in Alexa UI until firmware ships. |
| **Apple Home** (for context only) | Partial (1.4) | **No Matter-Camera support** | No | No | Apple has not yet announced Matter 1.5 Camera timing — AppleInsider reports it's a "when, not if" question for 2026 ([appleinsider](https://appleinsider.com/articles/26/04/06/what-the-new-matter-update-delivers-to-apple-home-users-on-smart-home-insider)). SimpleNVR's Apple path goes through HomeKit Secure Video anyway. |
| **Home Assistant** | Not yet | Not yet | Not yet | Not yet | Matter-Camera is on the roadmap but blocked by migration to matter.js backend and need to land WebRTC two-way audio first ([HA Core issue #2596](https://github.com/orgs/home-assistant/discussions/2596)). |

**Load-bearing observation for Phase 2B marketing claims:**

- **SmartThings**: "Today" — can claim full Matter-Camera integration at ship
- **Google Home**: "Via Google Home Preview, stable rollout coming" — qualify the claim
- **Alexa**: "Coming mid-2026" — cannot claim live view at ship; can only claim device registration + motion events
- **Apple Home**: "Handled by separate HomeKit bridge, HKSV"

If Phase 2B ships before Alexa's mid-2026 firmware lands, the honest UX copy is: *"Works with Google Home and SmartThings today. Alexa support rolls out as Amazon enables Matter 1.5 Cameras on Echo devices."*

---

## 4. Phase 2A Spike Feasibility Verdict

**Original plan**: prototype commissioning one synthetic accessory from Google Home on Android in a 1-week spike using python-matter-server or CHIP Python.

**Verdict**:

### With python-matter-server
**Impossible.** Wrong library category. Not 1 week or 100 weeks — it cannot advertise.

### With CHIP Python bindings
**Not 1 week. Realistically 3-6 weeks.** Requirements:
- Windows ARM64 out — dev must happen on macOS arm64 or Linux
- Write Python ctypes bindings for Matter 1.5 Camera clusters (not in `chip-clusters` wheel for accessory-side use)
- Stand up GStreamer WebRTC pipeline *and* bridge it into CHIP's WebRTC Transport Provider
- Google Home Dev Console integration (VID 0xFFF1, PID in 0x8000-0x801F range)
- Android country-code commissioning workaround (also affects matter.js path)

### With matter.js (recommended)
**1-week spike is plausible for a non-Camera device type.** Ship path:

**Day 1-2**: Scaffold a Node.js sidecar using `matter.js` 0.16, expose an OnOff Plug (test device type — tiny, works everywhere). Goal: commission to Google Home via iOS (avoiding Android country-code bug), confirm it appears.

**Day 3-4**: Swap to a Matter Bridge with 2-3 synthetic endpoints. Confirm Google Home, Alexa, SmartThings all see them. Document any ecosystem-specific gotchas.

**Day 5**: Write up learnings, lock Phase 2B architecture decisions (sidecar process model, IPC between Python and Node, VID/PID swap mechanism).

**Camera endpoint is NOT the spike target.** Camera integration is Phase 2B Increment 2 minimum — reason: matter.js's Camera high-level helpers aren't done yet, so you'd be writing raw WebRTC Transport Provider cluster implementation. Ship the bridge + OnOff/motion-sensor endpoints first, then add Camera endpoints as controllers' support matures through 2026.

### Recommended spike deliverables

1. Node sidecar boots inside SimpleNVR and advertises a `bridge` device type
2. User can scan a QR code from SimpleNVR's UI and complete commissioning into Google Home Preview (iOS and/or Android with country-code workaround)
3. A synthetic motion sensor endpoint fires state changes that surface in Google Home
4. Document the process-model decision (Node sidecar vs. Python-driven via subprocess RPC)
5. Empirical data on bundle size + startup cost of the Node runtime on Windows ARM64

---

## 5. Citations

### Matter specification & CSA
- [CSA Matter 1.5 announcement (Nov 2025)](https://csa-iot.org/newsroom/matter-1-5-introduces-cameras-closures-and-enhanced-energy-management-capabilities/) — Camera device type introduction
- [Samsung Research blog on Matter 1.5 cameras](https://research.samsung.com/blog/CSA-Matter-1-5-Release-Introducing-support-for-Cameras) — cluster details
- [Matter Alpha: What is a Matter camera](https://www.matteralpha.com/explainer/what-is-a-matter-camera-and-how-does-it-work) — Camera AV Stream Management + WebRTC Transport Provider/Requestor explainer
- [v1.5.1.0 SDK release (2026-04-13)](https://github.com/project-chip/connectedhomeip/releases/tag/v1.5.1.0) — latest Camera-related SDK patches

### Candidate libraries
- [home-assistant-libs/python-matter-server](https://github.com/home-assistant-libs/python-matter-server) — controller-only, maintenance mode
- [python-matter-server 8.1.2 release (2025-12-15)](https://github.com/matter-js/python-matter-server/releases/tag/8.1.2) — maintenance mode announcement
- [project-chip/connectedhomeip examples/camera-app](https://github.com/project-chip/connectedhomeip/tree/master/examples/camera-app) — C++/Linux-only, no Python
- [project-chip/connectedhomeip examples/lighting-app/python](https://github.com/project-chip/connectedhomeip/tree/master/examples/lighting-app/python) — the one Python accessory example in-tree
- [home-assistant-libs/chip-wheels README](https://github.com/home-assistant-libs/chip-wheels/blob/main/README.md) — confirms Linux+macOS-only wheel distribution; no Windows wheels
- [home-assistant-chip-core on PyPI](https://pypi.org/project/home-assistant-chip-core/) — Linux x86_64/aarch64 + macOS arm64 wheels only
- [adafruit/CircuitMatter](https://github.com/adafruit/CircuitMatter) — pure-Python hobby implementation, OnOff Light only

### Alternative (recommended) path
- [project-chip/matter.js](https://github.com/project-chip/matter.js/) — canonical TypeScript Matter implementation
- [matter.js 0.16 release (2026-01-12)](https://github.com/matter-js/matter.js/discussions/2976) — Electron + React Native runtime support
- [Luligu/matterbridge](https://github.com/Luligu/matterbridge) — production Matter bridge based on matter.js
- [matter.js ECOSYSTEMS.md](https://github.com/project-chip/matter.js/blob/main/docs/ECOSYSTEMS.md) — per-ecosystem device-type compatibility matrix

### Controller maturity
- [Samsung SmartThings announcement (Dec 2025)](https://news.samsung.com/global/samsung-smartthings-becomes-the-industrys-first-to-support-matter-cameras) — SmartThings first to ship Matter 1.5 Camera
- [SmartThings Expands Camera Support with Matter 1.5](https://blog.smartthings.com/smartthings-updates/smartthings-expands-camera-support-with-introduction-of-matter-1-5/)
- [9to5google: Matter 1.5 camera update](https://9to5google.com/2025/11/20/matter-1-5-update-brings-support-for-smart-home-cameras/) — Google Home timeline
- [Google Home Developers: Camera device guide for Android](https://developers.home.google.com/apis/android/device/camera) — `WebRtcLiveView` trait, updated Feb 2026
- [Google Home Developers: Matter troubleshooting](https://developers.home.google.com/matter/troubleshooting) — test VID 0xFFF1 + PID 0x8000-0x801F requirements
- [Aqara forum: Google Home Preview for Matter 1.5](https://forum.aqara.com/t/new-google-home-preview-for-matter-1-5/164308) — community commissioning reports
- [Smart Home Explorer: 2026 Matter-compatible devices](https://www.smarthomeexplorer.com/guides/best-matter-compatible-devices-2026) — Alexa mid-2026 timeline
- [Alexa Skills Kit: Matter support](https://developer.amazon.com/en-US/docs/alexa/smarthome/matter-support.html) — Alexa Matter commissioning docs
- [Alexa Connect Kit: Matter prototype provisioning](https://developer.amazon.com/en-US/docs/alexa/ack/matter-provision-device.html) — confirms 0xFFF1 test VID accepted
- [Home Assistant Matter roadmap discussion #2596](https://github.com/orgs/home-assistant/discussions/2596) — two-way-audio/WebRTC blockers for HA Matter Camera
- [Open Home Foundation roadmap issue #84](https://github.com/OpenHomeFoundation/roadmap/issues/84) — Matter video doorbell integration plan

### Ecosystem migration context
- [Matter Alpha: Home Assistant migrating to matter.js](https://www.matteralpha.com/industry-news/home-assistant-s-migrating-to-matter-js-for-device-integration) — HA's Python-to-TypeScript backend shift
- [Home Assistant Community: python-matter-server to matter.js migration](https://community.home-assistant.io/t/migration-of-python-matter-server-docker-container-to-matter-js-with-ha-2026-2/981070) — HA 2026.2 migration thread

### Licensing references
- [chip-wheels Dockerfile](https://github.com/home-assistant-libs/chip-wheels/blob/main/Dockerfile) — transitive native-library build graph (OpenSSL, glib, dbus via avahi)
- SimpleNVR internal: `CLAUDE.md` "License Constraint" (MIT/Apache/BSD permitted; LGPL subprocess-only; GPL/AGPL banned)
