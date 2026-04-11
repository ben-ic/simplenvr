# SimpleNVR — Object Classification and Event Summarization

> **Status (session 13 and later)**
>
> - **Object classifier (YOLOX-S)** — **SHIPPING**. Bundled into the PyInstaller sidecar via `backend/main.spec`. Weights live at `backend/classification/models/` (gitignored source, fetched by `scripts/fetch_yolox.sh` before bundling). Resolved at runtime by `classification.classifier._bundled_model_dir()` and `classification.capability_probe._bundled_model_dir()` from `sys._MEIPASS` in frozen mode or the repo path in dev. **There is no network download path.** If the ONNX file is missing from the bundle, the classifier raises `FileNotFoundError` and stays disabled; the Inbox falls back to "Motion at X" with no label.
> - **Event summarizer (Moondream 2B VLM)** — **REMOVED**. The on-device VLM summarizer was cut in session 13 in favor of a planned Claude Flash cloud subscription tier (see `architecture.md` §Summarizer and the memory note on project_subscription_tier). The sections below that describe the v2 summarizer experience, the ~1 GB Moondream download, the IR gate, the dual-prompt design, etc. are **preserved as historical design reference** for a future local-VLM revival, but no code in this repo currently emits `model_download` events, downloads weights, runs a VLM, or writes to `motion_events.summary` / `motion_events.description`. Those columns and the `_summarizer` hook point on the classifier remain in the schema/code so a future local VLM can slot in without migration work.
>
> When reading this doc, treat the summarizer parts as a frozen design document, not a current specification. The classifier parts describe what actually runs today.

Two related AI features were originally designed as separate product tiers:

1. **Object classifier (v1, always bundled)** — upgrades Inbox rows from *"Motion at Front door"* to *"Person at Front door"* / *"Vehicle at Driveway"* / *"Animal at Back yard"* using a bundled YOLOX-S ONNX detector (Apache-2.0, ~34 MB). **Currently shipping.**
2. **Event summarizer (v2, optional download)** — *originally* generated daily natural-language activity digests using Moondream 2B 4-bit (Apache-2.0, ~1 GB, user-elected download). *"Yesterday: a black car arrived at the carport at 07:14, the mail carrier delivered a package at 10:32, a kite was visible in the backyard most of the afternoon."* **Removed in session 13** — replaced by a planned Claude Flash cloud subscription tier, not yet built.

Both features were designed to be:
- License-clean (Apache-2.0 models + weights)
- Fully local (no cloud, no subscription, matches `product.md` zero-config principle) — the replacement Claude Flash path moves the summarizer half of this promise behind a paid subscription; the classifier half remains fully local
- Hardware-probed at first launch (weak hardware gets classifier only, strong hardware got both offered in the original design)
- Governed by the same silent-confidence-fallback invariant (when the model isn't confident, the Inbox row stays honest — "Motion at X" — rather than lying with a wrong label)

For the product framing of *why* these features exist, see `product.md`. For the system architecture they plug into, see `architecture.md`. For the concrete implementation plan with files and phases, see the private plan doc `classifier-and-summarizer-plan.md`. This document describes the *how*.

---

## Product framing

**What the user sees**: motion-event rows in the Inbox gain a label word when the model is confident. Nothing else changes — no settings screen, no confidence slider, no class picker, no "Enable AI" toggle. The feature is either working (Inbox rows get labels) or not (Inbox rows stay "Motion at X").

**The load-bearing design property**: silent confidence fallback. When the model is not confident, the row stays "Motion at X." Never a wrong label. Never a "Person" on a shadow. Never a "Bird" on a wall-mounted alarm keypad. The feature is strictly additive — it can never make the product worse than shipping without it.

**Label set**: `person`, `vehicle`, `animal`. Three buckets, not 80 COCO classes. Package detection is a separate v2 feature that requires fine-tuning on a non-COCO dataset. Face recognition, license-plate reading, person tracking, and behavior analysis are explicitly out of scope per `product.md`.

**Per `product.md`'s "basic motion detection yes, AI bells and whistles no" line**: object classification as described here is *basic motion detection with a label*, not an AI feature. It has no user-facing configuration, no screens, no knobs, and no settings. If it ever grows any of those things, it has become an AI bell and whistle and needs a different conversation. The **event summarizer** (v2) is closer to the line — it produces rich natural-language output — but remains zero-config, on-device, license-clean, and gated on hardware capability. It requires an explicit `product.md` sign-off before phase 4 of the plan executes.

---

## Hardware probe — the feature gate

SimpleNVR ships to a non-technical user's Intel NUC *and* the Snapdragon X Elite Copilot+ PC from the same installer. Hardware can't be hard-coded. At first launch, a **capability probe** runs a 20-inference YOLOX-Nano calibration benchmark and measures warm median latency against bundled test data. The result buckets into four tiers:

| Warm latency (YOLOX-Nano) | Tier | Classifier default | Summarizer offered? |
|---|---|---|---|
| < 15 ms | 🟢 **Strong** | YOLOX-S 640 | **Yes** — Moondream 2B 4-bit (~1 GB download) if ≥ 3 GB free disk |
| 15–50 ms | 🟡 **Normal** | YOLOX-S 640 | **Yes** — Moondream 2B 4-bit (~1 GB) if ≥ 3 GB free disk |
| 50–150 ms | 🟠 **Modest** | YOLOX-Nano 416 | No |
| ≥ 150 ms | 🔴 **Weak** | YOLOX-Nano 416 with reduced-fps sampling | No |

The probe **also checks disk space** via `shutil.disk_usage(app_data_dir)` and writes `settings.free_disk_mb` + `settings.disk_pressure`:

| Free disk (above recording budget) | `disk_pressure` | Behavior |
|---|---|---|
| ≥ 5 GB | `OK` | Normal |
| 1–5 GB | `LOW` | Summarizer pauses for new days. Classifier continues. User sees "Low disk — daily summaries paused until space frees up." |
| < 1 GB | `CRITICAL` | Both classifier and summarizer pause. User sees prominent warning. Recording continues per the existing janitor. |

The probe result is cached in `settings.capability_fingerprint` so it only re-runs when the OS version, accelerator drivers, or RAM change. This is cheap on every boot after the first.

**Ben-the-dev escape hatches** (never user-facing):
- `SIMPLENVR_CLASSIFIER_TIER=strong|normal|modest|weak` — force tier
- `SIMPLENVR_CLASSIFIER_EP=coreml|qnn|directml|openvino|cuda|cpu` — force EP
- `SIMPLENVR_CLASSIFIER=off` — disable classifier entirely
- `SIMPLENVR_SUMMARIZER=off` — disable summarizer entirely
- `SIMPLENVR_VLM_FORCE_AVAILABLE=1` — offer Moondream download even on Weak tier (for testing)

**Minimum system requirement**: 10 GB free disk at install time, enforced by the Tauri installer. Below that, SimpleNVR refuses to install — the product cannot usefully record video below that threshold regardless of the AI features.

---

## The pipeline

Modeled on a reference project's published architecture. Every component except the ONNX model is pure Python + OpenCV + existing SimpleNVR infra — no new dependencies, no new RTSP connections, no new ffmpeg pipes.

```
ffmpeg (per camera, unified pipeline — already built)
    │
    └── Output 2: scene-change JPEGs → frame_broadcaster  [existing]
                                            │
                                            ▼
                          backend/motion/detector.py      [existing, +30 lines]
                          MOG2 background subtraction,
                          emits motion events with
                          bounding-box coordinates
                                            │
                                            ▼
                          backend/motion/tracker.py        [NEW ~100 lines]
                          IOU-based track assignment,
                          groups consecutive motion
                          frames into tracked events
                                            │
                                            │ tracked_event (N≤3 frames, bbox, duration)
                                            ▼
                        ┌─────────────────────────────────┐
                        │  asyncio.Queue (bounded, global)│   [NEW]
                        └──────────────────┬──────────────┘
                                           │
                                           ▼
                          backend/classification/         [NEW ~350 lines]
                          classifier.py — one shared ORT session
                          manager.py — single worker task, drains queue
                                           │
                                           │ (label, confidence) or (None, None)
                                           ▼
                          db.motion_events.object_class  [NEW column]
                                           │
                                           ▼
                          event bus emits motion_event_updated
                                           │
                                           ▼
                          frontend/src/components/Inbox.tsx
                          motionEventToInboxEvent() reads
                          object_class, picks the right sentence
```

### Per-stage responsibilities

**1. Motion detector (existing, augmented)**. Uses OpenCV's `BackgroundSubtractorMOG2` instead of raw pixel diff. Learns the per-camera background model on first launch, so stationary clutter (keypads, clocks, wall art, pergolas, trees) becomes part of the baseline and never produces a motion blob. Emits `motion_event` records with a bounding box tightened to the moving contour.

**2. Tracker (new, ~100 lines, pure Python + numpy)**. IOU-based track assignment. Consecutive motion frames with overlapping bboxes on the same camera get merged into a `tracked_event`. A yard walk is *one* tracked event with N frames, not N raw motion events. The tracker promotes a candidate to a real tracked event only after ~5 consecutive frames — this alone filters out single-frame flickers that would otherwise fire the classifier.

**3. Classifier worker (new, ~350 lines total)**. One shared ONNX Runtime session for all 32 cameras. Consumes `tracked_event` messages from the bounded async queue. For each tracked event:
  - Picks 2-3 frames (start, midpoint, end)
  - Crops each frame to the tracked bbox + ~30% margin, square, resize letterbox to 640×640
  - (Optional preprocessing) CLAHE via OpenCV if the frame is detected as IR/greyscale
  - Runs YOLOX-S inference via ORT
  - Collapses COCO class ids → `{person, vehicle, animal, None}` **at the scoring layer**, not post-hoc — same labelmap approach a reference project documents
  - Takes the **median confidence across frames** per class (two-stage scoring — one-off high-confidence false positives on stationary clutter never survive median-of-track)
  - Promotes to `object_class` in the DB only if median ≥ 0.30, otherwise writes `NULL` (silent fallback)
  - Emits a WebSocket `motion_event_updated` event so the Inbox can refresh

**4. Inbox surface (existing, ~20 lines edit)**. `motionEventToInboxEvent()` already exists as the single seam for "upgrade the sentence." Reads `object_class` if present, else falls back to the generic "Motion at X."

---

## Model selection — YOLOX-S via ONNX Runtime

### Why YOLOX specifically

Alternative detectors were audited in the April 2026 license verification pass. The commercial-safe set (code + weights both Apache-2.0 or compatible) is:

- **YOLOX** (Megvii) — Apache-2.0 ✅ chosen default
- NanoDet / NanoDet-Plus — Apache-2.0
- MobileNet-SSD v2 (TF Model Garden) — Apache-2.0
- EfficientDet-Lite (Google) — Apache-2.0
- PP-YOLOE (PaddleDetection) — Apache-2.0
- RT-DETR (Paddle version only, NOT the ultralytics fork which is AGPL) — Apache-2.0
- DAMO-YOLO — Apache-2.0
- DEIMv2 — Apache-2.0
- D-FINE — Apache-2.0 (re-verify LICENSE file before bundling; raw fetch returned 404 in the audit)
- RF-DETR N/S/M/L only — Apache-2.0 (XL and 2XL are PML 1.0 proprietary, do not bundle)

YOLOX is chosen because:
1. Qualcomm publishes a pre-quantized YOLOX for Snapdragon X Elite NPU on [Qualcomm AI Hub](https://aihub.qualcomm.com/compute/models/yolox) — removes the x86-only quantization barrier for our primary deployment target
2. Measured warm latency of 30 ms at 640×640 on Apple Silicon CoreML (`experiments/object-detection/run_yolox_s.py`, 2026-04-09)
3. 9 ms at 640×640 INT8 on Snapdragon NPU per Qualcomm's published benchmark
4. 34 MB ONNX file — fits in the installer budget
5. Battle-tested in a reference project's production deployment (a reference project also uses onnxruntime for its ONNX detector path — we are not inventing)

### License RED list — do not bundle, ever

These were evaluated during research and are permanently excluded:

- **Ultralytics YOLOv5 / v8 / v11** — AGPL-3.0
- **YOLOv6** (Meituan) — GPL-3
- **YOLOv7** — GPL-3
- **Gold-YOLO** (Huawei Noah `Efficient-Computing/Detection/Gold-YOLO/LICENSE`) — confirmed GPL-3 verbatim. The parent Efficient-Computing repo has NO root LICENSE — every subproject sets its own. Do not assume "Huawei Noah = Apache."
- **YOLO-NAS** (Deci) — Apache code, **non-commercial weights** (the canonical weights-licensed-differently cautionary tale)
- **YOLO-World** (AILab-CVC) — GPL-3, cannot ship. (Legal to use offline on the build box to pre-label training data per GPL §2 carve-out, but not to ship.)
- **ExDark dataset** (low-light) — BSD-3 code + explicit commercial-use restriction in README requiring written permission
- **Objects365 dataset** — "Non-Commercial" framing despite CC-BY 4.0 annotations. Skip.
- **FLIR ADAS thermal dataset** — CC-BY-NC-SA, and wrong modality (LWIR thermal ≠ 850 nm near-IR surveillance)

### ONNX Runtime as the inference runtime

Same library a reference project uses under a thin provider-selection wrapper. Execution provider chain, picked at startup in priority order:

| Platform | EP priority | Falls back to |
|---|---|---|
| Windows ARM64 (Snapdragon X Elite, Copilot+) | QNN HTP → DirectML → CPU | DirectML on Adreno GPU if NPU driver missing; CPU if DML fails |
| Windows x86_64 (Intel) | OpenVINO → DirectML → CPU | |
| Windows x86_64 (NVIDIA) | CUDA → DirectML → CPU | |
| macOS Apple Silicon | CoreML → CPU | |
| macOS Intel | CoreML → CPU | CoreML on Intel is weak but still better than nothing |
| Linux x86_64 (Intel) | OpenVINO → CPU | |
| Linux x86_64 (NVIDIA) | CUDA → CPU | |
| Raspberry Pi 4/5 | XNNPACK → CPU | |

### The Snapdragon quantization gotcha

QNN HTP (the Hexagon NPU) requires pre-quantized models (uint8/uint16 QDQ). **ONNX Runtime's quantization utilities are x86_64-only** per the official QNN EP docs — you cannot quantize on an ARM64 device. This has one concrete implication for our build pipeline: **the Snapdragon-accelerated model must be prepared on an x86_64 CI runner.** Our macOS dev hosts can write code but cannot produce the Snapdragon artifact.

Build pipeline for the Snapdragon build:
1. x86_64 CI runner installs `onnxruntime-qnn`
2. Downloads upstream Megvii YOLOX-S ONNX (Apache-2.0)
3. Runs ORT QNN static quantization → INT8 QDQ model
4. Optionally generates a QNN context binary (`.bin`) pre-compiled for HTP — amortizes first-run compile cost
5. Bundles `.onnx` + `.bin` in `src-tauri/binaries/` alongside the FFmpeg builds

---

## Hardware tiering — per-device model selection

The product ships to a non-technical user's Costco laptop *and* your Snapdragon X Elite from the same installer. Hard-coding a model choice doesn't work. Instead, SimpleNVR runs a **one-time calibration benchmark** at first launch and picks the tier from measured latency, not from a spec database.

### The calibration

At first launch, after the classifier subsystem initializes, it runs **20 warmup inferences on YOLOX-Nano against a bundled 1 MB calibration image** and records the warm median latency. The benchmark itself is the answer — no "this chip should be fast" assumptions.

### The tier table (revised 2026-04-09 after Moondream empirical tests)

| Warm YOLOX-Nano latency | Tier | Classifier default | Summarizer offered? |
|---|---|---|---|
| < 15 ms | 🟢 Strong | YOLOX-S 640 | **Yes** — Moondream 2B 4-bit (~1 GB, background download after app working) |
| 15–50 ms | 🟡 Normal | YOLOX-S 640 | **Yes** — same |
| 50–150 ms | 🟠 Modest | YOLOX-Nano 416 | No |
| ≥ 150 ms | 🔴 Weak (RPi CPU, old laptop) | YOLOX-Nano 416 with reduced-fps sampling | No |

The tier is stored in `settings.classification_tier` so the calibration only runs once per install. `SIMPLENVR_CLASSIFIER_TIER=<strong|normal|modest|weak>` overrides it for Ben-the-dev.

### Moondream as a v2 feature, not a v3 one

Yesterday's doc treated Moondream as v3 / optional / premium-only. **Today's empirical testing moved it to v2 / shippable to all Strong+Normal tier users.** The change is justified by two concrete findings from the 2026-04-09 head-to-head test on 20 daytime frames:

1. **Moondream 2B describes daytime scenes accurately without confabulation.** On real daytime frames from Ben's carport camera, Moondream correctly produced *"A black car is parked in a covered parking area, partially obscured by a wooden fence"* — matching ground truth including vehicle type and context. Raw accuracy is comparable to YOLOX-S (both models missed similar events, both produced ~3 false positives out of 20).
2. **The confabulation failure mode is specific to IR night input.** On IR greyscale night frames, Moondream invented colors it couldn't possibly see ("black SUV and white hatchback" in a pure-grey image). This is handled by a 2-line RGB-channel-divergence gate in the summarizer: if the frame is IR, skip the VLM and fall back to the classifier's label. The describe output is only used on daylight/color frames.

Neither finding makes Moondream a drop-in replacement for YOLOX at the classify layer — it still hallucinates single-word labels ~15% of the time even on daylight. **The classifier stays YOLOX-S.** Moondream's real product value is the *describe* prompt used for daily summaries, where the natural-language output is a different UX entirely.

### The v2 summarizer experience (new)

Per Ben's 2026-04-09 decision, the ~1 GB Moondream 4-bit download is **not** an opt-in power-user feature hidden in an advanced menu. It's a **post-install background download** that happens automatically on Strong/Normal tier hardware with ≥ 3 GB free disk. The user sees:

1. App starts working immediately after install. YOLOX-S is bundled and labeling events from second 1.
2. After ~30 seconds of successful classification, a discreet banner in the Inbox header: *"Get daily activity summaries? Downloads a 1 GB model the first time. Everything runs locally on your machine."* With *"Get it"* and *"Not now"* buttons. Not a modal. Not blocking.
3. User clicks *"Get it"* → background download with a small progress indicator in the corner. App stays fully functional.
4. Once downloaded, a daily digest runs at 06:00 local each morning on the previous day's tracked events. It appears as a special Inbox row: *"Yesterday: a black car arrived at the carport at 07:14, the mail carrier delivered a package at 10:32, a kite was visible in the backyard most of the afternoon. 2 additional motion events at night were not described."*
5. On IR night events, the aggregator falls back to classifier-label-only summaries ("3 vehicle events at the carport, 1 person event at the front door"). No color confabulation, no invented specifics.

This design matches `product.md`'s principles: zero user-facing configuration (the banner is the only decision point), on-device, no cloud, no subscription, no recurring cost. The minimum system requirement increase (10 GB free disk at install) is a product-level change that needs explicit `product.md` sign-off, since it raises the bar from the current "runs on a potato" promise.

**The concrete implementation plan** with phases, file list, and test criteria lives in the private plan doc `classifier-and-summarizer-plan.md`.

---

## Moondream — model variants, sizes, hardware tiers

Verified 2026-04-09 via HuggingFace and Moondream's own benchmarks:

| Model | Disk | RAM | Warm inference | Shippable? |
|---|---|---|---|---|
| Moondream 0.5B INT8 | **479 MB** | **996 MB** | ~2-10 s depending on hardware | Weak tier only if we ship VLM there at all |
| Moondream 2B 4-bit | ~**1 GB** | ~2 GB | ~1-5 s on Apple Silicon / desktop | **v2 default — auto-downloaded on Strong/Normal** |
| Moondream 2B FP16 | **3.86 GB** | ~5 GB | 5 s measured on M4 MacBook Air MPS (2026-04-09) | Never ship — too large, no meaningful quality improvement over 4-bit |

### Raspberry Pi viability

Published Pi 5 benchmarks ([core-electronics guide](https://core-electronics.com.au/guides/getting-started-with-moondream-on-the-pi-5-human-like-computer-vision/), [Geeky Gadgets](https://www.geeky-gadgets.com/raspberry-pi-ai-vision-moondream/)):

- **Moondream 2B on Pi 5**: 22–25 seconds per image at best
- **Moondream 0.5B on Pi 5**: 8–10 seconds per image
- **Large (>512×512) images**: 30–45 seconds just to encode

Pi is a **Weak tier** platform. YOLOX-Nano via XNNPACK on Pi 5 CPU runs in ~100–200 ms (100–250× faster than any Moondream variant), which is what we ship there. Moondream on Pi is technically possible but only for very light workloads and never by default.

---

## What `product.md` won't let us do

Pre-committing these to the record so we don't relitigate them per-conversation:

- **No face recognition.** Rules out every identity-aware feature.
- **No license-plate reading.** Rules out LPR models and datasets.
- **No person tracking across cameras.** Single-camera tracking for the classifier's own debounce is fine; cross-camera identity is not.
- **No behavior analysis.** "This person looks suspicious" is not a product.
- **No cloud video.** Frames never leave the machine without explicit user action (e.g., clicking an "Export clip" button).
- **No always-on subscription.** A one-time license purchase is the business model.
- **No 100+ camera support.** The classification architecture assumes ≤32 cameras with sparse motion events.

The Moondream 0.5B optional-download tier respects all of these — the model runs locally, is a user-elected opt-in, has no cloud dependency, and doesn't enable any of the banned features.

---

## Research findings — empirical evidence from the 2026-04-09 test

All results from `experiments/object-detection/` in this repo. Frames extracted at exact motion-event timestamps from real recordings off Ben's 5 LAN cameras (Eufy, 2× Reolink, 2× Tapo).

### Performance (YOLOX via onnxruntime on Apple Silicon)

| Model | Input | Warm median | Cold | Disk |
|---|---|---|---|---|
| YOLOX-Nano | 416×416 | **6.4 ms** | 20 ms | 3.5 MB |
| YOLOX-S | 640×640 | **30 ms** | 40 ms | 34 MB |

Well under the <500 ms per-event latency budget. 32 cameras firing simultaneously at the Snapdragon NPU's 9 ms = 288 ms worst case. In practice motion events are sparse after tracking; steady-state CPU/NPU cost is negligible.

### Detection quality on real frames

| Camera | Scene | Baseline full-frame | **a reference project-style motion crop** | v1 behavior |
|---|---|---|---|---|
| Outdoor wide-angle (IR night, car in yard) | Car visible middle-left | Nothing (or garbage "traffic light 0.39") | **vehicle 0.58 via "car"** ✅ | "Vehicle at X" |
| Carport (IR night, person visible above pergola) | Person in frame with vehicles beyond | person 0.19 (sub-threshold) | **person 0.37** ✅ | "Person at X" |
| Carport (multi-subject) | Vehicle + person on same frame | vehicle 0.23, person 0.15 | **vehicle 0.34 + person 0.21** ✅ | "Vehicle at X" (higher wins) |
| Ceiling-mounted Tapo (IR, top-down view of person's head/hair) | Person walking under camera | 0.10–0.11 max everywhere | 0.10–0.11 max everywhere ❌ | **"Motion at X" (silent fallback — correct behavior)** |
| Empty scene with wall-mounted alarm keypad | Nothing | **bird: 0.63 (false positive)** | N/A — motion-gated pipeline never reaches inference on a still frame | **"Motion at X"** — motion gate prevents this class of error entirely |

### Key takeaways from the experiment

1. **The a reference project-style motion-blob crop is the single biggest quality win.** Taking a car from "not detected" to "vehicle 0.58 via car" on the same frame with the same model, just different preprocessing. This is not a model-selection problem — it's a pipeline problem.

2. **Label collapse at the scoring layer works.** Frame-level false positives like "traffic light 0.39" silently disappear because traffic lights are not in the `{person, vehicle, animal}` set. No post-hoc filtering needed.

3. **COCO classes on IR footage produce embarrassing-but-useful detections.** "Train" at 0.22-0.35 on parked cars in a carport (pergola + car shapes read as rail-car sides to the model). "Truck" at 0.34 on a different frame of the same scene. Both collapse to `vehicle` via the labelmap, so the user never sees the wrong COCO name — only *"Vehicle at Carport"* which is correct.

4. **Ceiling-mounted cameras are a genuine limitation, handled correctly by the fallback.** COCO "person" is trained on standing/walking full-body views. Top-down views of hair don't exist in COCO. No amount of cropping or preprocessing rescues this. The silent fallback keeps the row as "Motion at X" — honest behavior, no regression vs. shipping without the classifier.

5. **A single global confidence threshold is broken.** The wall-keypad-as-bird false positive fires at 0.63, *higher* than the real vehicle detections at 0.22–0.58. The fix is two-stage scoring (per-frame `min_score` ≈ 0.20 to reject noise, median-of-track `threshold` ≈ 0.50 to promote to an event) combined with motion gating so still-frame false positives can't enter the pipeline at all.

### Moondream 2B head-to-head — 2026-04-09 test results

On the same frames plus 20 additional daytime frames from camera `0c1ab3e9` (the dev backend's backyard camera) and `1c3afcfa` (carport):

**Performance on M4 MacBook Air, 24 GB RAM, via HuggingFace transformers + MPS**:

| Metric | YOLOX-S ORT/CoreML | Moondream 2B FP16 (bfloat16) MPS |
|---|---|---|
| Model disk size | 34 MB | **3.86 GB** (**113× bigger**) |
| Warm inference per prompt | 30 ms | **~5000 ms describe + ~4800 ms classify** (**~330× slower**) |
| Cold start | 40 ms | 8.1 s |
| RAM in use | ~150 MB | ~4-5 GB |

**Detection quality — daytime carport (3 frames with real vehicles)**:

| Frame | YOLOX-S | Moondream describe (natural language) | Moondream classify |
|---|---|---|---|
| `1f67a54c` | vehicle 0.75 via "car" + person 0.43 | *"A **black car** is parked in a **covered parking area**, partially obscured by a **wooden fence**, with a street and buildings in the background"* | vehicle ✅ |
| `794ca8e4` | vehicle 0.85 via "car" + person 0.31 | *"A **covered carport with cars parked inside** is visible on a residential street"* | vehicle ✅ |
| `ca7dc8be` | vehicle 0.81 via "car" | *"A covered parking area with cars, a wooden fence, and a small grassy space"* | vehicle ✅ |

**Both models agree confidently on real daytime vehicles.** This is the v1 validation — YOLOX-S produces 0.75–0.85 confidence on real daylight carport frames with the correct "car" class, well above the shipping threshold. Moondream's describe output adds no misleading specifics.

**The IR night confabulation finding**:

On IR greyscale night frames, Moondream 2B repeatedly invented colors it could not possibly see from a monochrome image:
- *"two cars, a **black SUV and a white hatchback**"* — an IR frame has zero color information
- *"**backyard with a swing set, fence, and shed**"* — some of these specifics were correct (this yard does have a swing set and shed, which the model may have pattern-matched from camera context) but others were stock phrasing
- *"**bench** near a fence"* — on a frame where YOLOX saw "train 0.23" on the pergola slats

**The lesson**: Moondream's `describe` prompt is useful on daylight/color, and a hallucination hazard on IR night. The summarizer subsystem gates on RGB channel divergence and skips the VLM entirely on IR frames, falling back to classifier-label-only summaries.

**The classify-prompt inconsistency finding**:

Across 20 daytime frames, Moondream's `classify` prompt (one-word output) disagreed with its own `describe` prompt (free text) on 5 frames — ~25% self-contradiction rate. Example: a frame with only a flagpole visible got `classify="person"` despite the describe text mentioning no person. **Moondream's classify prompt is unreliable even in daylight** and is NOT used as a label source. Only the describe prompt is used, and only for summaries.

**The YOLOX silent-fallback asymmetry**:

On the same 20 daytime frames, YOLOX-S also produced ~3 sub-threshold false positives (`person 0.26`, `person 0.16`, `vehicle 0.21` on empty backyard scenes). These would normally be "silent fallback to Motion at X" under the confidence threshold, hiding the errors from users. At the raw-prediction layer, **YOLOX and Moondream have comparable error rates** — YOLOX's advantage is design (silent fallback), not quality.

### Architectural consequence — two models for two UX surfaces

| Feature | Model | Prompt / layer | Source of truth |
|---|---|---|---|
| **Per-event Inbox labels** ("Person at Front door") | YOLOX-S | Detection + label collapse + median-of-track scoring | Classifier only — Moondream's classify prompt is unreliable |
| **Daily activity summaries** ("Yesterday: a black car arrived at 7:14...") | Moondream 2B 4-bit | `describe` prompt, daylight frames only | Summarizer only — YOLOX cannot produce natural language |

Neither model "wins" — they serve different product surfaces. Both are shipped on capable hardware; the classifier is always on, the summarizer runs once a day.

---

## Scale and cost — the "1 event per second" question

A busy-street camera can fire raw motion events at ~1/sec. Understanding why this is NOT the billable unit is load-bearing for any subscription-tier design.

### Three layers of "event"

| Layer | Source | Frequency on busy camera | What consumes it |
|---|---|---|---|
| Raw motion trigger | MOG2 pixel-mask contour | ~1/sec while something is moving | Tracker only |
| Tracked event | IOU-based track after ≥5 consecutive frames | ~1/minute typical | Classifier |
| User-visible event (Inbox row) | Tracked event ends / enters zone / changes class | ~1/5-min typical | User, persistence, WebSocket bus |

**Cost-per-event budgets must use the tracked-event rate, not the raw-trigger rate.** Any design that bills per raw motion frame is economically broken. a reference project bills per tracked event, which is why their a reference project+ service is economically viable.

### Local YOLOX — cost is free, throughput is the question

YOLOX-S at 30 ms warm = 33 inferences/second per worker. 32 cameras worst-case simultaneous firing = 960 ms/sec of compute = saturated but survivable via the bounded queue. In practice after tracker debounce, ≤5-20 tracked events/hour per camera, well under 1% of worker capacity.

### Hypothetical cloud LLM tier — the math

At raw motion frames (wrong unit):

| Service | Per-image | 1 camera busy | 5 cameras busy |
|---|---|---|---|
| Gemini Flash | $0.00002 | $52/mo | $260/mo |
| Claude Haiku | $0.0005 | $1,300/mo | $6,500/mo |
| GPT-4o-mini | $0.001 | $2,600/mo | $13,000/mo |
| Claude Sonnet | $0.005 | $13,000/mo | $65,000/mo |

**These numbers are what you get if you bypass the tracker. Nobody ships this.**

At tracked events (right unit, ~300 inferences/day typical 5-cam home):

| Service | Per-image | 5 cameras typical | Subscription headroom at $5/mo |
|---|---|---|---|
| Gemini Flash | $0.00002 | $0.18/mo | 97% margin |
| Claude Haiku | $0.0005 | $4.50/mo | 10% margin |
| GPT-4o-mini | $0.001 | $9/mo | needs $12/mo tier |
| Claude Sonnet | $0.005 | $45/mo | needs $60/mo tier |

A $5/mo tier is economically viable at Gemini Flash pricing and tight at Haiku pricing. **This is a product decision, not a technical one, and it conflicts with `product.md`'s "no cloud, no subscriptions" stance.** See `product.md` before touching this.

The local Moondream 0.5B path sidesteps all of this — no cloud, no subscription, no marginal cost, but requires a user-elected ~500 MB download and only works on "Strong" tier hardware.

---

## v1 scope (what ships first)

1. **OpenCV foundation**: MOG2 background subtraction + contour-tightened motion bboxes + optional CLAHE preprocessing for IR frames. All BSD-3, all pure code, ~100 lines augmenting `backend/motion/detector.py`.

2. **Tracker**: IOU-based track assignment, ~100 lines pure Python in a new `backend/motion/tracker.py`. Emits `tracked_event` records into an async queue.

3. **YOLOX-S classifier**: ~350 lines across `backend/classification/classifier.py` and `manager.py`. One shared ORT session, EP selection at startup, bounded async queue, median-of-track scoring, silent confidence fallback. Bundled YOLOX-S ONNX from Megvii release 0.1.1rc0, ~34 MB, Apache-2.0.

4. **Hardware calibration**: `backend/classification/hardware_probe.py`, ~50 lines. Runs once on first launch, writes `settings.classification_tier`.

5. **Inbox surface upgrade**: ~20 lines edit to `motionEventToInboxEvent()` in `frontend/src/components/Inbox.tsx`.

6. **Snapdragon build pipeline**: a CI step on the x86_64 runner that produces the pre-quantized YOLOX-S + QNN context binary for the Windows ARM64 installer. Not code, but build infrastructure work.

**Total new code**: ~700 lines of Python + ~20 lines of TypeScript + ~34 MB bundled model + CI step. Zero new Rust, zero new ffmpeg pipes, zero new RTSP connections.

---

## v1.1 polish (after v1 is stable)

- **YAMNet audio classification** (Apache-2.0, tensorflow/models). *"SimpleNVR listens too"* — a new product axis that complements the nighttime detection limitation. `bark`, `glass_break`, `gunshot`, `siren`, `car_horn` from the AudioSet ontology. The ffmpeg pipeline already demuxes audio; this is architecturally symmetric with the scene-change JPEG fan-out and reuses the same `frame_broadcaster` pattern.

- **CLAHE toggle** for cameras where the preprocessing helps more than it hurts, determined by the calibration pass.

- **Object mask UI** — user-drawn rectangles to exclude specific screen regions from any detection. a reference project's final-resort fix for persistent false positives that MOG2 background subtraction doesn't catch. Ship after v1 proves stable.

---

## v2 (later)

- **Package detection via YOLOX fine-tuned on Open Images V7**. The `Box` class exists in Open Images (annotations CC-BY 4.0 Google, images CC-BY 2.0 Flickr — commercial fine-tuning and shipping the weights is permitted). Cloud L4/A10 fine-tune, ~$50–$200, ~1–2 weeks work. COCO has no package class; this is the only license-clean path that doesn't route through a reference project+'s closed model.

- **Delivery logo second-stage classifier** (Amazon / UPS / FedEx / DHL) — a reference project+'s differentiating feature. A tiny MobileNet/EfficientNet-Lite on ~300×300 crops of detected packages. User-facing sentence: *"Amazon package at Front door."*

- **Moondream 0.5B as an optional downloadable tier** for Strong-hardware users. Unlocks rich descriptions, natural-language search across events, and end-of-day summaries. Requires a deliberate product.md edit (opt-in, on-device, zero cloud).

---

## Out of scope (permanently)

- Face recognition
- License-plate reading
- Cross-camera person tracking
- Behavior analysis
- Cloud-hosted inference as a default
- Any feature that requires typing settings beyond a single on/off toggle

---

## Experimental artifacts on disk

All in `experiments/object-detection/`:

- `yolox_nano.onnx` (3.5 MB) — Megvii release 0.1.1rc0, Apache-2.0
- `yolox_s.onnx` (34 MB) — Megvii release 0.1.1rc0, Apache-2.0
- `run_yolox.py` — Nano baseline on motion thumbnails
- `run_yolox_s.py` — YOLOX-S at 640 on full-res frames
- `a reference project_style.py` — 4 tile strategies + label collapse
- `run_event_frames.py` — extracts exact-timestamp frames from recordings and runs all strategies
- `event_frames/` — 8 real motion-event frames for regression testing
- `full_frames/` — full-res frames for wide-angle experiments

Re-running the experiment when daytime recordings are available is the remaining empirical step — expect confident ≥0.60 person/vehicle detections on daytime frames via a reference project-style motion-blob crops, validating the full pipeline end-to-end before code is written in `backend/classification/`.
