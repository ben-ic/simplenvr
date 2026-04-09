"""IOU-based multi-object tracker for per-camera motion pipelines.

Consumes MOG2 bboxes from the motion detector and groups them into
persistent tracks. A track represents one moving thing (a person,
a car, a cat) observed across a sequence of frames — which is a
different concept than a "motion event" (a time window where *some*
motion happened). A frame with two people walking in opposite
directions produces two tracks, not one merged blob.

Why this matters for the product:

  1. **Multi-object scenes classify correctly.** A person walking past a
     parked car in the carport: without tracks, the classifier medians
     across all frames and picks whichever wins; with tracks, each blob
     gets its own label, its own timestamps, its own Inbox row.

  2. **Summarizer sentence shapes need per-object timelines.** To say
     *"a delivery person dropped a package at 10:32"* the summarizer
     must know that *person* started at 10:32 as a distinct event from
     whatever else was in frame. Time-grouped events can't produce that
     sentence shape because they have no concept of "the person" as
     a thing separate from other motion.

  3. **Honest confidence aggregation.** Median-of-track for this
     specific person's 10 frames is a stronger signal than median-of-
     event for 20 frames mixing a confident vehicle and a marginal
     person. Tracks that never cross threshold drop silently; tracks
     that do become their own Inbox rows.

Design characteristics we care about (vs the general literature on
multi-object tracking):

  - **Pure function of its inputs.** No I/O, no DB, no event bus. The
    manager layer wires I/O on top. This is what makes the tracker
    unit-testable with synthetic bbox sequences.

  - **Greedy assignment, persistent identity.** Each incoming bbox is
    assigned to its best-IOU existing track; ties go by recency.
    Losing a single frame-match does NOT end a track. Tracks only
    close when they go fully silent for the idle timeout. This
    handles occlusions (person walking behind a car) and MOG2
    flicker (background model briefly reclassifying a pixel) without
    fragmenting one real-world object into multiple Inbox rows.

  - **Promotion gate.** Tracks start in CANDIDATE state and only become
    real tracked events after 5 frames. Single-frame MOG2 flickers
    never survive the gate and never feed the classifier. This is
    how we throw away leaf-jiggles and insect-on-lens without a
    zone configuration UI.

  - **Zero knobs.** IOU threshold, promotion frame count, and idle
    timeout are fixed constants tuned to the capability-probe hardware
    tiers. No per-camera configuration — the user never sees this.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tuning constants (zero-config — never exposed to users)
# ---------------------------------------------------------------------------

# Minimum IOU to consider a new bbox a continuation of an existing track.
# Below this, the bbox either starts a new candidate track or gets dropped
# as noise. 0.20 is loose enough to survive MOG2 bbox wiggle between
# frames, tight enough that two adjacent blobs stay distinct.
IOU_MATCH_THRESHOLD = 0.20

# Number of consecutive frame-matches before a CANDIDATE track is promoted
# to a real tracked event. 5 frames at typical scene-filter rate (~2-3 fps
# after scene-change gating) = roughly 2 seconds of sustained motion. A
# leaf jiggle or bird-flyby won't survive this, a person walking will.
PROMOTION_FRAME_COUNT = 5

# Idle timeout: how long a track can go without a new frame-match before
# we consider it closed. 2 seconds matches the existing MotionDetector
# debounce so tracker timing aligns with the rest of the pipeline.
IDLE_TIMEOUT_SECONDS = 2.0

# Bbox history length per track. Used for trajectory/smoothing if the
# classifier or summarizer wants it later. Bounded to keep memory flat
# on long tracks (a car sitting in a driveway for 10 minutes shouldn't
# balloon the process RSS).
MAX_BBOX_HISTORY = 32


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

# Bbox in pixel space: (x, y, w, h) with origin at top-left of the frame.
# We use x/y/w/h rather than x1/y1/x2/y2 because MOG2 contours return
# that shape natively via cv2.boundingRect and it saves a conversion.
Bbox = tuple[int, int, int, int]

TrackState = Literal["candidate", "promoted", "closed"]


@dataclass
class Track:
    """One moving thing observed across multiple frames on one camera.

    A Track starts as `candidate` when the first bbox creates it, is
    promoted to `promoted` after PROMOTION_FRAME_COUNT consecutive
    frame-matches, and becomes `closed` after IDLE_TIMEOUT_SECONDS of
    no new matching bbox. A closed track is what the classifier
    subsystem consumes — it represents a complete "this thing moved
    through the frame for X seconds" observation.

    Identity is assigned via uuid4 on creation and stable for the
    lifetime of the track. The DB's tracked_events table uses this
    id as its primary key so downstream subsystems can reference
    a specific observation.
    """
    id: str
    camera_id: str
    state: TrackState
    first_seen: datetime
    last_seen: datetime
    bbox: Bbox                                    # current (smoothed) bbox
    bbox_history: list[Bbox] = field(default_factory=list)
    frame_count: int = 0
    # Frames the classifier should consume. We keep the first, middle,
    # and last frame JPEG bytes so the classifier has representative
    # samples without storing every frame of a long track. Populated
    # by the manager layer, not by the tracker itself — the tracker
    # only knows bboxes.
    frame_refs: list[object] = field(default_factory=list)


# ---------------------------------------------------------------------------
# IOU math
# ---------------------------------------------------------------------------

def iou(a: Bbox, b: Bbox) -> float:
    """Intersection-over-union of two (x, y, w, h) bboxes.

    Returns 0.0 for disjoint or degenerate inputs. This is ~10 lines
    of math; we avoid pulling in a dedicated library for a formula
    that fits in a docstring.
    """
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    if aw <= 0 or ah <= 0 or bw <= 0 or bh <= 0:
        return 0.0

    # Intersection rect in absolute coordinates.
    ix1 = max(ax, bx)
    iy1 = max(ay, by)
    ix2 = min(ax + aw, bx + bw)
    iy2 = min(ay + ah, by + bh)
    iw = ix2 - ix1
    ih = iy2 - iy1
    if iw <= 0 or ih <= 0:
        return 0.0

    inter = iw * ih
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _smooth_bbox(old: Bbox, new: Bbox, alpha: float = 0.5) -> Bbox:
    """Exponential moving average bbox smoother.

    alpha = 0.5 gives equal weight to the old and new bboxes — new
    observations drag the track's bbox toward them but don't replace
    it wholesale. Smoothing is what keeps a track's bbox from
    jittering frame-to-frame when MOG2's contour bounding box wiggles
    by a few pixels due to lighting noise.
    """
    ox, oy, ow, oh = old
    nx, ny, nw, nh = new
    return (
        int(ox * (1 - alpha) + nx * alpha),
        int(oy * (1 - alpha) + ny * alpha),
        int(ow * (1 - alpha) + nw * alpha),
        int(oh * (1 - alpha) + nh * alpha),
    )


# ---------------------------------------------------------------------------
# Tracker — per-camera state machine
# ---------------------------------------------------------------------------

class CameraTracker:
    """One tracker instance per camera. Owns all currently-active tracks
    on that camera.

    Usage pattern (from the motion detector layer):

        tracker = CameraTracker(camera_id="cam-42")
        for bbox in mog2_bboxes_this_frame:
            tracker.observe(bbox, frame_time)
        closed = tracker.sweep_idle(frame_time)
        for track in closed:
            await classifier_queue.put(track)  # handed off to classifier

    The tracker is not thread-safe. Call it from a single asyncio task
    per camera. Cross-camera parallelism is fine because each camera
    has its own CameraTracker instance.
    """

    def __init__(self, camera_id: str):
        self.camera_id = camera_id
        self._active: dict[str, Track] = {}
        # Track id → last-observation-time used for the idle sweep. We
        # keep this separate from Track.last_seen so we can cheaply
        # iterate without touching the whole Track object.
        self._last_match: dict[str, datetime] = {}

    @property
    def active_count(self) -> int:
        return len(self._active)

    def observe(self, bbox: Bbox, frame_time: datetime) -> Track | None:
        """Assign a new bbox to an existing track or create a new candidate.

        Returns the Track that absorbed the observation (either an
        existing one whose bbox was updated, or a fresh candidate).
        Caller doesn't need to do anything with the return value unless
        it wants to inspect per-bbox assignment for debugging.

        Greedy assignment: we pick the single existing track with the
        highest IOU above the match threshold. Ties broken by recency
        (more recently seen wins). If nothing matches, a new candidate
        track is created.
        """
        best_id: str | None = None
        best_iou = IOU_MATCH_THRESHOLD  # must strictly exceed to match
        best_recency: datetime | None = None

        for track_id, track in self._active.items():
            if track.state == "closed":
                continue
            score = iou(track.bbox, bbox)
            if score <= best_iou:
                continue
            # Recency tiebreaker: prefer the track we saw more recently,
            # which will almost always be the right one when two tracks
            # have drifted into similar screen positions.
            last = self._last_match.get(track_id, track.last_seen)
            if score > best_iou or best_recency is None or last > best_recency:
                best_id = track_id
                best_iou = score
                best_recency = last

        if best_id is not None:
            track = self._active[best_id]
            track.bbox = _smooth_bbox(track.bbox, bbox)
            track.bbox_history.append(track.bbox)
            if len(track.bbox_history) > MAX_BBOX_HISTORY:
                track.bbox_history.pop(0)
            track.last_seen = frame_time
            track.frame_count += 1
            self._last_match[best_id] = frame_time
            # Promotion gate — candidate becomes a real tracked event.
            if track.state == "candidate" and track.frame_count >= PROMOTION_FRAME_COUNT:
                track.state = "promoted"
                logger.info(
                    "Track promoted: cam=%s track=%s frames=%d",
                    self.camera_id, track.id, track.frame_count,
                )
            return track

        # No match → new candidate track.
        new_id = str(uuid.uuid4())
        new_track = Track(
            id=new_id,
            camera_id=self.camera_id,
            state="candidate",
            first_seen=frame_time,
            last_seen=frame_time,
            bbox=bbox,
            bbox_history=[bbox],
            frame_count=1,
        )
        self._active[new_id] = new_track
        self._last_match[new_id] = frame_time
        return new_track

    def sweep_idle(self, now: datetime) -> list[Track]:
        """Close all tracks that haven't seen a matching bbox in
        IDLE_TIMEOUT_SECONDS. Returns the list of closed tracks that
        were promoted (i.e. survived the 5-frame gate) — these are
        what the classifier should process. Unpromoted candidates
        (leaf-jiggles, 2-frame noise) are discarded silently without
        ever reaching the classifier.

        Call this on every frame tick, not just on explicit events —
        a track that stops appearing shouldn't linger past the idle
        timeout even if no new bboxes are arriving on the camera.
        """
        deadline = now - timedelta(seconds=IDLE_TIMEOUT_SECONDS)
        to_close: list[str] = []
        for track_id, last in self._last_match.items():
            if last < deadline:
                to_close.append(track_id)

        closed_promoted: list[Track] = []
        for track_id in to_close:
            track = self._active.pop(track_id, None)
            self._last_match.pop(track_id, None)
            if track is None:
                continue
            track.state = "closed"
            if track.frame_count >= PROMOTION_FRAME_COUNT:
                closed_promoted.append(track)
                logger.info(
                    "Track closed (promoted): cam=%s track=%s frames=%d duration=%.1fs",
                    self.camera_id, track.id, track.frame_count,
                    (track.last_seen - track.first_seen).total_seconds(),
                )
            else:
                logger.debug(
                    "Track dropped (unpromoted): cam=%s track=%s frames=%d",
                    self.camera_id, track.id, track.frame_count,
                )

        return closed_promoted

    def flush(self, now: datetime | None = None) -> list[Track]:
        """Close every active track immediately and return the promoted
        ones. Called on camera shutdown / recorder stop so no promoted
        tracks get stranded with no terminal classifier pass.
        """
        now = now or datetime.now(timezone.utc)
        result: list[Track] = []
        for track_id in list(self._active.keys()):
            track = self._active.pop(track_id)
            self._last_match.pop(track_id, None)
            track.state = "closed"
            if track.frame_count >= PROMOTION_FRAME_COUNT:
                result.append(track)
        return result
