"""YOLOX-S / YOLOX-Nano ONNX classifier — pure inference layer.

This module owns the ONNX Runtime session and the per-track median-of-
frames scoring logic. It knows nothing about the event bus, the DB, or
the motion pipeline — it takes a Track (with its frame_refs list of
JPEG bytes) and returns a ClassificationResult.

Design contract (locked in Phase 2 planning):

  * **Bbox crop + 30 % margin, letterboxed to model input.** Empirically
    validated 2026-04-09 against the full-frame baseline: +0.20 to +0.40
    confidence on the same objects. The classifier is running on preview
    JPEGs (~320 wide) in Phase 2; margins make sure a slightly-off MOG2
    bbox still contains the whole object.

  * **Scoring skips NMS entirely.** Megvii's yolox_s.onnx is the raw
    grid head: output shape [1, 8400, 85] where 85 = [cx, cy, w, h,
    obj, class_0..class_79]. Per frame we compute obj × class for every
    anchor, take the max over the anchors for each of our three product
    labels (via the labelmap's COCO→label collapse), and that frame's
    score for each label is the result. No NMS because the crop has
    already done the spatial localization — we're classifying a known
    region, not detecting in an unknown scene.

  * **Median-of-track across frames, argmax across labels.** For each
    label in {person, vehicle, animal}, we take the median of the
    per-frame scores across all frames in Track.frame_refs. The label
    with the highest median wins. If the winning median is below
    _MIN_CONFIDENCE, the result is (None, 0.0) — silent fallback.

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
from pathlib import Path

from .labelmap import ObjectLabel, _COCO_TO_LABEL, all_labels
from ..motion.tracker import Track

logger = logging.getLogger(__name__)

# Minimum median confidence to promote a label. Below this, the track
# gets object_class=NULL and the Inbox row stays at "Motion at X".
#
# Tuned 2026-04-09 PM against replay of stored tracks on live cameras:
# YOLOX-S on 320-wide preview JPEGs correctly identifies a real car at
# top-1 score ~0.26. A threshold of 0.30 is above what the model can
# reach on preview-resolution input, so no positive label would ever
# land even on obvious objects. 0.20 sits cleanly between the noise
# floor we observed (0.008-0.15 on MOG2 fragment tracks) and the
# signal floor we observed (0.20-0.26 on real vehicles). Revisit if
# we ever switch the classifier input to the full-resolution recording
# frames — the old 0.30 threshold was calibrated on higher-res crops
# and will want to come back up.
_MIN_CONFIDENCE = 0.20

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

    async def classify_track(self, track: Track) -> ClassificationResult:
        """Run inference on every frame in track.frame_refs and return
        the per-label median-of-track verdict.

        Executed off-loop via run_in_executor — the ORT forward is the
        hot path and we don't want to block other cameras' consume
        loops while one track is being labeled.
        """
        import asyncio
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._classify_sync, track)

    # ------------------------------------------------------------------
    # Synchronous core (runs in executor)
    # ------------------------------------------------------------------

    def _classify_sync(self, track: Track) -> ClassificationResult:
        # Soft-dep guards: if cv2/numpy aren't importable we should never
        # have been constructed in the first place (the probe path would
        # have been disabled), but belt-and-suspenders.
        try:
            import cv2  # type: ignore
            import numpy as np  # type: ignore
        except ImportError:
            return ClassificationResult(label=None, confidence=0.0)

        frames: list[bytes] = [f for f in track.frame_refs if isinstance(f, (bytes, bytearray))]
        if not frames:
            # Detector didn't stuff any frames into the track — most
            # likely because the track closed before the detector
            # captured a frame at promotion time. Silent fallback.
            return ClassificationResult(label=None, confidence=0.0)

        # Per-label scores: one list per label, one entry per frame.
        # Only frames that successfully decode contribute. Frames that
        # fail to decode or crop are skipped, not penalized.
        per_label: dict[ObjectLabel, list[float]] = {
            label: [] for label in all_labels()
        }

        for jpeg in frames:
            try:
                frame_scores = self._score_one_frame(jpeg, track.bbox, cv2, np)
            except Exception as e:
                logger.debug("classify frame failed: %s", e)
                continue
            if frame_scores is None:
                continue
            for label in all_labels():
                per_label[label].append(frame_scores[label])

        # If every frame failed to decode we have nothing to work with.
        if not any(per_label[label] for label in all_labels()):
            return ClassificationResult(label=None, confidence=0.0)

        # Median-of-track per label, then argmax across labels.
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

        if best_label is None or best_median < _MIN_CONFIDENCE:
            # Silent fallback — load-bearing. Row stays "Motion at X".
            return ClassificationResult(label=None, confidence=float(best_median))

        return ClassificationResult(label=best_label, confidence=float(best_median))

    # ------------------------------------------------------------------
    # Per-frame scoring
    # ------------------------------------------------------------------

    def _score_one_frame(
        self,
        jpeg: bytes,
        bbox: tuple[int, int, int, int],
        cv2,
        np,
    ) -> dict[ObjectLabel, float] | None:
        """Decode JPEG → letterbox full frame → infer → per-label max
        score across all anchors. Returns a dict with an entry for
        every label (0.0 if no anchor cleared for that label on this
        frame). Returns None if decode fails.

        Scoring is full-frame, not cropped. Empirical replay on live
        stored tracks 2026-04-09 showed that cropping a 320-wide
        preview JPEG to an 81×97 MOG2 bbox (even with 30 % margin)
        loses enough context that YOLOX-S latches onto nonsense
        classes, while the same frame un-cropped correctly puts the
        real object at the top. The `bbox` argument is kept in the
        signature as a debugging hook for when we revisit crop-based
        scoring on full-resolution recording frames (Phase 2.5).
        """
        del bbox  # intentionally unused in the full-frame scoring path
        arr = np.frombuffer(jpeg, dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if frame is None:
            return None

        input_tensor = self._preprocess(frame, cv2, np)
        outputs = self._sess.run(None, {self._input_name: input_tensor})
        # YOLOX ONNX head output is [1, N, 85] where N=8400 for 640x640
        # and 3549 for 416x416. We only need (obj * class_conf) → per
        # anchor; NMS is skipped because the crop already localized.
        preds = outputs[0][0]  # [N, 85]
        if preds.shape[-1] < 85:
            return None
        obj = preds[:, 4:5]            # [N, 1]
        cls = preds[:, 5:]             # [N, 80]
        scores = obj * cls             # [N, 80]

        # For each of our three labels, find the max across anchors and
        # across the COCO ids that collapse to that label.
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
