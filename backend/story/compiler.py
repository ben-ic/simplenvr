"""Story compiler — template-based event summarizer.

Takes a list of classified motion events (with optional VLM structured
data) and produces grouped, human-readable story lines per camera.

Pipeline:
  1. Group events by camera + time window (5-min gap)
  2. Within each group, collapse by object_class (+ action when available)
  3. Format each group into a sentence using templates
  4. Produce a StoryDigest with per-camera summaries + overall summary

Example output:
  Front Door: Person arrived at 10:32 AM. 3 vehicles drove past.
  Backyard: Quiet — nothing detected.
  Elm St: 23 vehicles passed. 2 people walked by.

No cloud LLM required. Template-based, deterministic, instant.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)

# Maximum gap (seconds) between events before starting a new group.
_GROUP_GAP_S = 300  # 5 minutes — matches the episode endpoint

# When a camera has more than this many events of one type, it's a
# "street camera" pattern and we collapse aggressively.
_STREET_CAMERA_THRESHOLD = 5


@dataclass
class EventInput:
    """One motion event, as the compiler sees it."""
    id: str
    camera_id: str
    started_at: str          # ISO timestamp
    ended_at: str | None
    object_class: str | None  # person / vehicle / animal
    description: str | None   # Moondream free-form (current)
    # Future: structured VLM output (JSON string or pre-parsed dict).
    structured: dict | None = None


@dataclass
class StoryLine:
    """One sentence in the story."""
    camera_id: str
    camera_name: str
    text: str
    event_count: int
    started_at: str           # earliest event in this line
    event_ids: list[str] = field(default_factory=list)


@dataclass
class CameraSummary:
    """All story lines for one camera."""
    camera_id: str
    camera_name: str
    lines: list[StoryLine] = field(default_factory=list)

    @property
    def total_events(self) -> int:
        return sum(line.event_count for line in self.lines)

    @property
    def is_quiet(self) -> bool:
        return len(self.lines) == 0


@dataclass
class StoryDigest:
    """The complete story for a time period."""
    cameras: list[CameraSummary]
    period_label: str  # "Today", "Yesterday", "April 10"

    @property
    def total_events(self) -> int:
        return sum(c.total_events for c in self.cameras)

    @property
    def is_quiet(self) -> bool:
        return all(c.is_quiet for c in self.cameras)

    @property
    def overall_summary(self) -> str:
        """One-line digest for the whole period."""
        if self.is_quiet:
            return f"Quiet {self.period_label.lower()} — nothing detected."

        parts: list[str] = []
        for cam in self.cameras:
            if cam.is_quiet:
                continue
            if len(cam.lines) == 1:
                parts.append(f"{cam.camera_name}: {cam.lines[0].text}")
            else:
                joined = " ".join(line.text for line in cam.lines)
                parts.append(f"{cam.camera_name}: {joined}")
        return " ".join(parts)


# ── Grouping ───────────────────────────────────────────────────────────

@dataclass
class _EventGroup:
    """Consecutive events on one camera within the gap window."""
    camera_id: str
    events: list[EventInput] = field(default_factory=list)

    @property
    def started_at(self) -> str:
        return self.events[0].started_at

    @property
    def ended_at(self) -> str:
        return self.events[-1].started_at


def _group_events(events: list[EventInput]) -> list[_EventGroup]:
    """Group events by camera + time proximity."""
    # Sort by camera then time.
    sorted_events = sorted(events, key=lambda e: (e.camera_id, e.started_at))

    groups: list[_EventGroup] = []
    current: _EventGroup | None = None

    for ev in sorted_events:
        if current is None or ev.camera_id != current.camera_id:
            if current:
                groups.append(current)
            current = _EventGroup(camera_id=ev.camera_id, events=[ev])
            continue

        # Same camera — check time gap.
        try:
            prev_time = datetime.fromisoformat(current.events[-1].started_at)
            this_time = datetime.fromisoformat(ev.started_at)
            gap = abs((this_time - prev_time).total_seconds())
        except (ValueError, TypeError):
            gap = _GROUP_GAP_S + 1  # force new group on parse failure

        if gap <= _GROUP_GAP_S:
            current.events.append(ev)
        else:
            groups.append(current)
            current = _EventGroup(camera_id=ev.camera_id, events=[ev])

    if current:
        groups.append(current)

    return groups


# ── Collapse + format ──────────────────────────────────────────────────

_LABEL_SINGULAR: dict[str, str] = {
    "person": "person",
    "vehicle": "vehicle",
    "animal": "animal",
}

_LABEL_PLURAL: dict[str, str] = {
    "person": "people",
    "vehicle": "vehicles",
    "animal": "animals",
}

_DEFAULT_ACTIONS: dict[str, str] = {
    "person": "detected",
    "vehicle": "passed",
    "animal": "detected",
}

_DEFAULT_ACTIONS_PLURAL: dict[str, str] = {
    "person": "detected",
    "vehicle": "passed",
    "animal": "detected",
}


def _format_time(iso: str) -> str:
    """Format an ISO timestamp as a human-readable clock time."""
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%-I:%M %p").lower()
    except (ValueError, TypeError):
        return ""


def _collapse_group(group: _EventGroup, camera_name: str) -> list[StoryLine]:
    """Collapse a group of events into story lines.

    Single events get descriptive sentences. Multiple similar events
    get collapsed ("3 people detected", "23 vehicles passed").
    """
    if not group.events:
        return []

    # Count events by object_class.
    by_class: dict[str | None, list[EventInput]] = defaultdict(list)
    for ev in group.events:
        by_class[ev.object_class].append(ev)

    lines: list[StoryLine] = []

    for obj_class, class_events in by_class.items():
        if obj_class is None:
            # Unlabeled events — skip in story (noise-gated).
            continue

        count = len(class_events)
        event_ids = [ev.id for ev in class_events]
        earliest = min(ev.started_at for ev in class_events)

        if count == 1:
            ev = class_events[0]
            text = _format_single_event(ev, camera_name, obj_class)
        else:
            text = _format_collapsed_events(
                class_events, camera_name, obj_class, count,
            )

        lines.append(StoryLine(
            camera_id=group.camera_id,
            camera_name=camera_name,
            text=text,
            event_count=count,
            started_at=earliest,
            event_ids=event_ids,
        ))

    # Sort lines: people first, then vehicles, then animals.
    priority = {"person": 0, "vehicle": 1, "animal": 2}
    lines.sort(key=lambda l: priority.get(
        next((e.object_class for e in group.events if e.id in l.event_ids), ""), 9
    ))

    return lines


def _format_single_event(
    ev: EventInput, camera_name: str, obj_class: str,
) -> str:
    """Format a single event into a sentence."""
    # If we have structured VLM data, use it.
    if ev.structured:
        return _format_from_structured(ev.structured, obj_class)

    # If we have a free-form VLM description, use it directly.
    if ev.description:
        return ev.description

    # Fall back to YOLOX label + time.
    label = _LABEL_SINGULAR.get(obj_class, obj_class)
    time_str = _format_time(ev.started_at)
    action = _DEFAULT_ACTIONS.get(obj_class, "detected")
    if time_str:
        return f"{label.capitalize()} {action} at {time_str}."
    return f"{label.capitalize()} {action}."


def _format_collapsed_events(
    events: list[EventInput],
    camera_name: str,
    obj_class: str,
    count: int,
) -> str:
    """Format multiple similar events into a collapsed sentence."""
    plural = _LABEL_PLURAL.get(obj_class, f"{obj_class}s")
    action = _DEFAULT_ACTIONS_PLURAL.get(obj_class, "detected")

    # Check if we have structured data with actions to sub-group.
    action_counts: dict[str, int] = defaultdict(int)
    for ev in events:
        if ev.structured and "action" in ev.structured:
            action_counts[ev.structured["action"]] += 1

    if action_counts and len(action_counts) > 1:
        # Multiple distinct actions — break them out.
        parts = []
        for act, n in sorted(action_counts.items(), key=lambda x: -x[1]):
            parts.append(f"{n} {act}")
        return f"{count} {plural} ({', '.join(parts)})."

    # Simple collapse.
    return f"{count} {plural} {action}."


def _format_from_structured(data: dict, obj_class: str) -> str:
    """Build a sentence from structured VLM output."""
    objects = data.get("objects", [])
    action = data.get("action", "")
    direction = data.get("direction", "")

    if not objects:
        label = _LABEL_SINGULAR.get(obj_class, obj_class)
        if action:
            return f"{label.capitalize()} {action}."
        return f"{label.capitalize()} detected."

    # Pick the most relevant object (first one).
    subject = objects[0]
    parts = [subject.capitalize()]
    if action:
        parts.append(action)
    if direction and direction.lower() not in ("stationary", "none", ""):
        parts.append(direction)
    return " ".join(parts) + "."


# ── Public API ─────────────────────────────────────────────────────────

def compile_story(
    events: list[EventInput],
    camera_names: dict[str, str],
    period_label: str = "Today",
) -> StoryDigest:
    """Compile a list of events into a StoryDigest.

    Args:
        events: Flat list of motion events (from DB).
        camera_names: Mapping of camera_id → friendly name.
        period_label: "Today", "Yesterday", "April 10", etc.

    Returns:
        StoryDigest with per-camera summaries.
    """
    groups = _group_events(events)

    # Build per-camera summaries.
    camera_lines: dict[str, list[StoryLine]] = defaultdict(list)
    for group in groups:
        cam_name = camera_names.get(group.camera_id, "Camera")
        lines = _collapse_group(group, cam_name)
        camera_lines[group.camera_id].extend(lines)

    # Include cameras with zero events as "quiet".
    all_camera_ids = set(camera_names.keys()) | set(camera_lines.keys())

    cameras: list[CameraSummary] = []
    for cam_id in sorted(all_camera_ids):
        cam_name = camera_names.get(cam_id, "Camera")
        cameras.append(CameraSummary(
            camera_id=cam_id,
            camera_name=cam_name,
            lines=camera_lines.get(cam_id, []),
        ))

    # Sort: cameras with events first, then quiet ones.
    cameras.sort(key=lambda c: (c.is_quiet, c.camera_name))

    return StoryDigest(cameras=cameras, period_label=period_label)


def parse_structured_field(raw: str | None) -> dict | None:
    """Parse a JSON structured field from the DB.

    Tolerates None, empty string, and malformed JSON gracefully.
    """
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None
