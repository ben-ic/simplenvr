"""D-FINE-N COCO detector — ORT wrapper.

Thin wrapper around the D-FINE-N ONNX model exported with the in-graph
post-processor (NMS-free, 300 queries). Preprocessing and letterbox math
mirror the spike at ``experiments/d-fine-spike/g3_eyeball.py`` exactly;
don't drift from it without re-validating on real frames.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import cv2
import numpy as np
import onnxruntime as ort

logger = logging.getLogger(__name__)

_INPUT_SIZE = 640


@dataclass(slots=True, frozen=True)
class Detection:
    class_id: int
    class_name: str
    x1: float
    y1: float
    x2: float
    y2: float
    score: float


class DFineDetector:
    """D-FINE-N detector. COCO 80-class, 640x640 letterboxed input.

    The model's post-processor emits boxes in ``orig_target_sizes`` pixel
    coords, so we feed ``[[640, 640]]`` and undo the letterbox ourselves.
    """

    COCO_NAMES: ClassVar[tuple[str, ...]] = (
        "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck",
        "boat", "traffic light", "fire hydrant", "stop sign", "parking meter", "bench",
        "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra",
        "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
        "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove",
        "skateboard", "surfboard", "tennis racket", "bottle", "wine glass", "cup",
        "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
        "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
        "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
        "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
        "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
        "hair drier", "toothbrush",
    )

    SURVEILLANCE_CLASSES: ClassVar[frozenset[str]] = frozenset({
        "person", "bicycle", "car", "motorcycle", "bus", "truck",
        "cat", "dog", "bird", "horse", "sheep", "cow", "bear",
    })

    def __init__(
        self,
        model_path: Path | str,
        *,
        num_threads: int | None = None,
    ) -> None:
        self._model_path = Path(model_path)
        if not self._model_path.is_file():
            raise FileNotFoundError(f"D-FINE model not found: {self._model_path}")

        sess_options = ort.SessionOptions()
        if num_threads is not None:
            sess_options.intra_op_num_threads = int(num_threads)

        self._session = ort.InferenceSession(
            str(self._model_path),
            sess_options=sess_options,
            providers=["CPUExecutionProvider"],
        )
        # Post-processor targets the letterboxed canvas size.
        self._target_sizes = np.array([[_INPUT_SIZE, _INPUT_SIZE]], dtype=np.int64)

        # Warm up so the first real call isn't cold.
        dummy = np.zeros((1, 3, _INPUT_SIZE, _INPUT_SIZE), dtype=np.float32)
        try:
            self._session.run(
                None,
                {"images": dummy, "orig_target_sizes": self._target_sizes},
            )
        except Exception:
            logger.exception("D-FINE warmup inference failed")
            raise

    def infer(
        self,
        frame_bgr: np.ndarray,
        *,
        score_threshold: float = 0.4,
    ) -> list[Detection]:
        """Detect objects in a BGR frame. Returns boxes in original-frame coords."""
        if frame_bgr.ndim != 3 or frame_bgr.shape[2] != 3:
            raise ValueError(f"expected HxWx3 BGR frame, got shape {frame_bgr.shape}")

        tensor, ratio, pad_w, pad_h = _preprocess(frame_bgr)
        labels, boxes, scores = self._session.run(
            None,
            {"images": tensor, "orig_target_sizes": self._target_sizes},
        )
        return _postprocess(
            labels, boxes, scores, ratio, pad_w, pad_h, score_threshold, self.COCO_NAMES,
        )


def _preprocess(
    img_bgr: np.ndarray,
    target: int = _INPUT_SIZE,
) -> tuple[np.ndarray, float, int, int]:
    """Letterbox to target x target on a black canvas, /255, BGR->RGB, NCHW."""
    h, w = img_bgr.shape[:2]
    ratio = min(target / w, target / h)
    new_w, new_h = int(w * ratio), int(h * ratio)
    resized = cv2.resize(img_bgr, (new_w, new_h), interpolation=cv2.INTER_LINEAR)

    canvas = np.zeros((target, target, 3), dtype=np.uint8)
    pad_w = (target - new_w) // 2
    pad_h = (target - new_h) // 2
    canvas[pad_h : pad_h + new_h, pad_w : pad_w + new_w] = resized

    rgb = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)
    tensor = (rgb.astype(np.float32) / 255.0).transpose(2, 0, 1)[None, ...]
    return tensor, ratio, pad_w, pad_h


def _postprocess(
    labels: np.ndarray,
    boxes: np.ndarray,
    scores: np.ndarray,
    ratio: float,
    pad_w: int,
    pad_h: int,
    score_threshold: float,
    class_names: tuple[str, ...],
) -> list[Detection]:
    """Filter by score and undo the letterbox. Boxes NOT clipped to frame."""
    out: list[Detection] = []
    num_classes = len(class_names)
    for lab, box, scr in zip(labels[0], boxes[0], scores[0]):
        if scr < score_threshold:
            continue
        cid = int(lab)
        name = class_names[cid] if 0 <= cid < num_classes else f"cls{cid}"
        out.append(Detection(
            class_id=cid,
            class_name=name,
            x1=(float(box[0]) - pad_w) / ratio,
            y1=(float(box[1]) - pad_h) / ratio,
            x2=(float(box[2]) - pad_w) / ratio,
            y2=(float(box[3]) - pad_h) / ratio,
            score=float(scr),
        ))
    return out
