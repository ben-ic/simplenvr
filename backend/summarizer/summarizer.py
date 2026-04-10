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
# Brief: one-liner for the Inbox row (≤15 words).
_PROMPT_BRIEF = "In under 15 words: what objects, what action, what direction?"
# Detailed: every vehicle and person with color and count. Comma separated.
_PROMPT_CSV = (
    "List every vehicle and person visible with their color. "
    "Include count if multiple. Include action and direction. "
    "Example: 2 white sedans driving left, 1 red SUV parked, person in black jacket walking right. "
    "Comma separated, nothing else."
)

# IR gate: if the mean absolute difference between R, G, and B
# channels is below this threshold, the frame is greyscale (IR mode)
# and Moondream would confabulate colors.
_IR_CHANNEL_DIVERGENCE = 5.0


@dataclass
class SummaryResult:
    """What the summarizer hands back per event."""
    summary: str | None      # Brief one-liner for Inbox row
    description: str | None  # Detailed CSV for search + template engine


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

    def is_cached(self) -> bool:
        """Check if the model weights already exist on disk."""
        try:
            from huggingface_hub import try_to_load_from_cache
            result = try_to_load_from_cache(
                _MODEL_ID, "config.json", cache_dir=str(_MODELS_DIR)
            )
            return result is not None
        except Exception:
            return False

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
                dtype=torch.float16,
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

    async def describe_event(
        self,
        jpegs: list[bytes],
        duration_s: float = 0,
    ) -> SummaryResult:
        """Run Moondream on 1-3 frames and return summary + description.

        Encodes the image once, then runs two prompts:
          - brief → summary (one-liner for Inbox)
          - csv   → description (comma-delimited for search + templates)

        Returns SummaryResult(None, None) for IR frames, load failures,
        or inference errors.
        """
        import asyncio
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._describe_sync, jpegs, duration_s,
        )

    def _describe_sync(
        self,
        jpegs: list[bytes],
        duration_s: float,
    ) -> SummaryResult:
        if not self.available or not jpegs:
            return SummaryResult(summary=None, description=None)

        try:
            from PIL import Image
            import io
            import numpy as np

            images: list = []
            for jpeg in jpegs:
                img = Image.open(io.BytesIO(jpeg)).convert("RGB")

                # IR gate on the first frame.
                if not images:
                    arr = np.array(img, dtype=np.float32)
                    if arr.ndim == 3 and arr.shape[2] == 3:
                        channel_means = arr.mean(axis=(0, 1))
                        divergence = np.abs(
                            channel_means - channel_means.mean()
                        ).mean()
                        if divergence < _IR_CHANNEL_DIVERGENCE:
                            logger.debug("IR gate: skipping greyscale frame")
                            return SummaryResult(summary=None, description=None)

                images.append(img)

            if len(images) >= 2:
                # Multi-frame: stitch side by side for Moondream to see
                # the temporal progression in a single image.
                widths = [im.width for im in images]
                max_h = max(im.height for im in images)
                composite = Image.new("RGB", (sum(widths), max_h))
                x = 0
                for im in images:
                    composite.paste(im, (x, 0))
                    x += im.width
                target = composite
            else:
                target = images[0]

            # Encode once, answer twice.
            encoded = self._model.encode_image(target)

            brief = self._model.answer_question(
                encoded, _PROMPT_BRIEF, self._tokenizer,
            ).strip()

            csv = self._model.answer_question(
                encoded, _PROMPT_CSV, self._tokenizer,
            ).strip()

            return SummaryResult(
                summary=brief or None,
                description=csv or None,
            )

        except Exception as e:
            logger.warning("Moondream inference failed: %s", e)
            return SummaryResult(summary=None, description=None)
