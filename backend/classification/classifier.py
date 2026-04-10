"""YOLOX-S / YOLOX-Nano ONNX classifier — pure inference layer.

This module owns the ONNX Runtime session and the per-track median-of-
frames scoring logic. It knows nothing about the event bus, the DB, or
the motion pipeline — it takes a Track (with its frame_refs list of
JPEG bytes) and returns a ClassificationResult.

Design contract:

  * **Full-res crop + 30 % margin (Phase 2.5).** When a recording file
    is available, the classifier extracts full-resolution frames from the
    MP4, crops to the tracked bbox with 30 % margin, and scores the crop.
    Confidence is +0.20 to +0.40 higher than on 320-wide preview JPEGs.
    Falls back to preview-JPEG full-frame scoring when no recording is
    available.

  * **Scoring skips NMS entirely.** Megvii's yolox_s.onnx is the raw
    grid head: output shape [1, 8400, 85] where 85 = [cx, cy, w, h,
    obj, class_0..class_79]. Per frame we compute obj × class for every
    anchor, take the max over the anchors for each of our three product
    labels (via the labelmap's COCO→label collapse), and that frame's
    score for each label is the result. No NMS because we only need
    scalar per-label confidence, not bounding boxes from the model.

  * **Median-of-track across frames, argmax across labels.** For each
    label in {person, vehicle, animal}, we take the median of the
    per-frame scores across all frames sampled. The label with the
    highest median wins. If the winning median is below the confidence
    threshold, the result is (None, 0.0) — silent fallback.

  * **Silent fallback is first-class.** Returning None is not an error.
    It means "we looked and we're not confident enough to put a label
    on this" and the caller keeps the row as "Motion at X". This is
    the product invariant that makes the whole subsystem safe to ship.

No confidence number is ever surfaced to the user. The number exists
only so the manager can pick the highest-confidence track in a
multi-track scene; the Inbox never renders it.
"""
from __future__ import annotations

import logging
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .labelmap import ObjectLabel, _COCO_TO_LABEL, all_labels
from ..motion.tracker import Track

logger = logging.getLogger(__name__)

# Full-res crop scoring (Phase 2.5). The confidence boost from cropping
# a full-resolution recording frame to the tracked bbox lets us raise
# the threshold from the preview-era 0.20. Empirically, real objects
# land at 0.35-0.55 on tight crops while the noise floor stays at
# 0.005-0.08 (the 114-gray letterbox padding produces near-zero
# objectness).
_MIN_CONFIDENCE_FULLRES = 0.30

# Preview-JPEG fallback (Phase 2 legacy). Used when no recording file
# is available. 0.20 is the signal floor for YOLOX-S on 320-wide
# full-frame input — below this, real objects are indistinguishable
# from noise on MOG2 fragment tracks.
_MIN_CONFIDENCE_PREVIEW = 0.20

# Minimum crop size (in recording-resolution pixels) before falling
# back to full-frame scoring. Below 48px the YOLOX stride-8 head
# can't reliably classify the upscaled content.
_MIN_CROP_PX = 48

# Fraction of pixels that must be "decoder green" to consider a frame
# corrupt (H.264 macroblock fill from lost RTSP packets).
_GREEN_CORRUPT_THRESHOLD = 0.15

# YOLOX preprocessing constants. YOLOX uses no mean subtraction and no
# /255 normalization — the model was trained on raw uint8→float32
# pixel values with 114 as the letterbox pad color. See the Megvii
# repo's demo/ONNXRuntime/onnx_inference.py for the reference
# implementation this mirrors.
_PAD_COLOR = 114.0

# Model-specific input sizes by tier. The capability probe writes
# classification_tier into settings; the manager maps it to one of
# these. weak-tier installs use yolox_nano.onnx at 416 to keep the
# latency budget under 150 ms per frame.
MODEL_INPUTS: dict[str, tuple[str, int]] = {
    "strong": ("yolox_s.onnx", 640),
    "normal": ("yolox_s.onnx", 640),
    "modest": ("yolox_s.onnx", 640),
    "weak": ("yolox_nano.onnx", 416),
}

# ORT provider name map (shared shape with capability_probe).
_EP_TO_ORT: dict[str, str] = {
    "coreml": "CoreMLExecutionProvider",
    "qnn": "QNNExecutionProvider",
    "directml": "DmlExecutionProvider",
    "openvino": "OpenVINOExecutionProvider",
    "cuda": "CUDAExecutionProvider",
    "cpu": "CPUExecutionProvider",
}


@dataclass
class ClassificationResult:
    """What the classifier hands back per-track. `label=None` is a
    first-class silent-fallback result, not an error."""
    label: ObjectLabel | None
    confidence: float


@dataclass
class RecordingContext:
    """Recording segment data for full-resolution crop scoring.

    Passed from the manager (async, does the DB lookup) to the
    classifier sync path so file I/O stays off the event loop.
    """
    file_path: str
    started_at_utc: datetime
    duration_s: float | None  # None if segment is still in_progress


def _bundled_model_dir() -> Path:
    """Same PyInstaller-aware path resolution as capability_probe."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "backend" / "classification" / "models"  # type: ignore[attr-defined]
    return Path(__file__).parent / "models"


class YoloxClassifier:
    """Owns the ORT session for the lifetime of the process.

    One instance per backend process. `classify_track()` is async but
    runs the CPU/NPU work in the default executor so the event loop
    stays responsive — a single YOLOX-S forward is ~30 ms on M4
    CoreML, ~10 ms on Snapdragon QNN, up to ~150 ms on CPU fallback.
    """

    def __init__(self, tier: str, ep: str):
        self._tier = tier
        self._ep = ep

        model_name, input_size = MODEL_INPUTS.get(tier, MODEL_INPUTS["normal"])
        model_path = _bundled_model_dir() / model_name
        if not model_path.exists():
            raise FileNotFoundError(
                f"classifier model not bundled: {model_path}"
            )

        # Import here so an install without onnxruntime can still import
        # this module — it only blows up when you actually try to
        # instantiate the classifier, which only happens when the probe
        # said tier != disabled.
        import onnxruntime as ort  # type: ignore

        ort_provider = _EP_TO_ORT.get(ep, "CPUExecutionProvider")
        try:
            self._sess = ort.InferenceSession(
                str(model_path), providers=[ort_provider]
            )
        except Exception as e:
            logger.warning(
                "classifier EP=%s failed to load %s, falling back to CPU: %s",
                ep, model_name, e,
            )
            self._sess = ort.InferenceSession(
                str(model_path), providers=["CPUExecutionProvider"]
            )

        self._input_name = self._sess.get_inputs()[0].name
        self._input_size = input_size
        self._model_name = model_name
        logger.info(
            "classifier loaded: model=%s input=%d ep=%s actual=%s",
            model_name, input_size, ep, self._sess.get_providers(),
        )

    async def classify_track(
        self,
        track: Track,
        recording_ctx: RecordingContext | None = None,
    ) -> ClassificationResult:
        """Run inference and return the per-label median-of-track verdict.

        When *recording_ctx* is provided, full-resolution frames are
        extracted from the recording MP4, cropped to bbox, and scored
        at the higher 0.30 threshold.  Falls back to the 320-wide
        preview JPEGs in track.frame_refs at 0.20 if the recording
        path fails for any reason.
        """
        import asyncio
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._classify_sync, track, recording_ctx
        )

    # ------------------------------------------------------------------
    # Synchronous core (runs in executor)
    # ------------------------------------------------------------------

    def _classify_sync(
        self,
        track: Track,
        recording_ctx: RecordingContext | None,
    ) -> ClassificationResult:
        try:
            import cv2  # type: ignore
            import numpy as np  # type: ignore
        except ImportError:
            return ClassificationResult(label=None, confidence=0.0)

        # --- Full-res recording path (Phase 2.5) ----------------------
        if recording_ctx is not None:
            try:
                result = self._score_from_recording(
                    track, recording_ctx, cv2, np
                )
                if result is not None:
                    return result
                logger.debug(
                    "full-res scoring yielded no frames, "
                    "falling back to preview: track=%s", track.id,
                )
            except Exception as e:
                logger.warning(
                    "full-res scoring failed, falling back to preview: "
                    "track=%s err=%s", track.id, e,
                )

        # --- Preview-JPEG fallback (Phase 2 legacy) -------------------
        frames: list[bytes] = [
            f for f in track.frame_refs if isinstance(f, (bytes, bytearray))
        ]
        if not frames:
            return ClassificationResult(label=None, confidence=0.0)

        per_label: dict[ObjectLabel, list[float]] = {
            label: [] for label in all_labels()
        }
        for jpeg in frames:
            try:
                frame_scores = self._score_one_frame(jpeg, cv2, np)
            except Exception as e:
                logger.debug("classify frame failed: %s", e)
                continue
            if frame_scores is None:
                continue
            for label in all_labels():
                per_label[label].append(frame_scores[label])

        return self._median_verdict(per_label, _MIN_CONFIDENCE_PREVIEW)

    # ------------------------------------------------------------------
    # Full-resolution recording path (Phase 2.5)
    # ------------------------------------------------------------------

    def _score_from_recording(
        self,
        track: Track,
        ctx: RecordingContext,
        cv2,
        np,
    ) -> ClassificationResult | None:
        """Open the recording MP4, sample 3 frames across the track's
        lifetime, crop to the per-frame bbox with 30 % margin, and score
        each crop.  Returns None to signal "try the preview fallback."
        """
        cap = cv2.VideoCapture(ctx.file_path)
        if not cap.isOpened():
            logger.warning("VideoCapture failed to open: %s", ctx.file_path)
            return None

        try:
            cap_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            cap_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            if cap_w <= 0 or cap_h <= 0:
                return None

            # Scale from 320px preview space to recording resolution.
            # The preview pipeline uses scale=320:-2 which preserves
            # aspect ratio, so x and y scale factors are equal.
            scale = cap_w / 320.0

            samples = self._compute_seek_samples(track, ctx)
            if not samples:
                return None

            per_label: dict[ObjectLabel, list[float]] = {
                label: [] for label in all_labels()
            }
            for offset_ms, bbox in samples:
                cap.set(cv2.CAP_PROP_POS_MSEC, offset_ms)
                ret, frame = cap.read()
                if not ret or frame is None:
                    continue
                if self._is_corrupt_green(frame, np):
                    continue
                crop = self._crop_bbox(frame, bbox, scale, cap_w, cap_h)
                if crop is None:
                    continue
                scores = self._score_array(crop, cv2, np)
                if scores is None:
                    continue
                for label in all_labels():
                    per_label[label].append(scores[label])

            if not any(per_label[label] for label in all_labels()):
                return None

            return self._median_verdict(per_label, _MIN_CONFIDENCE_FULLRES)
        finally:
            cap.release()

    def _compute_seek_samples(
        self,
        track: Track,
        ctx: RecordingContext,
    ) -> list[tuple[float, tuple[int, int, int, int]]]:
        """Return up to 3 (offset_ms, bbox) pairs for seeking the recording.

        Samples bbox_history at 25 %, 50 %, and 75 % of the track's
        lifetime to get temporal spread while avoiding the noisiest
        first frames and the exiting-frame tail.
        """
        history = track.bbox_history or [track.bbox]
        n = len(history)

        # Sample indices at 25/50/75 % of the history length.
        sample_indices = sorted({
            max(0, n // 4),
            n // 2,
            min(n - 1, n * 3 // 4),
        })

        track_dur = (track.last_seen - track.first_seen).total_seconds()
        seg_offset = (track.first_seen - ctx.started_at_utc).total_seconds()
        max_ms = (ctx.duration_s or 120.0) * 1000.0

        pairs: list[tuple[float, tuple[int, int, int, int]]] = []
        for idx in sample_indices:
            frac = idx / max(n - 1, 1)
            offset_ms = max(0.0, min(
                (seg_offset + track_dur * frac) * 1000.0,
                max_ms,
            ))
            pairs.append((offset_ms, history[idx]))
        return pairs

    @staticmethod
    def _crop_bbox(
        frame,
        bbox: tuple[int, int, int, int],
        scale: float,
        cap_w: int,
        cap_h: int,
    ):
        """Scale bbox from 320px space to recording resolution, apply
        30 % margin, clamp to frame bounds.  Returns None if the crop
        is smaller than _MIN_CROP_PX in either dimension.
        """
        x, y, w, h = bbox
        sx, sy, sw, sh = (
            int(x * scale), int(y * scale),
            int(w * scale), int(h * scale),
        )
        mx = int(sw * 0.30)
        my = int(sh * 0.30)
        x1 = max(0, sx - mx)
        y1 = max(0, sy - my)
        x2 = min(cap_w, sx + sw + mx)
        y2 = min(cap_h, sy + sh + my)
        if (x2 - x1) < _MIN_CROP_PX or (y2 - y1) < _MIN_CROP_PX:
            return None
        return frame[y1:y2, x1:x2]

    @staticmethod
    def _is_corrupt_green(frame, np) -> bool:
        """Detect H.264 macroblock corruption (green fill)."""
        g = frame[:, :, 1].astype(np.int16)
        rb = frame[:, :, 0].astype(np.int16) + frame[:, :, 2].astype(np.int16)
        green_mask = (g > 200) & (rb < 80)
        return float(green_mask.mean()) > _GREEN_CORRUPT_THRESHOLD

    # ------------------------------------------------------------------
    # Shared scoring helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _median_verdict(
        per_label: dict[ObjectLabel, list[float]],
        min_confidence: float,
    ) -> ClassificationResult:
        """Median-of-track per label, argmax across labels."""
        best_label: ObjectLabel | None = None
        best_median = 0.0
        for label in all_labels():
            scores = per_label[label]
            if not scores:
                continue
            median = statistics.median(scores)
            if median > best_median:
                best_median = median
                best_label = label

        if best_label is None or best_median < min_confidence:
            return ClassificationResult(label=None, confidence=float(best_median))
        return ClassificationResult(label=best_label, confidence=float(best_median))

    # ------------------------------------------------------------------
    # Per-frame scoring
    # ------------------------------------------------------------------

    def _score_one_frame(
        self,
        jpeg: bytes,
        cv2,
        np,
    ) -> dict[ObjectLabel, float] | None:
        """Decode preview JPEG → full-frame score (no crop).

        Used only on the preview-JPEG fallback path when no recording
        is available.
        """
        arr = np.frombuffer(jpeg, dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if frame is None:
            return None
        return self._score_array(frame, cv2, np)

    def _score_array(
        self,
        img,
        cv2,
        np,
    ) -> dict[ObjectLabel, float] | None:
        """Letterbox → YOLOX forward → per-label max score.

        Accepts a decoded BGR ndarray at any resolution. Shared by
        both the full-res crop path and the preview-JPEG path.
        """
        input_tensor = self._preprocess(img, cv2, np)
        outputs = self._sess.run(None, {self._input_name: input_tensor})
        preds = outputs[0][0]  # [N, 85]
        if preds.shape[-1] < 85:
            return None
        obj = preds[:, 4:5]            # [N, 1]
        cls = preds[:, 5:]             # [N, 80]
        scores = obj * cls             # [N, 80]

        per_label: dict[ObjectLabel, float] = {label: 0.0 for label in all_labels()}
        for coco_id, label in _COCO_TO_LABEL.items():
            if coco_id >= scores.shape[1]:
                continue
            col_max = float(scores[:, coco_id].max())
            if col_max > per_label[label]:
                per_label[label] = col_max
        return per_label

    def _preprocess(self, img, cv2, np):
        """Letterbox `img` to self._input_size × self._input_size with
        pad color 114 and return a [1, 3, H, W] float32 tensor in BGR
        order. Matches the Megvii reference preprocessing exactly."""
        size = self._input_size
        padded = np.full((size, size, 3), _PAD_COLOR, dtype=np.float32)
        ih, iw = img.shape[:2]
        if ih <= 0 or iw <= 0:
            # Degenerate — just hand the padded blank through.
            padded = padded.transpose(2, 0, 1)
            return np.ascontiguousarray(padded, dtype=np.float32)[None, ...]
        r = min(size / ih, size / iw)
        new_h = max(1, int(ih * r))
        new_w = max(1, int(iw * r))
        resized = cv2.resize(
            img, (new_w, new_h), interpolation=cv2.INTER_LINEAR
        ).astype(np.float32)
        padded[:new_h, :new_w] = resized
        padded = padded.transpose(2, 0, 1)  # HWC → CHW
        return np.ascontiguousarray(padded, dtype=np.float32)[None, ...]
