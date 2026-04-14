"""ONNX Runtime execution-provider autodetection.

Picks the fastest locally-available EP for an ORT session. Shared by
D-FINE (vision) and YAMNet (audio) because the preference order is
identical for both — the hardware wins in the same order regardless
of model.

Preference order (fastest → fallback):

    1. QNNExecutionProvider   — Snapdragon NPU on Windows ARM64.
       Our shipping target (Snapdragon X Elite Copilot+) has a
       dedicated NPU that runs INT8 ONNX graphs at ~10x CPU throughput
       and frees CPU cores for the 32-camera recording pipeline.
    2. CoreMLExecutionProvider — Apple Silicon. Dev-host fast path;
       keeps `cargo tauri dev` iterations snappy on Ben's machine.
    3. DmlExecutionProvider   — DirectML (Windows x86_64). GPU-accelerated
       fallback for desktop Windows installs without an NPU.
    4. CPUExecutionProvider   — universal fallback. Every ORT build
       includes it, so this call can never return an empty list.

The order is NPU > GPU > CPU on purpose: NPUs are power-efficient AND
fast, GPUs are fast but contend with display work, CPU is always last.

Returns a one-element provider list (ORT accepts multiple, but we
pin to the single winner so logs unambiguously reflect which EP is
actually running).
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Highest-priority provider first. See module docstring for rationale.
_PREFERENCE: tuple[str, ...] = (
    "QNNExecutionProvider",
    "CoreMLExecutionProvider",
    "DmlExecutionProvider",
    "CPUExecutionProvider",
)


def select_providers(*, subsystem: str) -> list[str]:
    """Return a one-element ORT provider list, logging the pick.

    ``subsystem`` is a short label (e.g. "dfine", "yamnet") included
    in the log line so Ben can tell at a glance which model picked
    which EP — useful when one subsystem falls back to CPU but
    another succeeds on NPU.
    """
    try:
        import onnxruntime as ort
    except ImportError:
        # Caller will fail shortly anyway; return CPU so the error
        # surfaces at session-creation time with a clearer message.
        logger.warning(
            "onnxruntime import failed while selecting EP for %s — "
            "defaulting to CPUExecutionProvider",
            subsystem,
        )
        return ["CPUExecutionProvider"]

    available = set(ort.get_available_providers())
    for provider in _PREFERENCE:
        if provider in available:
            logger.info(
                "ORT EP selected for %s: %s (available=%s)",
                subsystem, provider, sorted(available),
            )
            return [provider]

    # Shouldn't happen — CPUExecutionProvider is in every ORT build —
    # but keep the fallback explicit so static analyzers stay happy.
    logger.warning(
        "No preferred ORT EP available for %s (available=%s) — "
        "falling back to CPUExecutionProvider",
        subsystem, sorted(available),
    )
    return ["CPUExecutionProvider"]
