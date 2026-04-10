"""Moondream 2B inference wrapper.

Owns the HuggingFace model and tokenizer for the lifetime of the
process. Downloads weights on first use to DATA_DIR/models/moondream2.
Inference runs in an executor (off the event loop) — a single
Moondream forward is ~1-3 seconds on M4 MPS, ~5-10 seconds on CPU.

The IR gate skips night/greyscale frames where Moondream confabulates
colors. When the gate fires, the caller gets None and falls back to
the YOLOX label alone.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from ..config import DATA_DIR

logger = logging.getLogger(__name__)

_MODELS_DIR = DATA_DIR / "models"
_MODEL_ID = "vikhyatk/moondream2"
_PROMPT = "Describe what you see in one short sentence."

# IR gate: if the mean absolute difference between R, G, and B
# channels is below this threshold, the frame is greyscale (IR mode)
# and Moondream would confabulate colors.
_IR_CHANNEL_DIVERGENCE = 5.0


@dataclass
class SummaryResult:
    """What the summarizer hands back per-episode."""
    description: str | None  # None = silent fallback (IR, error, etc.)


class MoondreamSummarizer:
    """Owns the HuggingFace model for the lifetime of the process.

    One instance per backend process. `describe_frame()` is async but
    runs inference in the default executor.
    """

    def __init__(self):
        self._model = None
        self._tokenizer = None
        self._loaded = False
        self._load_error: str | None = None

    @property
    def available(self) -> bool:
        return self._loaded and self._model is not None

    def load(self) -> bool:
        """Download weights (if needed) and load the model.

        Returns True if the model is ready for inference. Called from
        the manager's start() method in an executor so the download
        doesn't block the event loop.
        """
        if self._loaded:
            return self._model is not None

        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer

            cache_dir = str(_MODELS_DIR)
            os.makedirs(cache_dir, exist_ok=True)

            logger.info(
                "Loading Moondream 2B (cache_dir=%s)...", cache_dir
            )

            self._tokenizer = AutoTokenizer.from_pretrained(
                _MODEL_ID,
                trust_remote_code=True,
                cache_dir=cache_dir,
            )

            device_map = "mps" if torch.backends.mps.is_available() else "cpu"
            self._model = AutoModelForCausalLM.from_pretrained(
                _MODEL_ID,
                trust_remote_code=True,
                torch_dtype=torch.float16,
                device_map=device_map,
                cache_dir=cache_dir,
            )

            self._loaded = True
            logger.info(
                "Moondream loaded: device=%s", device_map,
            )
            return True

        except Exception as e:
            self._loaded = True  # don't retry on every call
            self._load_error = str(e)
            logger.error(
                "Moondream failed to load: %s", e, exc_info=True,
            )
            return False

    async def describe_frame(self, jpeg: bytes) -> SummaryResult:
        """Run Moondream on a JPEG frame and return a description.

        Returns SummaryResult(description=None) for IR frames, load
        failures, or inference errors.
        """
        import asyncio
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._describe_sync, jpeg,
        )

    def _describe_sync(self, jpeg: bytes) -> SummaryResult:
        if not self.available:
            return SummaryResult(description=None)

        try:
            from PIL import Image
            import io
            import numpy as np

            img = Image.open(io.BytesIO(jpeg)).convert("RGB")

            # IR gate: skip greyscale/IR frames.
            arr = np.array(img, dtype=np.float32)
            if arr.ndim == 3 and arr.shape[2] == 3:
                channel_means = arr.mean(axis=(0, 1))
                divergence = np.abs(
                    channel_means - channel_means.mean()
                ).mean()
                if divergence < _IR_CHANNEL_DIVERGENCE:
                    logger.debug("IR gate: skipping greyscale frame")
                    return SummaryResult(description=None)

            encoded = self._model.encode_image(img)
            answer = self._model.answer_question(
                encoded, _PROMPT, self._tokenizer,
            )
            # Clean up the answer — strip whitespace, ensure single sentence.
            description = answer.strip().rstrip(".")
            if description:
                description += "."

            return SummaryResult(description=description or None)

        except Exception as e:
            logger.warning("Moondream inference failed: %s", e)
            return SummaryResult(description=None)
