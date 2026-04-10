"""AudioSet class names → SimpleNVR sound labels.

YAMNet outputs 521 AudioSet class scores. We collapse them into a small
set of product-grade labels with a trust hierarchy:

  HIGH PRIORITY — fire independent events even with no concurrent motion.
  LOW PRIORITY  — only enrich existing vision events, never fire alone.

The class name strings must match the YAMNet class map CSV exactly
(case-sensitive). When in doubt, add the variant — an unused entry
costs nothing.
"""

from __future__ import annotations

from typing import Literal

SoundLabel = Literal[
    "glass_break",
    "gunshot",
    "scream",
    "siren",
    "bark",
    "car_horn",
    "door_slam",
    "doorbell",
    "meow",
    "footsteps",
]

HIGH_PRIORITY: frozenset[SoundLabel] = frozenset(
    {"glass_break", "gunshot", "scream", "siren"}
)

LOW_PRIORITY: frozenset[SoundLabel] = frozenset(
    {"bark", "car_horn", "door_slam", "doorbell", "meow", "footsteps"}
)

ALL_LABELS: frozenset[SoundLabel] = HIGH_PRIORITY | LOW_PRIORITY

# AudioSet display_name → our label. Multiple AudioSet classes can map
# to the same SimpleNVR label (e.g. all siren subtypes → "siren").
_CLASS_MAP: dict[str, SoundLabel] = {
    # ── high priority ──
    "Shatter": "glass_break",
    "Glass": "glass_break",
    "Breaking": "glass_break",
    "Gunshot, gunfire": "gunshot",
    "Machine gun": "gunshot",
    "Firecracker": "gunshot",
    "Scream": "scream",
    "Screaming": "scream",
    "Siren": "siren",
    "Civil defense siren": "siren",
    "Ambulance (siren)": "siren",
    "Fire engine, fire truck (siren)": "siren",
    "Police car (siren)": "siren",
    # ── low priority ──
    "Bark": "bark",
    "Yip": "bark",
    "Bow-wow": "bark",
    "Growling": "bark",
    "Vehicle horn, car horn, honking": "car_horn",
    "Air horn, truck horn": "car_horn",
    "Reversing beeps": "car_horn",
    "Door": "door_slam",
    "Slam": "door_slam",
    "Doorbell": "doorbell",
    "Ding-dong": "doorbell",
    "Meow": "meow",
    "Purr": "meow",
    "Hiss": "meow",
    "Walk, footsteps": "footsteps",
    "Run": "footsteps",
}


def map_class(display_name: str) -> SoundLabel | None:
    """Map an AudioSet display name to our simplified label, or None."""
    return _CLASS_MAP.get(display_name)


def is_high_priority(label: SoundLabel) -> bool:
    return label in HIGH_PRIORITY
