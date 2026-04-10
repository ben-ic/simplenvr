"""YAMNet ONNX classifier — pure inference layer.

Takes a 0.96s PCM window (15360 samples at 16kHz, s16le) and returns a
(SoundLabel, confidence) or (None, 0.0). Knows nothing about the event
bus or DB — the manager handles those concerns.

YAMNet is ~4 MB and runs in ~10ms on CPU. No hardware tiering needed
(unlike YOLOX which varies 10x across tiers).
"""

from __future__ import annotations

import logging
import struct
from pathlib import Path

import numpy as np

from .labelmap import SoundLabel, map_class, is_high_priority

logger = logging.getLogger(__name__)

_MODEL_DIR = Path(__file__).resolve().parent.parent / "classification" / "models"
_MODEL_PATH = _MODEL_DIR / "yamnet.onnx"
_CLASSES_PATH = _MODEL_DIR / "yamnet_classes.txt"

# Confidence thresholds. High-priority classes (gunshot, glass break)
# need high confidence to avoid false alarms. Low-priority classes
# (bark, footsteps) can be more permissive since they only enrich
# existing vision events and never fire alone.
_THRESHOLD_HIGH = 0.50
_THRESHOLD_LOW = 0.30

# Number of samples in a 0.96s window at 16kHz
_WINDOW_SAMPLES = 15360


class YamnetClassifier:
    def __init__(self) -> None:
        self._session = None
        self._class_names: list[str] = []
        self._input_name: str = ""
        self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def start(self) -> None:
        if not _MODEL_PATH.exists():
            logger.info("YAMNet model not found at %s — audio classification disabled", _MODEL_PATH)
            return
        if not _CLASSES_PATH.exists():
            logger.info("YAMNet class map not found at %s — audio classification disabled", _CLASSES_PATH)
            return

        try:
            import onnxruntime as ort
        except ImportError:
            logger.info("onnxruntime not available — audio classification disabled")
            return

        try:
            self._class_names = _CLASSES_PATH.read_text().strip().splitlines()
            logger.info("Loaded %d YAMNet class names", len(self._class_names))
        except Exception as e:
            logger.error("Failed to load YAMNet class names: %s", e)
            return

        try:
            opts = ort.SessionOptions()
            opts.inter_op_num_threads = 1
            opts.intra_op_num_threads = 1
            self._session = ort.InferenceSession(
                str(_MODEL_PATH),
                sess_options=opts,
                providers=["CPUExecutionProvider"],
            )
            inp = self._session.get_inputs()[0]
            self._input_name = inp.name
            logger.info(
                "YAMNet loaded: input=%s shape=%s, %d classes",
                inp.name, inp.shape, len(self._class_names),
            )
            self._enabled = True
        except Exception as e:
            logger.error("Failed to load YAMNet ONNX model: %s", e)

    def classify(self, pcm_window: bytes) -> tuple[SoundLabel | None, float]:
        """Classify a 0.96s PCM s16le 16kHz mono window.

        Returns (label, confidence) where label is None if nothing
        recognized or below threshold.
        """
        if not self._enabled or self._session is None:
            return (None, 0.0)

        # Convert s16le bytes → float32 normalized to [-1, 1]
        n_samples = len(pcm_window) // 2
        samples = np.frombuffer(pcm_window, dtype=np.int16).astype(np.float32)
        if len(samples) < _WINDOW_SAMPLES:
            # Pad short windows with silence
            samples = np.pad(samples, (0, _WINDOW_SAMPLES - len(samples)))
        elif len(samples) > _WINDOW_SAMPLES:
            samples = samples[:_WINDOW_SAMPLES]
        samples = samples / 32768.0

        # Reshape to match model input. Standard YAMNet expects [N] or [1, N].
        inp_shape = self._session.get_inputs()[0].shape
        if len(inp_shape) == 2:
            waveform = samples.reshape(1, -1)
        else:
            waveform = samples

        try:
            outputs = self._session.run(None, {self._input_name: waveform})
        except Exception as e:
            logger.debug("YAMNet inference failed: %s", e)
            return (None, 0.0)

        # Output shape varies by export. Common shapes:
        # [1, 521] — single window scores
        # [M, 521] — M sub-windows (if model does its own framing)
        scores = outputs[0]
        if scores.ndim > 1:
            # Average across sub-windows if multiple
            scores = scores.mean(axis=0) if scores.shape[0] > 1 else scores[0]

        if len(scores) == 0 or len(scores) != len(self._class_names):
            return (None, 0.0)

        # Find the best mapped class
        best_label: SoundLabel | None = None
        best_conf: float = 0.0

        for idx in np.argsort(scores)[::-1][:20]:
            conf = float(scores[idx])
            name = self._class_names[idx]
            label = map_class(name)
            if label is None:
                continue

            threshold = _THRESHOLD_HIGH if is_high_priority(label) else _THRESHOLD_LOW
            if conf >= threshold and conf > best_conf:
                best_label = label
                best_conf = conf
                break  # Top match wins

        return (best_label, best_conf)
