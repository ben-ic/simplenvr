#!/usr/bin/env python
"""Offline replay harness for the Detection v2 pipeline.

Copies the proposal + FP-filter logic from backend/motion/detector.py
so we can run the pipeline against a recorded .mp4 without the DB,
asyncio, event-bus, and ffmpeg supervisor layers. The production pure
components (DFineDetector, ByteTracker, TrackConfidence, HeatmapLayer,
labelmap.collapse) are imported directly.

Prints one line per frame with proposals/detections/tracks/per-layer
rejections so we can see at a glance which stage is eating events.

Usage:
    python scripts/detect_replay.py path/to/segment.mp4 [--limit N]
"""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np
from scipy import ndimage

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from backend.classification.dfine import DFineDetector  # noqa: E402
from backend.classification.labelmap import collapse as collapse_coco_label  # noqa: E402
from backend.motion.confidence import TrackConfidence  # noqa: E402
from backend.motion.heatmap import HeatmapLayer  # noqa: E402
from backend.motion.tracker import ByteTracker, Detection as TrackerDetection, Track  # noqa: E402

# --- Constants copied from backend/motion/detector.py --------------------
_BG_ALPHA_IDLE = 0.02
_BG_ALPHA_SPIKE = 0.10
_FG_THRESHOLD = 25
_MIN_COMPONENT_AREA = 400
_SCENE_CHANGE_SIGMA = 3.0
_SCENE_CHANGE_EWMA_ALPHA = 0.1
_SCENE_CHANGE_MIN_SAMPLES = 5
_MORPH_KERNEL = np.ones((3, 3), dtype=np.uint8)

_L1_MIN_DIM_PX = 8
_L1_ASPECT_MIN = 0.1
_L1_ASPECT_MAX = 10.0
_L1_MAX_FRAME_FRACTION = 0.7
_L2_MIN_SCORE = 0.3
_L3_MIN_AGE = 2
_STEADY_SCORE_THRESHOLD = 0.4

_MOVEMENT_PERSON_AGE_FRAMES = 20
_MOVEMENT_DIST_FRACTION_OF_H = 1.0


class Pipeline:
    """Stand-in for MotionDetector — just the stages we need for diagnosis."""

    def __init__(self, frame_w: int, frame_h: int, model_path: Path) -> None:
        self.fw = frame_w
        self.fh = frame_h
        self.dfine = DFineDetector(model_path)
        self.tracker = ByteTracker(frame_rate=2)
        self.heatmap = HeatmapLayer(sqlite3.connect(":memory:"), "replay")
        self.confidences: dict[int, TrackConfidence] = {}
        self.last_seen: dict[int, int] = {}
        self.last_emitted: set[int] = set()
        self.observations: dict[int, list[tuple[float, float]]] = {}
        self._bg: np.ndarray | None = None
        self._chg_mean = 0.0
        self._chg_var = 0.0
        self._chg_samples = 0
        self.n = 0
        # State from the last step(), used by draw_annotations().
        self._last_confirmed: list[Track] = []
        self._last_best: tuple[float, Track, str] | None = None
        self._last_stationary_ids: set[int] = set()

    def step(self, frame_rgb: np.ndarray) -> None:
        self.n += 1
        proposals, scene_changed, diag = self._proposals(frame_rgb)

        detections = []
        if proposals and not scene_changed:
            frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
            detections = self.dfine.infer(
                frame_bgr, score_threshold=_STEADY_SCORE_THRESHOLD,
            )

        tracker_input = [
            TrackerDetection(
                x1=d.x1, y1=d.y1, x2=d.x2, y2=d.y2,
                score=d.score, class_id=d.class_id,
            )
            for d in detections
        ]
        confirmed = self.tracker.update(tracker_input)

        # Confidence update + observation logging
        this_frame: set[int] = set()
        for tr in confirmed:
            this_frame.add(tr.track_id)
            conf = self.confidences.setdefault(tr.track_id, TrackConfidence())
            conf.update("high", tr.score)
            self.last_seen[tr.track_id] = self.n
            cx = (tr.x1 + tr.x2) / 2.0
            cy = (tr.y1 + tr.y2) / 2.0
            self.observations.setdefault(tr.track_id, []).append((cx, cy))
        for tid in self.last_emitted - this_frame:
            if tid in self.confidences:
                self.confidences[tid].update("none")
        self.last_emitted = this_frame

        # FP filters 1-6 + labelmap + heatmap weighting.
        # (L7 stationary-NCC and explicit L8 heatmap score are not
        # reproduced here; L8 still applies via heatmap.score_multiplier.)
        rej = {"label": 0, "geom": 0, "score": 0, "age": 0, "conf": 0, "move": 0}
        best: tuple[float, Track, str] | None = None
        stationary_ids: set[int] = set()
        for tr in confirmed:
            label = collapse_coco_label(tr.class_id)
            if label is None:
                rej["label"] += 1
                continue
            if not self._geom_ok(tr):
                rej["geom"] += 1
                continue
            if tr.score < _L2_MIN_SCORE:
                rej["score"] += 1
                continue
            if tr.age < _L3_MIN_AGE:
                rej["age"] += 1
                continue
            conf = self.confidences.get(tr.track_id)
            if conf is None or not conf.emittable:
                rej["conf"] += 1
                continue
            if not self._move_ok(tr, label):
                rej["move"] += 1
                stationary_ids.add(tr.track_id)
                continue
            cx = (tr.x1 + tr.x2) / 2.0
            cy = (tr.y1 + tr.y2) / 2.0
            weight = self.heatmap.score_multiplier(cx, cy, self.fw, self.fh)
            weighted = conf.p_hat * weight
            if best is None or weighted > best[0]:
                best = (weighted, tr, label)

        self._last_confirmed = list(confirmed)
        self._last_best = best
        self._last_stationary_ids = stationary_ids

        if proposals or detections or scene_changed or self.n % 20 == 0:
            top = sorted(
                ((d.class_name, d.score) for d in detections),
                key=lambda x: -x[1],
            )[:3]
            top_s = ", ".join(f"{n}:{s:.2f}" for n, s in top)
            emit = f"{best[2]}@{best[0]:.2f}" if best else "-"
            rej_s = ",".join(f"{k}={v}" for k, v in rej.items() if v)
            # Top-3 tracks by score with their Beta posterior state.
            ranked = sorted(confirmed, key=lambda t: -t.score)[:3]
            tinfo = []
            for tr in ranked:
                conf = self.confidences.get(tr.track_id)
                if conf is None:
                    continue
                tinfo.append(
                    f"id{tr.track_id}(age{tr.age},s{tr.score:.2f},"
                    f"p{conf.p_hat:.2f}/lo{conf.p_hat_lo:.2f},{conf.state})"
                )
            tinfo_s = " ".join(tinfo)
            # Fresh tracks (age < 5) — candidates for new moving objects.
            fresh = [t for t in confirmed if t.age < 5]
            finfo = []
            for tr in fresh:
                conf = self.confidences.get(tr.track_id)
                if conf is None:
                    continue
                finfo.append(
                    f"id{tr.track_id}(age{tr.age},s{tr.score:.2f},"
                    f"p{conf.p_hat:.2f}/lo{conf.p_hat_lo:.2f},{conf.state})"
                )
            fresh_s = (" fresh=[" + ", ".join(finfo) + "]") if finfo else ""
            print(
                f"f={self.n:04d} mean_chg={diag['mean_change']:.2f} "
                f"fg_px={diag['fg_pixels']:d} ncomp={diag['n_components']:d} "
                f"props={len(proposals):d} scene_chg={scene_changed} "
                f"dets={len(detections):d} tracks={len(confirmed):d} "
                f"top=[{top_s}] emit={emit} rej=[{rej_s}] {tinfo_s}{fresh_s}",
                flush=True,
            )

    def _proposals(
        self, frame_rgb: np.ndarray
    ) -> tuple[list[tuple[int, int, int, int]], bool, dict]:
        diag = {"mean_change": 0.0, "fg_pixels": 0, "n_components": 0}
        gray = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
        if self._bg is None:
            self._bg = gray.copy()
            return [], False, diag

        diff = np.abs(gray - self._bg)
        mean_change = float(diff.mean())
        diag["mean_change"] = mean_change

        self._chg_samples += 1
        if self._chg_samples == 1:
            self._chg_mean = mean_change
        else:
            delta = mean_change - self._chg_mean
            self._chg_mean += _SCENE_CHANGE_EWMA_ALPHA * delta
            self._chg_var = (
                (1 - _SCENE_CHANGE_EWMA_ALPHA) * self._chg_var
                + _SCENE_CHANGE_EWMA_ALPHA * (delta * delta)
            )

        scene_changed = False
        if self._chg_samples > _SCENE_CHANGE_MIN_SAMPLES:
            sigma = float(np.sqrt(self._chg_var)) if self._chg_var > 0 else 0.0
            if sigma > 0 and mean_change - self._chg_mean > _SCENE_CHANGE_SIGMA * sigma:
                scene_changed = True

        if scene_changed:
            return [], True, diag

        mask = (diff > _FG_THRESHOLD).astype(np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, _MORPH_KERNEL)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, _MORPH_KERNEL)
        diag["fg_pixels"] = int(mask.sum())

        labeled, n_labels = ndimage.label(mask)
        diag["n_components"] = int(n_labels)
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
                if w * h < _MIN_COMPONENT_AREA:
                    continue
                proposals.append((int(x1), int(y1), int(w), int(h)))

        alpha = _BG_ALPHA_SPIKE if proposals else _BG_ALPHA_IDLE
        self._bg = alpha * gray + (1 - alpha) * self._bg
        return proposals, False, diag

    def _geom_ok(self, tr: Track) -> bool:
        w = tr.x2 - tr.x1
        h = tr.y2 - tr.y1
        if w < _L1_MIN_DIM_PX or h < _L1_MIN_DIM_PX:
            return False
        aspect = w / max(h, 1e-6)
        if aspect < _L1_ASPECT_MIN or aspect > _L1_ASPECT_MAX:
            return False
        frac = (w * h) / float(self.fw * self.fh)
        if frac > _L1_MAX_FRAME_FRACTION:
            return False
        return True

    def draw_annotations(self, frame_bgr: np.ndarray) -> np.ndarray:
        """Return an annotated copy of frame_bgr for video output.
        Green thick box = emitted track. Yellow thin box = stationary
        (confirmed but killed by movement gate — i.e. parked cars).
        """
        out = frame_bgr.copy()
        for tr in self._last_confirmed:
            x1, y1 = int(tr.x1), int(tr.y1)
            x2, y2 = int(tr.x2), int(tr.y2)
            label = collapse_coco_label(tr.class_id) or "?"
            is_best = self._last_best is not None and self._last_best[1] is tr
            if is_best:
                color, thickness = (0, 255, 0), 2
                tag = f"{label} emit {self._last_best[0]:.2f}"
            elif tr.track_id in self._last_stationary_ids:
                color, thickness = (0, 255, 255), 1
                tag = f"{label} parked"
            else:
                continue
            cv2.rectangle(out, (x1, y1), (x2, y2), color, thickness)
            cv2.putText(
                out, tag, (x1, max(y1 - 4, 10)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1, cv2.LINE_AA,
            )
        header = f"f={self.n} confirmed={len(self._last_confirmed)} emit={'Y' if self._last_best else 'N'}"
        cv2.putText(
            out, header, (8, 18),
            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA,
        )
        return out

    def _move_ok(self, tr: Track, label: str) -> bool:
        obs = self.observations.get(tr.track_id)
        if not obs or len(obs) < 2:
            return True
        dx = obs[-1][0] - obs[0][0]
        dy = obs[-1][1] - obs[0][1]
        distance = (dx * dx + dy * dy) ** 0.5
        box_h = max(tr.y2 - tr.y1, 1.0)
        stationary = distance < _MOVEMENT_DIST_FRACTION_OF_H * box_h
        if not stationary:
            return True
        if label == "person" and tr.age >= _MOVEMENT_PERSON_AGE_FRAMES:
            return True
        return False


def probe_dims(segment: Path) -> tuple[int, int]:
    r = subprocess.run(
        ["ffprobe", "-v", "error",
         "-select_streams", "v:0",
         "-show_entries", "stream=width,height",
         "-of", "csv=p=0:s=x", str(segment)],
        check=True, capture_output=True, text=True,
    )
    w, h = r.stdout.strip().split("x")
    return int(w), int(h)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("segment", type=Path)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument(
        "--out-video", type=Path, default=None,
        help="Write annotated MP4 (green=emit, yellow=parked-rejected)",
    )
    args = p.parse_args()

    if not args.segment.is_file():
        print(f"segment not found: {args.segment}", file=sys.stderr)
        return 2

    src_w, src_h = probe_dims(args.segment)
    out_w = 640
    out_h = round(src_h * out_w / src_w / 2) * 2
    print(f"[replay] src={src_w}x{src_h} detect={out_w}x{out_h}", file=sys.stderr)

    model_path = REPO_ROOT / "backend" / "classification" / "models" / "dfine_n.onnx"
    pipe = Pipeline(out_w, out_h, model_path)
    frame_bytes = out_w * out_h * 3

    proc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-i", str(args.segment),
         "-vf", "scale=640:-2,fps=2",
         "-pix_fmt", "rgb24",
         "-f", "rawvideo", "pipe:1"],
        stdout=subprocess.PIPE,
    )
    assert proc.stdout is not None

    encoder: subprocess.Popen | None = None
    if args.out_video is not None:
        args.out_video.parent.mkdir(parents=True, exist_ok=True)
        encoder = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "rawvideo", "-pix_fmt", "bgr24",
             "-s", f"{out_w}x{out_h}", "-r", "2",
             "-i", "pipe:0",
             "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart",
             str(args.out_video)],
            stdin=subprocess.PIPE,
        )
        print(f"[replay] annotated video → {args.out_video}", file=sys.stderr)

    try:
        n = 0
        while True:
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            frame = np.frombuffer(buf, dtype=np.uint8).reshape(out_h, out_w, 3)
            pipe.step(frame)
            if encoder is not None and encoder.stdin is not None:
                annotated = pipe.draw_annotations(
                    cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
                )
                encoder.stdin.write(annotated.tobytes())
            n += 1
            if args.limit is not None and n >= args.limit:
                break
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        if encoder is not None and encoder.stdin is not None:
            encoder.stdin.close()
            try:
                encoder.wait(timeout=30)
            except subprocess.TimeoutExpired:
                encoder.kill()
                encoder.wait()

    print(f"[replay] processed {n} frames", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
