# Substream codec awareness

> **Picking this up cold?** Read this file top-to-bottom. The load-bearing product rule is in §Architecture: **every camera we set up must produce playable recordings, on main AND on sub, regardless of the codec the camera exposes.** Quality degradation (lower resolution, lower bitrate) is acceptable. Silent black recordings are not.

## Context

On 2026-04-17 we shipped tiered DHCP-rebind reconciliation (`mac-fallback-ip-change-plan.md`). During live validation we hit an unrelated, pre-existing bug on Ben's TP-Link Tapo C120 street camera: the chronic-failure breaker had downgraded the recorder from main to sub-stream, and every subsequent segment rendered as **black** in the browser playback pipeline. Motion detection kept firing (detect-role ffmpeg runs on a separate consumer of the main stream via go2rtc), live preview kept working (libmpv on main), only the recorded files were broken.

Root cause, from the running ffmpeg process list + live RTSP probes:

- The Tapo C120 exposes **three** ONVIF profiles: `/stream1` (H.264 2560×1440 main), `/stream2` (H.264 640×360 sub), and `/stream8` (MJPEG 640×360 "preview"/snapshot profile).
- Our ONVIF profile picker (`backend/discovery/onvif_client.py:158-173`) selects the sub-stream by **pixel area only**: smallest-area profile that isn't main.
- `/stream2` and `/stream8` are **tied on area** (both 640×360). Tie-break is ONVIF response order. Tapo returned `/stream8` first. We wrote `substream_uri='rtsp://10.0.0.63:554/stream8'` into the DB — an MJPEG stream.
- When the breaker tripped `recording_stream_override='sub'` on this camera, the recorder's ffmpeg stream-copied MJPEG into a fragmented MP4 container with `-movflags +frag_keyframe+empty_moov+default_base_moof` — flags designed for H.264 GOP structure. Every MJPEG frame is a keyframe, so `+frag_keyframe` produced a fragment per frame (1800 fragments in a 60 s segment). Players render that combination as black / refuse to decode.

"If it happened to me, it can happen to anyone." Tapo is a top-five consumer IP-camera brand. Every household buying a C100/C110/C120/C210 series camera is one chronic-failure breaker trip away from this bug. We fix it structurally.

## Architecture

**Two independent fixes, both ship in this plan:**

| # | Fix | Where | Intent |
|---|-----|-------|--------|
| 1 | Codec-preferring profile selection | `backend/discovery/onvif_client.py` | Never pick an MJPEG profile as "sub" when an H.264/H.265 alternative exists |
| 2 | Codec-aware recording | `backend/recording/camera_recorder.py` + `backend/recording/codec.py` | When the only available stream is MJPEG (no H.264 alternative on this camera), transcode to H.264 at record time so recordings are playable |

Fix 1 handles the 99%+ case: cameras with a real H.264 sub alongside an MJPEG preview. Discovery picks the H.264 sub. Fix 2 handles the <1% edge: genuinely MJPEG-only cameras (very old IP cameras, certain cheap brands' bargain-bin models). Transcoding a 640×360 MJPEG sub to H.264 with a hardware encoder costs <5% CPU per camera and keeps all downstream code (container format, fragmented MP4, playback in hls.js / native HLS / mpv) untouched.

### The rule (load-bearing)

**Every camera SimpleNVR ever records must produce playable segment files.** This is a product commitment, not an engineering preference. The NVR's entire value proposition is "you can review what happened." A black recording is a silent failure mode worse than no recording at all — the user looks for evidence, finds nothing visible, loses trust. Cameras that physically cannot produce an H.264/H.265 stream at any profile must be transcoded; we never ship a pathway that produces unplayable .mp4 files.

This rule is the dual of the reconcile plan's "auth is a verifier, not a matcher" — both are constraints that the obvious engineering shortcut violates. Here the shortcut is "let ffmpeg copy any codec the camera emits." The shortcut is wrong because the downstream assumption (fragmented-MP4 + H.264 playback) is load-bearing.

### Fix 1: Codec-preferring profile selection

**Current (`onvif_client.py:116-173`):** `profile_areas: list[tuple[int, object]]` = `(area, profile)`. Main = max area. Sub = min area among non-main.

**Change:** capture codec alongside area. Rank candidates by `(codec_rank, area)` with:

```python
def _codec_rank(enc) -> int:
    c = (getattr(enc, "Encoding", None) or "").upper()
    if c in ("H265", "HEVC"): return 0
    if c == "H264":          return 1
    return 2  # MJPEG, H.263, anything else — allowed but dispreferred
```

- **Main:** largest area among `_codec_rank <= 1` profiles. If none (MJPEG-only camera), fall through to largest area overall — Fix 2 handles the recording side.
- **Sub:** smallest area among `_codec_rank <= 1` profiles that aren't main. If none, smallest area overall.

Codec is captured into DB (new `substream_codec` and `rtsp_codec` columns — see §Schema) so Fix 2 doesn't have to re-probe at recorder spawn.

### Fix 2: Codec-aware recording

**Current (`codec.py:build_unified_cmd`):** one ffmpeg command template regardless of input codec. `-c copy` with H.264-shaped movflags.

**Change:** at recorder spawn, read the codec from the camera row (`rtsp_codec` for main, `substream_codec` for sub, whichever stream we're about to use). Branch the command construction:

- **H.264 / H.265 input:** stream-copy with current movflags. Zero change.
- **MJPEG input:** transcode to H.264 via the platform hardware encoder (`select_encoder` already returns `h264_videotoolbox` / `h264_mf` / `h264_vaapi`). Keep current movflags — the ffmpeg output is now H.264, so they work correctly.
- **MJPEG input on a platform with no hardware encoder available:** refuse to populate the recorder. Emit a `camera_health = "unsupported_codec"` event (new state) and log at ERROR. Do not silently produce broken recordings. Covered in §Known limitations — currently only affects bare-metal Linux without a GPU, which isn't a ship target.

**Transcode flags (MJPEG → H.264 via hardware encoder):**

```
-c:v h264_videotoolbox -b:v 2000k -profile:v high -g 60
```

- Bitrate ~2 Mbps is appropriate for 640×360 sub-stream playback-quality output. User never configures this — it's a fallback path by definition.
- GOP 60 (= segment duration in seconds × fps = 60 × 1 if we end up at low fps; adjust per `recording_fps` setting) matches our segment boundaries, so segment cuts land on keyframes.
- No audio changes — audio is handled by a separate ffmpeg.

## Schema

Two new nullable columns on `cameras`:

```sql
ALTER TABLE cameras ADD COLUMN rtsp_codec TEXT;        -- "H264", "H265", "MJPEG", NULL = unknown
ALTER TABLE cameras ADD COLUMN substream_codec TEXT;   -- same
```

Both default to NULL. Populated by `interrogate_camera` on every new interrogation. A NULL value means "pre-fix-1 row that hasn't been re-interrogated yet" — handled by the startup migration sweep below.

## Remediation for existing rows

Three paths, pick per install state:

1. **Startup sweep (baked in, runs once).** On app start, `main.py` fires a one-shot task: for every camera with `substream_codec IS NULL`, invoke `interrogate_camera` with the stored credentials. This fills in codec columns and, if Fix 1's selector now picks a different profile, updates `rtsp_uri` / `substream_uri` in-place. Emit `camera_updated` so the recorder picks up the new URIs. Cost: one ONVIF roundtrip per camera on first boot post-upgrade; thereafter free (columns populated).

2. **Manual API: `POST /cameras/{id}/re-interrogate`.** User-initiated. For cases where the startup sweep ran but hit transient ONVIF failure. Same mechanism as `retry-main-stream` but re-probes profiles instead of clearing the fallback.

3. **Implicit: credentials re-submit.** Already exists (`scanner.py:1133`). Any user who re-enters credentials triggers re-interrogation; they get the fix for free.

For Ben's Street camera (row `c7b0241c-dea1-4cb3-88cc-2a8aa2fe9051`) specifically: after the fix lands, restart the app. Startup sweep runs. Tapo returns the same three profiles, but Fix 1 now picks `/stream2` (H.264 640×360) as sub. `substream_codec='H264'`. Breaker trip no longer produces black recordings.

## Files to modify

**Backend:**
- `backend/discovery/onvif_client.py` — Fix 1 logic. `CameraInfo` gets `rtsp_codec: str | None`, `substream_codec: str | None` fields. Profile-walk updated to capture + rank by codec.
- `backend/db.py` — schema migration for the two new columns (use the existing `_migrate_add_column` helper). `insert_camera` / `update_camera` write the new fields.
- `backend/models.py` — `Camera` pydantic model gets `rtsp_codec` and `substream_codec` optional strings.
- `backend/discovery/scanner.py` — propagate codec fields through the camera-write paths (the two spots at `scanner.py:229` and `scanner.py:457`).
- `backend/main.py` — startup sweep (see §Remediation path 1).
- `backend/api/cameras.py` — new `POST /cameras/{id}/re-interrogate` endpoint (see §Remediation path 2).
- `backend/recording/camera_recorder.py` — read codec from camera row at spawn; pass to `build_unified_cmd`.
- `backend/recording/codec.py` — `build_unified_cmd` gains a `codec` parameter; MJPEG branch produces the transcode pipeline.

**Tests:**
- `tests/test_profile_selection.py` — new. Table-driven tests for `_codec_rank` + profile-ranking given synthetic ONVIF profile lists.
- `tests/test_codec_aware_build.py` — new. `build_unified_cmd(codec="H264")` produces the stream-copy line; `codec="MJPEG"` with hardware encoder present produces the transcode line; `codec="MJPEG"` with no hardware encoder raises.

**Docs:**
- `docs/architecture.md` — add a paragraph to the Discovery section explaining the codec-preference rule; add a paragraph to the Recording section explaining the codec-aware muxer branch. Both belong inline next to their subsystems; don't duplicate this plan.

**Memory index:** MEMORY.md entry for "Substream codec awareness shipped" once landed.

## Pseudocode

### `onvif_client.py` — profile selection

```python
def _codec_rank(enc) -> int:
    c = (getattr(enc, "Encoding", None) or "").upper()
    if c in ("H265", "HEVC"): return 0
    if c == "H264":          return 1
    return 2

# During profile walk, capture (area, encoder_config, profile):
profile_areas: list[tuple[int, object, object]] = []
for profile in profiles:
    enc = profile.VideoEncoderConfiguration
    w = int(enc.Resolution.Width) if enc and enc.Resolution else 0
    h = int(enc.Resolution.Height) if enc and enc.Resolution else 0
    profile_areas.append((w * h, enc, profile))

# Main: largest area among codec-preferred; fallback to largest area overall.
preferred = [pa for pa in profile_areas if _codec_rank(pa[1]) <= 1 and pa[0] > 0]
if preferred:
    preferred.sort(key=lambda pa: -pa[0])
    main_tuple = preferred[0]
else:
    sized = [pa for pa in profile_areas if pa[0] > 0]
    main_tuple = max(sized, key=lambda pa: pa[0]) if sized else (0, None, profiles[0])

main_profile = main_tuple[2]
info.rtsp_codec = (getattr(main_tuple[1], "Encoding", None) or "").upper() or None

# Sub: codec-preferred wins over smaller area; ties on codec break to smaller area.
sub_candidates = [pa for pa in profile_areas if pa[2] is not main_profile]
sub_candidates.sort(key=lambda pa: (_codec_rank(pa[1]), pa[0]))
if sub_candidates:
    sub_tuple = sub_candidates[0]
    info.substream_codec = (getattr(sub_tuple[1], "Encoding", None) or "").upper() or None
    # ... GetStreamUri on sub_tuple[2].token as before
```

### `codec.py` — codec-aware command

```python
def build_unified_cmd(
    *,
    rtsp_uri: str,
    output_pattern: str,
    segment_secs: int,
    fps_setting: str,
    encoder: str | None,
    encoder_flags: list[str] | None,
    codec: str | None,  # "H264", "H265", "MJPEG", or None (trust stream-copy)
) -> list[str]:
    transcode_needed = (codec or "").upper() == "MJPEG"

    if transcode_needed:
        if not encoder:
            raise RuntimeError(
                "MJPEG source requires a hardware encoder to produce playable "
                "recordings; none available on this platform. Refusing to spawn."
            )
        video_args = [
            "-c:v", encoder, *encoder_flags,
            "-b:v", "2000k",
            "-profile:v", "high",
            "-g", "60",
        ]
    else:
        video_args = ["-c:v", "copy"]

    # ... rest of command (unchanged): input args, segment flags, output_pattern
```

### Recorder spawn — pass codec through

```python
# In camera_recorder.py _spawn, after picking input_uri:
stream_codec = (
    self.camera.substream_codec if use_sub else self.camera.rtsp_codec
)
cmd = build_unified_cmd(
    rtsp_uri=input_uri,
    # ... existing args ...
    codec=stream_codec,
)
```

## Tests

Decision-boundary unit tests, no live cameras required:

1. **`test_profile_selection.py::test_prefers_h264_over_mjpeg_on_tie`** — synthetic Tapo-like profile list with one 640×360 H.264 and one 640×360 MJPEG; asserts H.264 is picked as sub.
2. **`test_profile_selection.py::test_prefers_h265_over_h264`** — synthetic Axis-like list with H.265 sub + H.264 sub at same res; H.265 wins.
3. **`test_profile_selection.py::test_mjpeg_only_camera_picks_mjpeg`** — one profile, MJPEG only; picked as main, sub is None.
4. **`test_profile_selection.py::test_mjpeg_plus_h264_sub_rank_inversion`** — regression guard: a small MJPEG + a larger H.264 at the sub slot; H.264 wins on codec rank *despite* larger area. (Codec rank is the primary sort, not a tiebreaker.)
5. **`test_codec_aware_build.py::test_h264_copy`** — `build_unified_cmd(codec="H264")` contains `-c:v copy` and no transcode flags.
6. **`test_codec_aware_build.py::test_mjpeg_transcodes`** — `build_unified_cmd(codec="MJPEG", encoder="h264_videotoolbox")` contains the encoder + `-b:v 2000k`, not `-c:v copy`.
7. **`test_codec_aware_build.py::test_mjpeg_no_encoder_raises`** — `build_unified_cmd(codec="MJPEG", encoder=None)` raises `RuntimeError`.

## Verification

Live LAN, against Ben's 5 cameras (Eufy 10.0.0.9, Reolinks 10.0.0.13/14, Tapos 10.0.0.46/63):

1. **Pre-flight:** DB state before app restart. Note current `substream_uri` + `substream_codec` (NULL for all — pre-migration).
2. **Restart app.** Startup sweep re-interrogates. After sweep completes: every camera should have `substream_codec IN ('H264','H265')` (we expect all 5 LAN cameras to have H.264 sub-streams).
3. **Street Tapo (10.0.0.63) specifically:** `substream_uri` should change from `rtsp://10.0.0.63:554/stream8` to `rtsp://10.0.0.63:554/stream2`. Confirm via `sqlite3` query.
4. **Trigger breaker trip on Street Tapo.** Force main-stream failure (e.g., kill go2rtc briefly, or temporarily block 10.0.0.63:554 with `pfctl`). Wait for `CIRCUIT_BREAKER_THRESHOLD` restarts to flip to sub.
5. **Play a post-trip segment** from `.../c7b0241c.../2026-04-17/HH-MM-SS.mp4` in both mpv directly and the in-app Browse Footage view. Expect: actual video, not black.
6. **Clear the override** via `POST /cameras/{id}/retry-main-stream`. Recorder flips back to main. Continuity preserved.

For the MJPEG-only transcode path (Fix 2), we don't have a native test camera. Synthetic test:

7. **ffmpeg-synthesized MJPEG RTSP source** via `ffmpeg -re -i <test pattern> -c:v mjpeg -f rtsp rtsp://127.0.0.1:8554/mjpegtest`. Add to go2rtc config. Point a fake camera row at it with `substream_codec='MJPEG'`. Confirm: segments land on disk, encoded as H.264, play correctly in mpv and the in-app player.

## Known limitations

- **Bare-metal Linux without a GPU and MJPEG-only camera:** `build_unified_cmd` raises `UnsupportedSourceCodecError`, the recorder refuses to spawn, `camera_health = "unsupported_codec"` surfaces in UI. Not a shipping platform today (deployment target is Windows ARM + macOS dev; Linux is developer-only). If Linux ships later and encounters this combo, the fix is to allow libx264 transcode **in the non-bundled Linux build only** (GPL concerns don't apply to a distribution the user compiles themselves) — out of scope for this plan.
- **All-None-encoder profile list:** a pathological camera that returns every `VideoEncoderConfiguration` as None (observed on a tiny subset of very old ONVIF stacks) gives the selector no codec or area signal to rank on. `select_main_and_sub` falls back to ONVIF response order: first profile becomes main, second becomes sub. Downstream `GetStreamUri` either succeeds (and the recorder stream-copies; `source_codec=None` → stream-copy branch) or fails, at which point the scanner logs and moves on. Non-fatal; leaves the camera in the same shape it would have been with the old area-only selector.
- **H.263 / WebM-only cameras:** `_codec_rank` returns 2 for these. Fix 1 treats them the same as MJPEG (dispreferred but accepted); Fix 2 refuses to transcode them (no path in `select_encoder` for H.263 → H.264 decode, and the MJPEG transcode branch only fires on `codec == "MJPEG"`). Not a real-world case for any camera sold in the last decade.
- **Camera exposes multiple H.264 profiles with bad ones:** e.g., a malformed H.264 stream that plays in libmpv but not in hls.js. Out of scope — that's a per-camera compat issue, not a codec-selection issue.
- **Re-interrogation during a lease rebind + codec-change sequence:** if a camera firmware-updates and starts exposing different profiles, the reconcile path already re-interrogates (`scanner.py:197`), so codec changes are picked up automatically.

### Pre-ship remediation note

This plan ships lazy: no startup sweep, no `POST /cameras/{id}/re-interrogate` endpoint, no proactive remediation of existing DB rows. That is acceptable while Ben is the only user (his single affected row is fixed by a one-shot manual SQL UPDATE, see §Remediation). **Before the first public release**, reinstate an equivalent self-healing trigger for users who already have a wrong `substream_uri` in their DB — either the startup sweep described in §Remediation path 1, or an on-first-recorder-spawn codec probe that updates the row if the stored codec disagrees with the live stream. Without that, every user who migrates in with an MJPEG `substream_uri` gets the black-recording bug once the chronic-failure breaker trips, even on the fixed build.

## Out of scope (deliberate)

- **Changing the camera's firmware behavior.** Tapo will keep exposing `/stream8` as an MJPEG profile. We adapt; the camera does not.
- **Full rearchitecture of the fragmented-MP4 pipeline.** Transcoding MJPEG to H.264 keeps every downstream assumption intact (hls.js, native HLS, mpv, Browse Footage). Changing container formats (MKV, MOV) or fragmentation strategy (time-based frag for MJPEG stream-copy) is a separate, larger project that we don't need.
- **Auto-retry-main integration.** Covered by `plans/auto-retry-main-plan.md`. Orthogonal — this plan handles codec correctness; that plan handles recovery cadence. Ship independently.
- **UI surfacing of codec information.** The user doesn't see codecs in the Camera Setup screen. If they ever do, it's a separate UX change driven by a use case we don't have today.

## UX touch

**None required.** The fix is invisible from the user's perspective — which is the point. Users never learn that their camera has multiple profiles, never see a "which sub-stream?" picker, never get a codec-chooser dialog. They just see: recording works, scrubbing works, the chronic-failure breaker still downgrades to sub under stress without producing black files.

The only surface a user might notice is a one-time "Interrogating cameras..." moment on first boot post-upgrade (startup sweep). If it takes more than 5 seconds, we surface a subtle toast; otherwise silent.

## Resume prompt for fresh context

> Read `/Users/benjamincates/Dev/simplenvr/plans/substream-codec-aware-plan.md` top-to-bottom first. The load-bearing rule is in §Architecture: **every camera we record must produce playable segments, regardless of codec.** Ben hit this on his street Tapo C120 on 2026-04-17: discovery picked an MJPEG "preview" profile as the sub-stream (`/stream8`), breaker tripped the recorder to sub, and every segment rendered black. Fix 1 (codec-preferring profile selection) handles the common case; Fix 2 (codec-aware recording with transcode for MJPEG) handles the edge of genuinely MJPEG-only cameras. Two new DB columns, ~60 lines of Python total, ~100 lines of tests. Verification: 5 live LAN cameras + one synthetic MJPEG source.
