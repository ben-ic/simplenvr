"""ByteTrack multi-object tracker for SimpleNVR detection pipeline v2.

Consumes per-frame object detections (YOLOX / D-FINE bounding boxes with
score and class_id) and groups them into persistent tracks with stable
identities across frames. A track represents one moving thing observed
over time — a person, a vehicle, a pet — which is a different concept
than a "motion event" (a time window where *some* motion happened).

Algorithm: ByteTrack (Zhang et al., 2022). We associate high-confidence
detections first, then sweep up low-confidence detections against any
tracks that didn't match in pass 1. This recovers identity through the
brief score dips that happen when a detector gets distracted by
occlusion, motion blur, or atypical pose.

Key design points:

  - **Kalman filter** with 8-D state `[x, y, a, h, vx, vy, va, vh]` where
    (x, y) is the bbox center, `a` is the aspect ratio (w/h), and `h` is
    the height. Constant velocity. Noise scales with `h` so we track
    large and small objects with the same code.

  - **Two-pass association.** Pass 1: high-score detections vs confirmed
    + lost tracks (IoU gate MATCH_THRESH). Pass 2: low-score detections
    vs the still-unmatched tracks from pass 1 (stricter IoU gate
    SECOND_PASS_IOU). Tentative tracks get one separate pass with the
    strictest gate TENTATIVE_IOU.

  - **Class policy.** Single track pool regardless of class. When pairing
    a detection with a track, a class mismatch adds CLASS_MISMATCH_PENALTY
    to the IoU cost. The track inherits the detection's class on match —
    most-recent wins. This handles classifier flicker between overlapping
    classes (person ↔ bicycle on a cyclist) without fragmenting tracks.

  - **Coasting.** An unmatched confirmed track goes to Lost and coasts on
    the Kalman prediction for TRACK_BUFFER frames before it's removed.
    The buffer is 6 frames — 3 seconds at our 2 fps detect stride —
    which is much shorter than ifzhang's reference default of 30 (tuned
    for 30 fps).

  - **Monotonic IDs.** A removed track's ID is never reused.

  - **Pure function.** update() has no I/O. Wiring into the event bus,
    DB, or classifier happens at a higher layer.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
from scipy.linalg import cho_factor, cho_solve
from scipy.optimize import linear_sum_assignment


# ---------------------------------------------------------------------------
# Hyperparameters (module-level constants; zero-config)
# ---------------------------------------------------------------------------

# Detections with score >= TRACK_THRESH are "high" and drive pass 1.
TRACK_THRESH = 0.5

# Pass 1 IoU cost gate. `cost = 1 - IoU` (with optional class penalty), so
# cost <= MATCH_THRESH is equivalent to IoU >= 1 - MATCH_THRESH.
MATCH_THRESH = 0.8

# Pass 2 IoU gate (low-score detections). Stricter spatial overlap required
# because a low-confidence detection has less independent evidence.
SECOND_PASS_IOU = 0.5

# Tentative track IoU gate — strictest, because we don't yet trust the
# track's identity and a wrong early link will poison all future frames.
TENTATIVE_IOU = 0.7

# Frames of coasting before a Lost track is removed. Reference ByteTrack
# uses 30 for 30 fps video (~1s). SimpleNVR runs detection at 2 fps, so
# 6 frames = 3 s. Long enough to ride through a person walking behind
# a parked car; short enough that ID churn feels immediate on screen.
TRACK_BUFFER = 6

# Low-band detections: kept for pass 2 if score in [LOW_THRESH, TRACK_THRESH).
LOW_THRESH = 0.1

# Minimum score required to spawn a new track from an unmatched high-score
# detection. Higher than TRACK_THRESH so we don't seed tracks from every
# marginal detection — continuations happen freely, spawns are strict.
DET_THRESH = 0.6

# Reserved for future use — downstream filters (movement gate, area gate)
# apply their own thresholds before reaching the tracker.
MIN_BOX_AREA = 100

# Additive IoU-cost penalty when detection class differs from track class.
# Value of 0.1 means class switches cost up to 10% worse IoU.
CLASS_MISMATCH_PENALTY = 0.1


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------

@dataclass(slots=True, frozen=True)
class Detection:
    """One detector output for a single frame, in pixel coordinates."""
    x1: float
    y1: float
    x2: float
    y2: float
    score: float
    class_id: int


@dataclass(slots=True)
class Track:
    """One tracked object at a single instant (current frame).

    Emitted by ByteTracker.update(). Only confirmed tracks are returned.
    """
    track_id: int
    x1: float
    y1: float
    x2: float
    y2: float
    score: float
    class_id: int
    age: int
    time_since_update: int
    state: Literal["tentative", "confirmed", "lost", "removed"]
    confirmed: bool


# ---------------------------------------------------------------------------
# Bbox conversions
# ---------------------------------------------------------------------------

def _tlbr_to_xyah(x1: float, y1: float, x2: float, y2: float) -> np.ndarray:
    """Top-left/bottom-right → center/aspect/height measurement vector."""
    w = x2 - x1
    h = y2 - y1
    cx = x1 + w * 0.5
    cy = y1 + h * 0.5
    a = w / h if h > 0 else 0.0
    return np.array([cx, cy, a, h], dtype=np.float64)


def _xyah_to_tlbr(xyah: np.ndarray) -> tuple[float, float, float, float]:
    cx, cy, a, h = xyah[0], xyah[1], xyah[2], xyah[3]
    w = a * h
    return (cx - w * 0.5, cy - h * 0.5, cx + w * 0.5, cy + h * 0.5)


def _iou_matrix(atlbr: np.ndarray, btlbr: np.ndarray) -> np.ndarray:
    """IoU between every row of atlbr (N x 4) and btlbr (M x 4) → N x M."""
    if atlbr.size == 0 or btlbr.size == 0:
        return np.zeros((atlbr.shape[0], btlbr.shape[0]), dtype=np.float64)
    ax1, ay1, ax2, ay2 = atlbr[:, 0:1], atlbr[:, 1:2], atlbr[:, 2:3], atlbr[:, 3:4]
    bx1, by1, bx2, by2 = btlbr[:, 0], btlbr[:, 1], btlbr[:, 2], btlbr[:, 3]
    ix1 = np.maximum(ax1, bx1)
    iy1 = np.maximum(ay1, by1)
    ix2 = np.minimum(ax2, bx2)
    iy2 = np.minimum(ay2, by2)
    iw = np.clip(ix2 - ix1, 0, None)
    ih = np.clip(iy2 - iy1, 0, None)
    inter = iw * ih
    area_a = np.clip(ax2 - ax1, 0, None) * np.clip(ay2 - ay1, 0, None)
    area_b = np.clip(bx2 - bx1, 0, None) * np.clip(by2 - by1, 0, None)
    union = area_a + area_b - inter
    with np.errstate(divide="ignore", invalid="ignore"):
        iou = np.where(union > 0, inter / union, 0.0)
    return iou


# ---------------------------------------------------------------------------
# Kalman filter
# ---------------------------------------------------------------------------

class _KalmanFilter:
    """8-D constant-velocity Kalman filter operating on (x, y, a, h).

    State: [cx, cy, a, h, vx, vy, va, vh]. Measurement: [cx, cy, a, h].
    Noise stds are proportional to the current height `h` so a 40-pixel
    person and a 400-pixel truck get appropriately scaled covariances
    without any per-camera tuning.
    """

    # Per plan §5 — weights match the reference SORT/ByteTrack formulation.
    _STD_POSITION = 1.0 / 20.0
    _STD_VELOCITY = 1.0 / 160.0

    def __init__(self) -> None:
        ndim = 4
        # F — constant velocity. Position[t+1] = Position[t] + Velocity[t].
        self._F = np.eye(2 * ndim, dtype=np.float64)
        for i in range(ndim):
            self._F[i, ndim + i] = 1.0
        # H — measurement is the first ndim of state.
        self._H = np.eye(ndim, 2 * ndim, dtype=np.float64)

    # --- helpers --------------------------------------------------------

    def _process_noise(self, h: float) -> np.ndarray:
        sp = self._STD_POSITION * h
        sv = self._STD_VELOCITY * h
        return np.square(np.array([
            sp, sp, 1e-2, sp,
            sv, sv, 1e-5, sv,
        ], dtype=np.float64))

    def _measurement_noise(self, h: float) -> np.ndarray:
        sp = self._STD_POSITION * h
        return np.square(np.array([sp, sp, 1e-1, sp], dtype=np.float64))

    # --- public API -----------------------------------------------------

    def initiate(self, measurement: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Cold-start a track from its first measurement."""
        mean_pos = measurement.copy()
        mean_vel = np.zeros(4, dtype=np.float64)
        mean = np.concatenate([mean_pos, mean_vel])
        h = measurement[3]
        sp = self._STD_POSITION * h
        sv = self._STD_VELOCITY * h
        # Wide initial covariance (2x the process std) — we have no prior
        # on velocity and only one sample on position.
        std = np.array([
            2 * sp, 2 * sp, 1e-2, 2 * sp,
            10 * sv, 10 * sv, 1e-5, 10 * sv,
        ], dtype=np.float64)
        covariance = np.diag(np.square(std))
        return mean, covariance

    def predict(self, mean: np.ndarray, covariance: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        h = max(mean[3], 1.0)
        Q = np.diag(self._process_noise(h))
        new_mean = self._F @ mean
        new_cov = self._F @ covariance @ self._F.T + Q
        new_cov = 0.5 * (new_cov + new_cov.T)
        return new_mean, new_cov

    def update(
        self,
        mean: np.ndarray,
        covariance: np.ndarray,
        measurement: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray]:
        h = max(mean[3], 1.0)
        R = np.diag(self._measurement_noise(h))
        projected_mean = self._H @ mean
        projected_cov = self._H @ covariance @ self._H.T + R
        # Solve K = P H^T S^-1 via Cholesky; stable and fast.
        projected_cov = 0.5 * (projected_cov + projected_cov.T)
        chol = cho_factor(projected_cov, lower=True, check_finite=False)
        # K.T = cho_solve(chol, (P H^T).T) → K = solution.T
        ph_t = covariance @ self._H.T  # shape (8, 4)
        kalman_gain = cho_solve(chol, ph_t.T, check_finite=False).T
        innovation = measurement - projected_mean
        new_mean = mean + kalman_gain @ innovation
        new_cov = covariance - kalman_gain @ projected_cov @ kalman_gain.T
        new_cov = 0.5 * (new_cov + new_cov.T)
        return new_mean, new_cov


# ---------------------------------------------------------------------------
# Internal track (mutable; private to the tracker)
# ---------------------------------------------------------------------------

class _STrack:
    """Single-track internal state. Not exported."""

    __slots__ = (
        "track_id", "mean", "covariance", "score", "class_id",
        "state", "age", "time_since_update", "hits",
    )

    def __init__(
        self,
        track_id: int,
        mean: np.ndarray,
        covariance: np.ndarray,
        score: float,
        class_id: int,
    ) -> None:
        self.track_id = track_id
        self.mean = mean
        self.covariance = covariance
        self.score = score
        self.class_id = class_id
        self.state: Literal["tentative", "confirmed", "lost", "removed"] = "tentative"
        self.age = 1
        self.time_since_update = 0
        self.hits = 1

    def predict(self, kf: _KalmanFilter) -> None:
        # If the track is coasting, zero the velocity in xy to avoid runaway
        # prediction off-screen — standard ByteTrack trick. (Keep va/vh.)
        if self.state != "confirmed":
            self.mean[6] = 0.0  # va — aspect velocity
        self.mean, self.covariance = kf.predict(self.mean, self.covariance)
        self.age += 1
        self.time_since_update += 1

    def update_from_detection(self, kf: _KalmanFilter, det: Detection) -> None:
        meas = _tlbr_to_xyah(det.x1, det.y1, det.x2, det.y2)
        self.mean, self.covariance = kf.update(self.mean, self.covariance, meas)
        self.score = det.score
        self.class_id = det.class_id  # most-recent-wins class policy
        self.time_since_update = 0
        self.hits += 1
        # A lost track that re-matches comes straight back to confirmed.
        # Tentative tracks remain tentative here — promotion to confirmed
        # is decided by the tracker loop based on hit count.
        if self.state == "lost":
            self.state = "confirmed"

    def tlbr(self) -> tuple[float, float, float, float]:
        return _xyah_to_tlbr(self.mean[:4])


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------

class ByteTracker:
    """ByteTrack multi-object tracker.

    One instance per logical stream. update() is called once per frame
    with the full detection list for that frame and returns the confirmed
    tracks as of that frame.
    """

    def __init__(self, frame_rate: int = 2) -> None:
        # frame_rate is accepted for API compatibility and for future
        # tuning (buffer-in-seconds vs buffer-in-frames). The Kalman model
        # uses dt=1 per frame regardless of wall-clock rate, matching the
        # reference ByteTrack formulation. TRACK_BUFFER is tuned for 2 fps.
        self._frame_rate = frame_rate
        self._kf = _KalmanFilter()
        self._tracks: list[_STrack] = []
        self._next_id = 1

    # --- lifecycle ------------------------------------------------------

    def reset(self) -> None:
        self._tracks = []
        self._next_id = 1

    # --- main loop ------------------------------------------------------

    def update(self, detections: list[Detection]) -> list[Track]:
        # 1. Kalman predict for every existing track.
        for trk in self._tracks:
            trk.predict(self._kf)

        # 2. Partition detections by score band.
        high_dets: list[Detection] = []
        low_dets: list[Detection] = []
        for det in detections:
            if det.score >= TRACK_THRESH:
                high_dets.append(det)
            elif det.score >= LOW_THRESH:
                low_dets.append(det)

        # 3. Partition tracks into (confirmed+lost) vs tentative.
        active_tracks = [t for t in self._tracks if t.state in ("confirmed", "lost")]
        tentative_tracks = [t for t in self._tracks if t.state == "tentative"]

        # --- Pass 1: high-score detections vs active tracks -------------
        unmatched_high_idx, unmatched_active_idx = self._associate(
            active_tracks, high_dets, MATCH_THRESH, apply_class_penalty=True,
        )

        # --- Pass 2: low-score detections vs still-unmatched active tracks
        still_unmatched_active = [active_tracks[i] for i in unmatched_active_idx]
        _, unmatched_low_in_still = self._associate(
            still_unmatched_active, low_dets, SECOND_PASS_IOU, apply_class_penalty=True,
        )
        # Tracks in still_unmatched_active that didn't match in pass 2
        # are the ones that should transition to Lost / be removed.
        tracks_no_pass2_match = {id(still_unmatched_active[i]) for i in unmatched_low_in_still}

        # --- Pass 3: remaining high-score dets vs tentative tracks ------
        remaining_high = [high_dets[i] for i in unmatched_high_idx]
        unmatched_remaining_high_idx, _ = self._associate(
            tentative_tracks, remaining_high, TENTATIVE_IOU, apply_class_penalty=True,
        )

        # 4. Handle unmatched active tracks: confirmed → lost, lost → maybe removed.
        for trk in active_tracks:
            if id(trk) in tracks_no_pass2_match:
                if trk.state == "confirmed":
                    trk.state = "lost"
                elif trk.state == "lost" and trk.time_since_update > TRACK_BUFFER:
                    trk.state = "removed"

        # 5. Handle unmatched tentative tracks — drop immediately (never
        #    got their 2nd consecutive match).
        for trk in tentative_tracks:
            if trk.time_since_update > 0:
                trk.state = "removed"

        # 6. Promote tentative tracks that matched to confirmed (hits >= 2).
        for trk in tentative_tracks:
            if trk.state == "tentative" and trk.hits >= 2:
                trk.state = "confirmed"

        # 7. Spawn new tentative tracks from unmatched high-score dets with
        #    score >= DET_THRESH.
        for i in unmatched_remaining_high_idx:
            det = remaining_high[i]
            if det.score < DET_THRESH:
                continue
            meas = _tlbr_to_xyah(det.x1, det.y1, det.x2, det.y2)
            mean, cov = self._kf.initiate(meas)
            new_track = _STrack(
                track_id=self._next_id,
                mean=mean,
                covariance=cov,
                score=det.score,
                class_id=det.class_id,
            )
            self._next_id += 1
            self._tracks.append(new_track)

        # 8. Garbage-collect removed tracks.
        self._tracks = [t for t in self._tracks if t.state != "removed"]

        # 9. Return confirmed tracks only.
        output: list[Track] = []
        for trk in self._tracks:
            if trk.state != "confirmed":
                continue
            x1, y1, x2, y2 = trk.tlbr()
            output.append(Track(
                track_id=trk.track_id,
                x1=x1, y1=y1, x2=x2, y2=y2,
                score=trk.score,
                class_id=trk.class_id,
                age=trk.age,
                time_since_update=trk.time_since_update,
                state="confirmed",
                confirmed=True,
            ))
        return output

    # --- association ----------------------------------------------------

    def _associate(
        self,
        tracks: list[_STrack],
        dets: list[Detection],
        iou_gate: float,
        apply_class_penalty: bool,
    ) -> tuple[list[int], list[int]]:
        """Run Hungarian assignment between tracks and detections.

        Cost = 1 - IoU (+ CLASS_MISMATCH_PENALTY on class mismatch).
        Costs strictly above iou_gate are set to np.inf so Hungarian
        never picks them; any inf-cost match in the solution is rejected.

        Applies matches in-place (Kalman update on matched tracks) and
        returns (unmatched_det_indices, unmatched_track_indices).
        """
        n_trk = len(tracks)
        n_det = len(dets)
        if n_trk == 0:
            return list(range(n_det)), []
        if n_det == 0:
            return [], list(range(n_trk))

        track_boxes = np.array([t.tlbr() for t in tracks], dtype=np.float64)
        det_boxes = np.array(
            [[d.x1, d.y1, d.x2, d.y2] for d in dets], dtype=np.float64,
        )
        iou = _iou_matrix(track_boxes, det_boxes)  # (n_trk, n_det)
        cost = 1.0 - iou

        if apply_class_penalty:
            track_classes = np.array([t.class_id for t in tracks])
            det_classes = np.array([d.class_id for d in dets])
            mismatch = track_classes[:, None] != det_classes[None, :]
            cost = cost + mismatch.astype(np.float64) * CLASS_MISMATCH_PENALTY

        # Gate before Hungarian so the solver can't pair infeasible rows.
        gated = np.where(cost <= iou_gate, cost, np.inf)

        # If every cell is inf, linear_sum_assignment will still return a
        # valid permutation; we reject all of those below by the inf check.
        finite_cost = np.where(np.isinf(gated), 1e6, gated)
        row_ind, col_ind = linear_sum_assignment(finite_cost)

        matched_track_idx: set[int] = set()
        matched_det_idx: set[int] = set()
        for r, c in zip(row_ind, col_ind):
            if not np.isfinite(gated[r, c]):
                continue
            tracks[r].update_from_detection(self._kf, dets[c])
            matched_track_idx.add(r)
            matched_det_idx.add(c)

        unmatched_dets = [i for i in range(n_det) if i not in matched_det_idx]
        unmatched_tracks = [i for i in range(n_trk) if i not in matched_track_idx]
        return unmatched_dets, unmatched_tracks
