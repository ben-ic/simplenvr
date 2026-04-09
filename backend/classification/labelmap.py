"""COCO-80 class id → SimpleNVR label collapse.

The classifier runs YOLOX-S trained on COCO, which produces 80 fine-grained
class ids. The user only cares about three buckets: is it a person, a
vehicle, or an animal. Everything else is silent fallback.

Collapse happens AT the scoring layer, not post-hoc. That means frame-level
false positives like "traffic light 0.39" or "train 0.22 on pergola slats"
silently disappear because they're not in the output label set — no
post-hoc filtering needed, no user-visible wrong labels.

This design is load-bearing for the product promise: the user never sees
a COCO class name. They see "Vehicle at Carport", not "Truck at Carport"
even when the raw detection was "truck 0.34". The label collapse is the
seam between the COCO training distribution and the three-label product.
"""
from __future__ import annotations

from typing import Final, Literal

# The three product labels. Anything outside this set becomes None and the
# Inbox row stays at "Motion at X" via the silent-confidence-fallback path.
ObjectLabel = Literal["person", "vehicle", "animal"]

# COCO-80 id → product label. Missing ids collapse to None (silent drop).
#
# COCO class ids are documented in many places; the canonical source is
# the COCO 2017 instances dataset and the YOLOX repo's coco_classes.py.
# Ids in this file are the 80-class "contiguous" id scheme that YOLOX
# outputs via its ONNX head (0..79), NOT the sparse 91-class scheme used
# in some other COCO derivatives. YOLOX output ids are what we index on.
_COCO_TO_LABEL: Final[dict[int, ObjectLabel]] = {
    # --- person ---
    0: "person",
    # --- vehicle ---
    # Note: "bicycle" (1) and "motorcycle" (3) are deliberately excluded.
    # A parked bicycle on a lawn would fire "Vehicle at Back yard" which
    # is technically correct but UX-wrong for most users. If a user cares
    # about bike theft specifically, that's a v2 conversation.
    2: "vehicle",   # car
    5: "vehicle",   # bus
    7: "vehicle",   # truck
    # Note: "boat" (8), "airplane" (4), "train" (6) are excluded. They fire
    # as false positives on IR night frames of parked cars (empirically
    # confirmed 2026-04-09 — pergola slats read as "train 0.22"), and
    # they're never the real intent of a home/SMB NVR anyway.
    # --- animal ---
    14: "animal",   # bird
    15: "animal",   # cat
    16: "animal",   # dog
    17: "animal",   # horse
    18: "animal",   # sheep
    19: "animal",   # cow
    20: "animal",   # elephant (exotic but cheap to include)
    21: "animal",   # bear (matters in rural installs)
    22: "animal",   # zebra (same)
    23: "animal",   # giraffe (same)
}


def collapse(coco_id: int) -> ObjectLabel | None:
    """Map a COCO class id to the product label, or None if it's not in
    the set we care about.

    Returning None is a first-class result — it means the detection is
    silently dropped and the Inbox row stays at "Motion at X". This is
    the silent-confidence-fallback path at the label layer (as opposed
    to the confidence-threshold fallback at the score layer).
    """
    return _COCO_TO_LABEL.get(coco_id)


def all_labels() -> tuple[ObjectLabel, ...]:
    """Every label this module can emit. Stable, deterministic ordering."""
    return ("person", "vehicle", "animal")
