"""MotionDetector — per-camera detection pipeline.

Owns one `DetectFfmpegSource` (low-fps RGB24 decoder off the go2rtc
loopback), a `NewestFrameSlot`, a `ByteTracker`, a `HeatmapLayer`, and
per-track `TrackConfidence` state.

Per-frame flow:

  1. Grab newest RGB frame from the slot.
  2. Grayscale + running-average background subtraction → foreground
     mask → connected components → motion-proposal bboxes.
     Scene-change guard (global mean |frame - B|) freezes the background
     on IR-cut/exposure jumps so they don't flood the detector with
     full-frame motion.
  3. If no proposals: tracker.update([]) so existing tracks coast; no
     D-FINE inference (motion-gated).
  4. If proposals: run D-FINE on the full BGR frame at 640 letterbox;
     feed detections to ByteTrack.
  5. Per confirmed track, FP filters 1–5:
       L1 geometric (size/aspect/frame-area)
       L2 min score
       L3 init-delay (track.age >= 3)
       L4 Bayesian Beta confidence
       L5 recoverable-doubt state machine
     Layer 8 (false-alarm heatmap) multiplies p_hat when the grid has
     enough samples; layers 6/7 (movement gate, stationary NCC) arrive
     in a later pass per plan §10.
  6. Best emittable track drives the event lifecycle:
       open  — first emittable track → motion_events row + thumbnail +
               motion_started WS event + initial classification.
       update — class/p_hat improves → update_motion_event_classification
                + motion_event_updated WS event.
       close — no emittable track for MOTION_DEBOUNCE_SECONDS → ended_at
               + motion_ended WS + spawn create_motion_clip.

Preserves the wire contracts documented in plan §8: DB schema, WS event
payloads, clip.py spawn site.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import cv2
import numpy as np
from scipy import ndimage

from dataclasses import dataclass

from .. import db
from ..classification.dfine import DFineDetector, Detection
from ..classification.labelmap import collapse as collapse_coco_label
from ..config import MOTION_DEBOUNCE_SECONDS, MOTION_THUMBNAILS_DIR
from ..detect_frames.ffmpeg_source import DetectFfmpegSource
from ..detect_frames.shm_ring import NewestFrameSlot
from .clip import create_motion_clip
from .confidence import TrackConfidence
from .heatmap import HeatmapLayer
from .tracker import ByteTracker, Detection as TrackerDetection, Track


@dataclass(slots=True)
class _ParkingSpot:
    """A learned stationary-vehicle region.

    - `bbox` is (x1,y1,x2,y2) in detect-frame pixel coords.
    - `template` is a 128×96 grayscale canonicalization of the crop
      captured when the spot was learned. Fixed size so memory is flat
      against bbox size.
    - `last_match_frame` is the frame counter value at the most recent
      NCC-accepted match (initialized to the promotion frame). Drives
      LRU eviction when the spot cap is hit.
    """
    bbox: tuple[int, int, int, int]
    template: np.ndarray
    last_match_frame: int


@dataclass(slots=True)
class _ParkingSpotCandidate:
    """Live candidate — held per-track while a vehicle exceeds the
    learn-threshold age. Promoted to a permanent spot when the track
    finally prunes from the confidence/observation dicts."""
    bbox: tuple[int, int, int, int]
    template: np.ndarray

if TYPE_CHECKING:
    import aiosqlite

    from ..api.ws import EventBus
    from ..models import Camera

logger = logging.getLogger(__name__)

# Per-frame detection trace is opt-in. Cranking this on for 32 cameras at
# 2 fps puts 64 INFO lines/s into the log; useful when Ben is debugging a
# missed detection, noise otherwise. Set SIMPLENVR_DETECT_TRACE=1 to
# enable; when enabled, the trace is emitted at DEBUG level (readers
# need log_level=DEBUG too). Evaluated at import time because the gate
# fires every non-idle frame and env lookups add up.
_DETECT_TRACE_ENABLED: bool = os.environ.get("SIMPLENVR_DETECT_TRACE") == "1"

# ---------------------------------------------------------------------------
# Background subtraction + scene-change guard
# ---------------------------------------------------------------------------

# Running-average learning rates. Idle is slow so a briefly-paused object
# doesn't bleed into the background; motion-spike bumps to 0.1 so after
# the scene-change guard unfreezes we re-stabilize fast.
_BG_ALPHA_IDLE = 0.02
_BG_ALPHA_SPIKE = 0.10

# Foreground threshold on |frame - B| (uint8 diff).
_FG_THRESHOLD = 25

# Minimum connected-component area (pixels in the 640-wide detect frame).
_MIN_COMPONENT_AREA = 400

# Scene-change guard — if this frame's global |frame - B| jumps > 3 sigma
# above the EWMA of recent changes, freeze B and drop proposals for this
# frame (prevents IR-cut / auto-exposure flips from spawning an event).
_SCENE_CHANGE_SIGMA = 3.0
_SCENE_CHANGE_EWMA_ALPHA = 0.1   # how fast the rolling baseline tracks
_SCENE_CHANGE_MIN_SAMPLES = 5    # before which we don't trust sigma

# Morph kernel for noise cleanup on the foreground mask.
_MORPH_KERNEL = np.ones((3, 3), dtype=np.uint8)

# ---------------------------------------------------------------------------
# FP filter thresholds (plan §6 Layers 1–3; 4 and 5 live in confidence.py)
# ---------------------------------------------------------------------------

# L1 — Geometric
_L1_MIN_DIM_PX = 8
_L1_ASPECT_MIN = 0.1
_L1_ASPECT_MAX = 10.0
_L1_MAX_FRAME_FRACTION = 0.7

# L2 — Min score (complements the tracker's spawn threshold)
_L2_MIN_SCORE = 0.3

# L3 — Init delay (frames of consecutive matches before we emit). At 2 fps
# detector cadence, 2 frames = 1 s — enough to kill single-frame transients
# without gating real events that are only visible briefly (a car passing
# across the camera's FOV in ~2 s).
_L3_MIN_AGE = 2

# Audio-vision fusion (plan §7). When the audio pipeline fires a
# high-priority label (glass break, gunshot, scream, siren), the motion
# pipeline boosts frame rate and relaxes the D-FINE score threshold so
# partial detections that correlate with the audio spike are more likely
# to latch a track. The window is short by design — 5 s of aggressive
# search, then back to steady-state.
_AUDIO_BOOST_WINDOW_S = 5.0
_AUDIO_BOOST_FPS = 5
_AUDIO_BOOST_SCORE_THRESHOLD = 0.25
_STEADY_FPS = 2
_STEADY_SCORE_THRESHOLD = 0.4

# L6 — Movement gate. A track whose center has moved less than
# `1 * bbox_height` total over its lifetime is stationary and gets
# dropped — kills parked cars, the Kamado Joe, sculptures, furniture.
#
# Person carveout (plan §6 Layer 6 + project_dfine_fp_observations):
# a stationary track classified as `person` may emit after
# `track.age >= _MOVEMENT_PERSON_AGE_FRAMES` frames. At 2 fps detector
# cadence that's 10 s of standing — catches a real human standing in
# the frame while leaving the heatmap (Layer 8) to learn suppression
# for stationary objects consistently mis-labeled as "person".
_MOVEMENT_PERSON_AGE_FRAMES = 20       # 10 s at 2 fps
_MOVEMENT_DIST_FRACTION_OF_H = 1.0     # move at least 1x bbox height

# L7 — Stationary NCC classifier (vehicles only).
# A car that's been present ≥ _STATIONARY_LEARN_FRAMES frames (5 min at
# 2 fps) becomes a learned "parking spot." Future vehicle tracks whose
# bbox overlaps that spot and whose crop matches its stored reference
# crop at or above _STATIONARY_NCC_THRESHOLD are silently dropped.
# Catches the same car re-parking in its slot (daily commuter) and the
# camera-drift case where a subtle pan shifts a parked car's bbox.
_STATIONARY_LEARN_FRAMES = 600
_STATIONARY_NCC_THRESHOLD = 0.9
_STATIONARY_IOU_GATE = 0.3
# Reference crops are canonicalized to a small grayscale template before
# storage. Keeps memory flat against bbox size (a close-up pickup vs a
# distant sedan both cost the same) and stays far below the resolution
# where NCC can't distinguish "same car in same slot" from "different
# car in same slot."
#   128 × 96 × 1 byte = 12 KB per spot
#
# Cap + eviction for a *busy SMB parking lot* (100+ arrivals/day):
#   FIFO at a tiny cap (original 16) thrashes — every 17th arrival
#   evicts a spot we may still be actively using. Instead we cap at
#   256 and evict the spot with the OLDEST last_match_frame (LRU).
#   Semantics: regularly-returning employees keep their spot; a
#   one-time visitor from three weeks ago falls off first.
#
# Budget under this cap:
#   256 spots × 12 KB  = ~3 MB per camera
#   × 32 cameras       = ~96 MB across the whole system, worst case.
# Fine on any v1 target (X Elite has 16+ GB).
#
# In-memory only. A detector restart re-learns spots over the first 5
# minutes of runtime — acceptable given how rarely the sidecar cycles.
_STATIONARY_TEMPLATE_W = 128
_STATIONARY_TEMPLATE_H = 96
_MAX_PARKING_SPOTS_PER_CAMERA = 256

# ---------------------------------------------------------------------------
# Per-track confidence pruning
# ---------------------------------------------------------------------------

# Drop per-track TrackConfidence entries that haven't been observed in this
# many frames. ByteTracker's own TRACK_BUFFER is 6 frames (3 s at 2 fps);
# giving the confidence dict a bit more slack is cheap and avoids edge
# races where a track re-emerges just outside the buffer window.
_CONFIDENCE_PRUNE_FRAMES = 30


def _draw_track_box(bgr: np.ndarray, track: Track, label: str) -> np.ndarray:
    """Return a copy of the BGR frame with the track's bbox drawn."""
    out = bgr.copy()
    x1, y1, x2, y2 = (int(track.x1), int(track.y1), int(track.x2), int(track.y2))
    cv2.rectangle(out, (x1, y1), (x2, y2), (0, 255, 0), 2)
    (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
    cv2.rectangle(out, (x1, max(y1 - th - 4, 0)), (x1 + tw, y1), (0, 255, 0), -1)
    cv2.putText(
        out, label, (x1, max(y1 - 2, 10)),
        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1,
    )
    return out


class MotionDetector:
    """Per-camera detection pipeline. One instance per live camera.

    Lifecycle is owned by `MotionManager`; it calls `start()` when a
    recorder comes up and `stop()` when the recorder stops or the camera
    disappears.
    """

    def __init__(
        self,
        *,
        camera: "Camera",
        conn: "aiosqlite.Connection",
        event_bus: "EventBus",
        dfine: DFineDetector,
        heatmap_conn: sqlite3.Connection,
        rtsp_url: str,
    ) -> None:
        self.camera = camera
        self._conn = conn
        self._event_bus = event_bus
        self._dfine = dfine
        self._rtsp_url = rtsp_url

        # Frame source.
        self._slot = NewestFrameSlot()
        self._source = DetectFfmpegSource(
            camera_id=camera.id,
            rtsp_url=rtsp_url,
            slot=self._slot,
        )

        # Tracker + FP layer state.
        self._tracker = ByteTracker(frame_rate=2)
        self._confidences: dict[int, TrackConfidence] = {}
        # track_id -> frame index of last match; for pruning.
        self._last_seen_frame: dict[int, int] = {}
        self._frame_counter = 0
        # track_ids we returned to the event logic last frame — used to
        # apply MATCHED_NONE to tracks that coasted.
        self._last_emitted_ids: set[int] = set()

        # Background subtraction state.
        self._bg: np.ndarray | None = None          # float32 grayscale
        self._change_mean: float = 0.0
        self._change_var: float = 0.0
        self._change_samples: int = 0

        # Heatmap — per-camera, lazy CREATE TABLE on init.
        self._heatmap = HeatmapLayer(heatmap_conn, camera.id)
        # Track_id -> list of (cx, cy) observations across lifetime.
        self._track_observations: dict[int, list[tuple[float, float]]] = {}
        # Track_id -> True once it fired a real event (used at end of track
        # to decide TP vs FP for heatmap learning).
        self._track_emitted_event: dict[int, bool] = {}

        # Layer 7 state — stationary-vehicle NCC suppression.
        # `_spot_candidates` is live (track_id -> candidate); promoted to
        # `_parking_spots` when the track prunes. Both are in-memory per
        # camera; no persistence across detector restarts in v1 — the
        # learn period re-runs from scratch in 5 min of runtime.
        self._parking_spots: list[_ParkingSpot] = []
        self._spot_candidates: dict[int, _ParkingSpotCandidate] = {}

        # Event state — preserved contract with plan §8 and clip.py.
        self._current_event_id: str | None = None
        self._current_event_started_at: datetime | None = None
        self._last_frame_at: datetime | None = None
        self._best_class: str | None = None
        self._best_confidence: float = 0.0
        # ByteTracker IDs already persisted as tracked_events rows for the
        # current event. One row per distinct track_id gives the Today view
        # accurate per-object counts (3 people in the same motion window →
        # 3 tracked_events rows → summary reads "3 people", not "1 person").
        self._current_event_tracked_ids: set[int] = set()
        self._lock = asyncio.Lock()

        # Audio-boost state (plan §7). `_audio_boost_until` is a
        # monotonic deadline; `_boost_task` is the supervisor task that
        # restores fps when the window closes.
        self._audio_boost_until: float = 0.0
        self._boost_task: asyncio.Task | None = None

        # Tasks.
        self._consumer_task: asyncio.Task | None = None
        self._closer_task: asyncio.Task | None = None
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        # DetectFfmpegSource.start() is async-supervised — its supervisor
        # task does the first ffprobe + ffmpeg spawn, which takes
        # hundreds of ms to a couple seconds depending on how fast
        # go2rtc completes its upstream RTSP handshake. We don't
        # block on readiness here: we just spawn the consume loop,
        # which awaits on the NewestFrameSlot and wakes once the
        # first frame lands. If the source never produces frames (bad
        # URL, auth failure) the supervise loop will keep retrying
        # with exponential backoff and log the cause.
        await self._source.start()
        self._running = True
        self._consumer_task = asyncio.create_task(
            self._consume_loop(), name=f"motion-consume-{self.camera.id}",
        )
        self._closer_task = asyncio.create_task(
            self._idle_closer(), name=f"motion-close-{self.camera.id}",
        )
        logger.info(
            "MotionDetector attached: %s (%s) — detect ffmpeg supervised, "
            "awaiting frames on %s",
            self.camera.id, self.camera.ip, self._rtsp_url,
        )

    async def stop(self) -> None:
        self._running = False
        for t in (self._consumer_task, self._closer_task, self._boost_task):
            if t and not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        self._consumer_task = self._closer_task = self._boost_task = None
        await self._source.stop()
        async with self._lock:
            if self._current_event_id is not None:
                await self._close_current_event_locked()

    # ------------------------------------------------------------------
    # Audio-vision fusion
    # ------------------------------------------------------------------

    def audio_boost(self) -> None:
        """Enter a 5 s high-alert window on a high-priority audio label.
        Bumps the detect ffmpeg to 5 fps and relaxes D-FINE's score
        threshold to 0.25 so partial detections still latch tracks.
        Repeated calls within the window extend the deadline.
        """
        self._audio_boost_until = time.monotonic() + _AUDIO_BOOST_WINDOW_S
        if self._boost_task is None or self._boost_task.done():
            self._boost_task = asyncio.create_task(
                self._run_audio_boost(),
                name=f"motion-boost-{self.camera.id}",
            )

    async def _run_audio_boost(self) -> None:
        """Supervisor for the audio-boost window. Bumps fps up, sleeps
        until the deadline (possibly extended by re-triggers), then
        restores steady-state fps. The score-threshold swing is read
        inline from `_audio_boost_until` on each frame, so it needs no
        coordination here — fps is the only thing with side effects on
        the ffmpeg subprocess.
        """
        try:
            await self._source.set_fps(_AUDIO_BOOST_FPS)
            while True:
                remaining = self._audio_boost_until - time.monotonic()
                if remaining <= 0:
                    break
                await asyncio.sleep(remaining)
        except asyncio.CancelledError:
            raise
        finally:
            # Always restore fps, even on cancellation — the ffmpeg
            # process outlives this task and we don't want it stuck on
            # 5 fps after a stop+start race.
            try:
                await self._source.set_fps(_STEADY_FPS)
            except Exception:
                logger.debug("audio-boost fps restore failed", exc_info=True)

    # ------------------------------------------------------------------
    # Consume loop
    # ------------------------------------------------------------------

    async def _consume_loop(self) -> None:
        try:
            while self._running:
                frame_rgb = await self._slot.get()
                try:
                    await self._handle_frame(frame_rgb)
                except Exception as e:
                    logger.exception(
                        "MotionDetector frame error cam=%s: %s",
                        self.camera.id, e,
                    )
        except asyncio.CancelledError:
            raise

    async def _handle_frame(self, frame_rgb: np.ndarray) -> None:
        now = datetime.now(timezone.utc)
        self._frame_counter += 1

        # 1. Background subtraction + proposals (with scene-change guard).
        proposals, scene_changed = self._extract_proposals(frame_rgb)

        # TEMPORARY: unconditional per-frame heartbeat while we diagnose
        # missed events. Once every 10 frames so logs stay readable
        # (5s cadence at 2 fps). Remove once root cause is identified.
        if self._frame_counter % 10 == 0:
            logger.info(
                "detect HEARTBEAT cam=%s frame=%d props=%d scene_chg=%s",
                self.camera.id, self._frame_counter,
                len(proposals), scene_changed,
            )

        # 2. Detections — motion-gated. Proposals empty → no D-FINE.
        # frame_bgr is kept around when present so Layer 7 / thumbnail
        # writes don't re-cvtColor.
        detections: list[Detection] = []
        frame_bgr: np.ndarray | None = None
        if proposals and not scene_changed:
            # Relax the score threshold during the audio-boost window
            # so partial detections that correlate with a gunshot/glass
            # break are more likely to latch a track (plan §7).
            boosted = time.monotonic() < self._audio_boost_until
            score_threshold = (
                _AUDIO_BOOST_SCORE_THRESHOLD if boosted
                else _STEADY_SCORE_THRESHOLD
            )
            # Off-loop: 50 ms inference shouldn't block async tasks.
            # Serialized through DFineDetector's single-worker executor
            # so intra_op threads don't contend across the 32 cameras.
            frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
            detections = await self._dfine.infer_async(
                frame_bgr, score_threshold=score_threshold,
            )

        # 3. Feed tracker. Always call update so confirmed tracks coast
        # consistently with empty detections.
        tracker_input = [
            TrackerDetection(
                x1=d.x1, y1=d.y1, x2=d.x2, y2=d.y2,
                score=d.score, class_id=d.class_id,
            )
            for d in detections
        ]
        confirmed_tracks = self._tracker.update(tracker_input)

        # 4. Confidence update: high for matched-this-frame, none for
        # previously-seen tracks that dropped out.
        this_frame_ids: set[int] = set()
        for tr in confirmed_tracks:
            this_frame_ids.add(tr.track_id)
            conf = self._confidences.setdefault(tr.track_id, TrackConfidence())
            conf.update("high", tr.score)
            self._last_seen_frame[tr.track_id] = self._frame_counter
            # Record bbox center for heatmap learning at end-of-track.
            cx = (tr.x1 + tr.x2) / 2.0
            cy = (tr.y1 + tr.y2) / 2.0
            self._track_observations.setdefault(tr.track_id, []).append(
                (cx, cy)
            )
        # Coast: confidence decay for tracks we emitted last frame but not
        # this frame.
        for tid in self._last_emitted_ids - this_frame_ids:
            if tid in self._confidences:
                self._confidences[tid].update("none")
        self._last_emitted_ids = this_frame_ids

        # Layer 7 pre-work: for vehicle tracks old enough to be a
        # candidate parking spot, cache the latest crop (we have
        # frame_bgr in hand exactly when the track was matched this
        # frame). Promotion to a permanent spot happens in the prune
        # step below, once the track stops being seen entirely.
        if confirmed_tracks and frame_bgr is not None:
            self._update_parking_spot_candidates(confirmed_tracks, frame_bgr)

        # Prune confidence + observation dicts for track_ids we haven't
        # seen in a while.
        stale_ids = [
            tid for tid, f in self._last_seen_frame.items()
            if self._frame_counter - f > _CONFIDENCE_PRUNE_FRAMES
        ]
        for tid in stale_ids:
            # Heatmap learning on track-end: TP if we fired an event for
            # it, FP otherwise. "Dominant cell" voted inside HeatmapLayer.
            obs = self._track_observations.pop(tid, None)
            if obs:
                if self._track_emitted_event.get(tid):
                    self._heatmap.record_true_positive(
                        obs, self._source.width, self._source.height,
                    )
                else:
                    self._heatmap.record_false_positive(
                        obs, self._source.width, self._source.height,
                    )
            self._track_emitted_event.pop(tid, None)
            self._confidences.pop(tid, None)
            self._last_seen_frame.pop(tid, None)
            # Layer 7: promote a stale spot-candidate into a parking spot.
            cand = self._spot_candidates.pop(tid, None)
            if cand is not None:
                self._parking_spots.append(
                    _ParkingSpot(
                        bbox=cand.bbox,
                        template=cand.template,
                        last_match_frame=self._frame_counter,
                    )
                )
                # LRU eviction: over cap, drop the spot whose last
                # successful NCC match is oldest. A just-promoted spot
                # has last_match_frame = self._frame_counter so it
                # survives; a spot that hasn't matched in a long time
                # (stale visitor) rolls off.
                if len(self._parking_spots) > _MAX_PARKING_SPOTS_PER_CAMERA:
                    stale = min(
                        range(len(self._parking_spots)),
                        key=lambda i: self._parking_spots[i].last_match_frame,
                    )
                    self._parking_spots.pop(stale)
                logger.info(
                    "parking spot learned cam=%s bbox=%s (total=%d)",
                    self.camera.id, cand.bbox, len(self._parking_spots),
                )

        # 5. FP filters 1–5 + labelmap collapse + heatmap weighting → best track.
        best_track: Track | None = None
        best_label: str | None = None
        best_score: float = 0.0
        fw, fh = self._source.width, self._source.height
        # TEMPORARY: per-layer rejection counters, surfaced in the trace
        # below so we can tell which FP layer is eating tracks. Remove
        # once tuning lands.
        rej = {"label": 0, "geom": 0, "score": 0, "age": 0, "conf": 0,
               "move": 0, "ncc": 0}
        for tr in confirmed_tracks:
            # Product-label collapse: COCO 80 → {person, vehicle, animal}.
            # None here silently drops the track (bicycle, clock, airplane,
            # etc.) — preserves the UX contract and kills a known class
            # of FPs in one shot (see classification/labelmap.py rationale).
            label = collapse_coco_label(tr.class_id)
            if label is None:
                rej["label"] += 1
                continue
            if not self._passes_geometric(tr, fw, fh):
                rej["geom"] += 1
                continue
            if tr.score < _L2_MIN_SCORE:
                rej["score"] += 1
                continue
            if tr.age < _L3_MIN_AGE:
                rej["age"] += 1
                continue
            conf = self._confidences.get(tr.track_id)
            if conf is None or not conf.emittable:
                rej["conf"] += 1
                continue
            # Layer 6 — movement gate.
            if not self._passes_movement_gate(tr, label):
                rej["move"] += 1
                continue
            # Layer 7 — stationary NCC (vehicle-only, needs frame_bgr).
            if not self._passes_stationary_ncc(tr, label, frame_bgr):
                rej["ncc"] += 1
                continue
            # Layer 8 weighting (no-op when heatmap is sparse).
            cx = (tr.x1 + tr.x2) / 2.0
            cy = (tr.y1 + tr.y2) / 2.0
            weight = self._heatmap.score_multiplier(cx, cy, fw, fh)
            weighted = conf.p_hat * weight
            if weighted > best_score:
                best_track = tr
                best_label = label
                best_score = weighted

        # 6. Event lifecycle.
        if best_track is not None and best_label is not None:
            self._track_emitted_event[best_track.track_id] = True
            await self._observe_emittable(
                now, frame_rgb, best_track, best_label, best_score,
            )

        # 7. Diagnostic — TEMPORARY: promoted to INFO + ungated while we
        # diagnose missed events post-MOG2-revert. Fires on every frame
        # with proposals OR detections so we see the gate behavior, not
        # just D-FINE output. Roll back to DEBUG + _DETECT_TRACE_ENABLED
        # gate once we've identified the drop point.
        if proposals or detections or scene_changed:
            sample = sorted(
                ((d.class_name, d.score) for d in detections),
                key=lambda x: -x[1],
            )[:3]
            sample_str = ", ".join(f"{n}:{s:.2f}" for n, s in sample)
            emit_str = (
                f"{best_label}@{best_score:.2f}" if best_track else "-"
            )
            rej_str = ",".join(f"{k}={v}" for k, v in rej.items() if v)
            logger.info(
                "detect cam=%s props=%d scene_chg=%s dets=%d tracks=%d top=[%s] emit=%s rej=[%s]",
                self.camera.id, len(proposals), scene_changed,
                len(detections), len(confirmed_tracks),
                sample_str, emit_str, rej_str,
            )

    # ------------------------------------------------------------------
    # Motion proposals + scene-change guard
    # ------------------------------------------------------------------

    def _extract_proposals(
        self, frame_rgb: np.ndarray
    ) -> tuple[list[tuple[int, int, int, int]], bool]:
        """Return (proposals, scene_changed). Proposals are (x,y,w,h)
        in detect-frame pixel coords. Proposals always drop-through as
        empty on a scene change.
        """
        gray = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
        if self._bg is None:
            self._bg = gray.copy()
            return [], False

        diff = np.abs(gray - self._bg)
        mean_change = float(diff.mean())

        # Update EWMA + variance of global change, skipping the first few
        # samples (they're biased toward zero right after init).
        self._change_samples += 1
        if self._change_samples == 1:
            self._change_mean = mean_change
        else:
            delta = mean_change - self._change_mean
            self._change_mean += _SCENE_CHANGE_EWMA_ALPHA * delta
            self._change_var = (
                (1 - _SCENE_CHANGE_EWMA_ALPHA) * self._change_var
                + _SCENE_CHANGE_EWMA_ALPHA * (delta * delta)
            )

        scene_changed = False
        if self._change_samples > _SCENE_CHANGE_MIN_SAMPLES:
            sigma = float(np.sqrt(self._change_var)) if self._change_var > 0 else 0.0
            if (
                sigma > 0
                and mean_change - self._change_mean > _SCENE_CHANGE_SIGMA * sigma
            ):
                scene_changed = True

        if scene_changed:
            # Freeze background; don't let the spike contaminate B.
            return [], True

        # Foreground mask.
        mask = (diff > _FG_THRESHOLD).astype(np.uint8)
        # Open then close to kill noise + bridge near-contiguous regions.
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, _MORPH_KERNEL)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, _MORPH_KERNEL)

        # Connected components.
        labeled, n_labels = ndimage.label(mask)
        proposals: list[tuple[int, int, int, int]] = []
        if n_labels > 0:
            slices = ndimage.find_objects(labeled)
            for sl in slices:
                if sl is None:
                    continue
                y_slice, x_slice = sl
                y1, y2 = y_slice.start, y_slice.stop
                x1, x2 = x_slice.start, x_slice.stop
                w = x2 - x1
                h = y2 - y1
                # Quick area gate — find_objects returns the bounding slice,
                # not the blob's pixel count; we check the actual pixel
                # count inside the bbox as a cheap approximation.
                if w * h < _MIN_COMPONENT_AREA:
                    continue
                proposals.append((int(x1), int(y1), int(w), int(h)))

        # Adaptive learning rate — faster when motion present so we lock
        # in quickly after things settle.
        alpha = _BG_ALPHA_SPIKE if proposals else _BG_ALPHA_IDLE
        self._bg = alpha * gray + (1 - alpha) * self._bg

        return proposals, False

    # ------------------------------------------------------------------
    # FP layer 1: geometric
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # FP layer 7: stationary-vehicle NCC
    # ------------------------------------------------------------------

    def _update_parking_spot_candidates(
        self, confirmed_tracks: list[Track], frame_bgr: np.ndarray
    ) -> None:
        """Cache the latest canonical template for any vehicle track old
        enough to qualify as a parking spot. Promotion happens when the
        track prunes (see the stale-id loop in `_handle_frame`)."""
        for tr in confirmed_tracks:
            label = collapse_coco_label(tr.class_id)
            if label != "vehicle":
                continue
            if tr.age < _STATIONARY_LEARN_FRAMES:
                continue
            bbox = (int(tr.x1), int(tr.y1), int(tr.x2), int(tr.y2))
            template = self._canonical_template(frame_bgr, bbox)
            if template is None:
                continue
            self._spot_candidates[tr.track_id] = _ParkingSpotCandidate(
                bbox=bbox, template=template,
            )

    def _passes_stationary_ncc(
        self, tr: Track, label: str, frame_bgr: np.ndarray | None
    ) -> bool:
        """Layer 7 — reject vehicle tracks whose crop matches any learned
        parking-spot template at NCC >= 0.9, subject to an IoU bbox gate
        first. Non-vehicle classes fall through (True).

        frame_bgr can legitimately be None (no detections this frame),
        but then this track wouldn't be in confirmed_tracks either —
        the None branch is defensive.

        On a successful match, the spot's `last_match_frame` is refreshed
        so LRU eviction keeps actively-matching spots fresh.
        """
        if label != "vehicle" or not self._parking_spots or frame_bgr is None:
            return True
        tr_bbox = (int(tr.x1), int(tr.y1), int(tr.x2), int(tr.y2))
        live_template: np.ndarray | None = None
        for spot in self._parking_spots:
            if self._iou(tr_bbox, spot.bbox) < _STATIONARY_IOU_GATE:
                continue
            # Compute the live template once, lazily — only cameras that
            # have a learned spot in the vicinity pay the resize cost.
            if live_template is None:
                live_template = self._canonical_template(frame_bgr, tr_bbox)
                if live_template is None:
                    return True
            try:
                res = cv2.matchTemplate(
                    spot.template, live_template, cv2.TM_CCOEFF_NORMED,
                )
                ncc = float(res[0, 0])
            except cv2.error:
                # Shapes mismatched unexpectedly — canonicalization
                # guarantees they won't, but fail open rather than
                # silently suppress.
                continue
            if ncc >= _STATIONARY_NCC_THRESHOLD:
                spot.last_match_frame = self._frame_counter
                return False
        return True

    @staticmethod
    def _canonical_template(
        frame_bgr: np.ndarray, bbox: tuple[int, int, int, int]
    ) -> np.ndarray | None:
        """Grab the crop at `bbox` and canonicalize to a fixed grayscale
        template (`_STATIONARY_TEMPLATE_W` × `_STATIONARY_TEMPLATE_H`).

        Returns None if the bbox doesn't yield a positive-area crop.
        """
        x1, y1, x2, y2 = bbox
        h, w = frame_bgr.shape[:2]
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(w, x2), min(h, y2)
        if x2 <= x1 or y2 <= y1:
            return None
        crop = frame_bgr[y1:y2, x1:x2]
        resized = cv2.resize(
            crop,
            (_STATIONARY_TEMPLATE_W, _STATIONARY_TEMPLATE_H),
            interpolation=cv2.INTER_AREA,
        )
        return cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)

    @staticmethod
    def _iou(
        a: tuple[int, int, int, int], b: tuple[int, int, int, int]
    ) -> float:
        ax1, ay1, ax2, ay2 = a
        bx1, by1, bx2, by2 = b
        ix1 = max(ax1, bx1)
        iy1 = max(ay1, by1)
        ix2 = min(ax2, bx2)
        iy2 = min(ay2, by2)
        iw = max(0, ix2 - ix1)
        ih = max(0, iy2 - iy1)
        inter = iw * ih
        a_area = max(0, ax2 - ax1) * max(0, ay2 - ay1)
        b_area = max(0, bx2 - bx1) * max(0, by2 - by1)
        union = a_area + b_area - inter
        if union <= 0:
            return 0.0
        return inter / union

    # ------------------------------------------------------------------
    # FP layer 6: movement gate
    # ------------------------------------------------------------------

    def _passes_movement_gate(self, tr: Track, label: str) -> bool:
        """Layer 6 — drop stationary tracks, with a time-bounded person
        carveout.

        "Stationary" means center-displacement across the track's lifetime
        is less than its bbox height. At 2 fps a real person walking
        crosses that threshold almost instantly; a parked car, a grill,
        a sculpture never will.

        The person carveout: after `_MOVEMENT_PERSON_AGE_FRAMES` frames
        the gate stops blocking "person" tracks even when stationary, so
        a real human standing still isn't silently ignored. The heatmap
        (Layer 8) is the backstop for stationary objects consistently
        mis-classified as person — it learns their cells over time.
        """
        obs = self._track_observations.get(tr.track_id)
        if not obs or len(obs) < 2:
            # Can't judge — fail open so tentative tracks aren't
            # suppressed before we even have a trajectory.
            return True
        first_cx, first_cy = obs[0]
        last_cx, last_cy = obs[-1]
        dx = last_cx - first_cx
        dy = last_cy - first_cy
        distance = (dx * dx + dy * dy) ** 0.5
        box_h = max(tr.y2 - tr.y1, 1.0)
        stationary = distance < _MOVEMENT_DIST_FRACTION_OF_H * box_h
        if not stationary:
            return True
        # Stationary — person carveout only, and only after the age gate.
        if label == "person" and tr.age >= _MOVEMENT_PERSON_AGE_FRAMES:
            return True
        return False

    @staticmethod
    def _passes_geometric(tr: Track, frame_w: int, frame_h: int) -> bool:
        w = tr.x2 - tr.x1
        h = tr.y2 - tr.y1
        if w < _L1_MIN_DIM_PX or h < _L1_MIN_DIM_PX:
            return False
        aspect = w / max(h, 1e-6)
        if aspect < _L1_ASPECT_MIN or aspect > _L1_ASPECT_MAX:
            return False
        if frame_w > 0 and frame_h > 0:
            frac = (w * h) / float(frame_w * frame_h)
            if frac > _L1_MAX_FRAME_FRACTION:
                return False
        return True

    # ------------------------------------------------------------------
    # Event lifecycle
    # ------------------------------------------------------------------

    async def _observe_emittable(
        self,
        now: datetime,
        frame_rgb: np.ndarray,
        track: Track,
        class_name: str,
        p_hat: float,
    ) -> None:
        async with self._lock:
            self._last_frame_at = now

            if self._current_event_id is None:
                # Open event.
                event_id = str(uuid.uuid4())
                cam_dir = MOTION_THUMBNAILS_DIR / self.camera.id
                cam_dir.mkdir(parents=True, exist_ok=True)
                thumb_path = cam_dir / f"{event_id}.jpg"
                try:
                    frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
                    boxed = _draw_track_box(
                        frame_bgr, track, f"{class_name} {p_hat:.2f}",
                    )
                    ok, encoded = cv2.imencode(".jpg", boxed, [cv2.IMWRITE_JPEG_QUALITY, 85])
                    if ok:
                        thumb_path.write_bytes(encoded.tobytes())
                except Exception as e:
                    logger.warning("thumbnail write failed: %s", e)

                self._current_event_id = event_id
                self._current_event_started_at = now
                self._best_class = class_name
                self._best_confidence = p_hat

                try:
                    await db.insert_motion_event(
                        self._conn,
                        event_id=event_id,
                        camera_id=self.camera.id,
                        started_at=now.isoformat(),
                        thumbnail_path=str(thumb_path),
                    )
                    await db.update_motion_event_classification(
                        self._conn,
                        event_id=event_id,
                        object_class=class_name,
                        object_confidence=p_hat,
                    )
                except Exception as e:
                    logger.error("insert_motion_event failed: %s", e)

                await self._event_bus.emit(
                    "motion_started",
                    {
                        "id": event_id,
                        "camera_id": self.camera.id,
                        "started_at": now.isoformat(),
                        "thumbnail_url":
                            f"/api/motion_events/{event_id}/thumbnail.jpg",
                    },
                )
                await self._event_bus.emit(
                    "motion_event_updated",
                    {
                        "id": event_id,
                        "camera_id": self.camera.id,
                        "object_class": class_name,
                        "object_confidence": p_hat,
                    },
                )
                logger.info(
                    "Motion started: cam=%s event=%s class=%s p=%.3f",
                    self.camera.id, event_id, class_name, p_hat,
                )
                await self._persist_tracked_if_new(now, track, class_name, p_hat)
                return

            # Update: class changed, or confidence improved enough to
            # surface (0.05 delta is the noise floor — tighter than that
            # would generate churn on every frame).
            improved = (
                class_name != self._best_class
                or p_hat - self._best_confidence >= 0.05
            )
            if improved:
                self._best_class = class_name
                self._best_confidence = p_hat
                try:
                    await db.update_motion_event_classification(
                        self._conn,
                        event_id=self._current_event_id,
                        object_class=class_name,
                        object_confidence=p_hat,
                    )
                except Exception as e:
                    logger.error(
                        "update_motion_event_classification failed: %s", e,
                    )
                await self._event_bus.emit(
                    "motion_event_updated",
                    {
                        "id": self._current_event_id,
                        "camera_id": self.camera.id,
                        "object_class": class_name,
                        "object_confidence": p_hat,
                    },
                )
            # Per-object persist is independent of whether motion_events'
            # dominant class changed: a 2nd concurrent person in the same
            # window should add a row even if the event's class is stable.
            await self._persist_tracked_if_new(now, track, class_name, p_hat)

    async def _persist_tracked_if_new(
        self,
        now: datetime,
        track: Track,
        class_name: str,
        p_hat: float,
    ) -> None:
        """Write one tracked_events row the first time a ByteTrack ID is
        seen as emittable within the current motion event. ByteTracker has
        no track-close signal so we persist on first-emit instead — the
        Today view's counts come from COUNT(*) grouped by object_class,
        so each distinct track_id contributing one row is what it needs.
        """
        if self._current_event_id is None:
            return
        if track.track_id in self._current_event_tracked_ids:
            return
        self._current_event_tracked_ids.add(track.track_id)
        now_iso = now.isoformat()
        tracked_id = str(uuid.uuid4())
        try:
            await db.insert_tracked_event(
                self._conn,
                tracked_id=tracked_id,
                camera_id=self.camera.id,
                motion_event_id=self._current_event_id,
                started_at=now_iso,
                ended_at=now_iso,
                frame_count=1,
                bbox_json=json.dumps(
                    [track.x1, track.y1, track.x2, track.y2]
                ),
                bbox_history_json=None,
                thumbnail_path=None,
            )
            await db.update_tracked_event_classification(
                self._conn,
                tracked_id=tracked_id,
                object_class=class_name,
                object_confidence=p_hat,
            )
        except Exception as e:
            logger.error("insert_tracked_event failed: %s", e)

    async def _close_current_event_locked(self) -> None:
        event_id = self._current_event_id
        if event_id is None:
            return
        ended_at = self._last_frame_at or datetime.now(timezone.utc)
        started_at = self._current_event_started_at
        try:
            await db.complete_motion_event(
                self._conn, event_id=event_id, ended_at=ended_at.isoformat(),
            )
        except Exception as e:
            logger.error("complete_motion_event failed: %s", e)
        await self._event_bus.emit(
            "motion_ended",
            {
                "id": event_id,
                "camera_id": self.camera.id,
                "ended_at": ended_at.isoformat(),
            },
        )
        logger.info(
            "Motion ended: cam=%s event=%s class=%s",
            self.camera.id, event_id, self._best_class,
        )

        # Clear state before spawning clip so re-entrancy is safe.
        self._current_event_id = None
        self._current_event_started_at = None
        self._best_class = None
        self._best_confidence = 0.0
        self._current_event_tracked_ids.clear()

        # Spawn clip (contract with clip.py: 5 positional args, no knowledge
        # of tracker internals).
        start_iso = started_at.isoformat() if started_at else ended_at.isoformat()
        try:
            asyncio.create_task(
                create_motion_clip(
                    self._conn,
                    self.camera.id,
                    event_id,
                    start_iso,
                    ended_at.isoformat(),
                )
            )
        except Exception:
            logger.exception("create_motion_clip spawn failed for %s", event_id)

    async def _idle_closer(self) -> None:
        try:
            while True:
                await asyncio.sleep(1.0)
                async with self._lock:
                    if (
                        self._current_event_id is not None
                        and self._last_frame_at is not None
                    ):
                        idle = (
                            datetime.now(timezone.utc) - self._last_frame_at
                        ).total_seconds()
                        if idle >= MOTION_DEBOUNCE_SECONDS:
                            await self._close_current_event_locked()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(
                "MotionDetector idle closer error cam=%s: %s",
                self.camera.id, e,
            )
