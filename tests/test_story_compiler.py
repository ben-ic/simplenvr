"""Tests for the Story template compiler."""
from __future__ import annotations

import pytest

from backend.story.compiler import (
    EventInput,
    StoryDigest,
    compile_story,
    parse_structured_field,
)


def _ev(
    id: str = "e1",
    camera_id: str = "cam1",
    started_at: str = "2026-04-10T14:00:00",
    ended_at: str | None = "2026-04-10T14:00:30",
    object_class: str | None = "person",
    description: str | None = None,
    structured: dict | None = None,
) -> EventInput:
    return EventInput(
        id=id,
        camera_id=camera_id,
        started_at=started_at,
        ended_at=ended_at,
        object_class=object_class,
        description=description,
        structured=structured,
    )


CAMERA_NAMES = {"cam1": "Front Door", "cam2": "Elm St", "cam3": "Backyard"}


# ── Single event tests ─────────────────────────────────────────────────

class TestSingleEvent:
    def test_yolox_label_only(self):
        digest = compile_story(
            [_ev(object_class="person")],
            CAMERA_NAMES,
        )
        active = [c for c in digest.cameras if not c.is_quiet]
        assert len(active) == 1
        assert active[0].lines[0].text == "Person detected at 2:00 pm."

    def test_vlm_description_used(self):
        digest = compile_story(
            [_ev(description="Blue sedan drove past the house.")],
            CAMERA_NAMES,
        )
        assert digest.cameras[0].lines[0].text == "Blue sedan drove past the house."

    def test_structured_vlm_output(self):
        digest = compile_story(
            [_ev(structured={
                "objects": ["person in blue jacket"],
                "action": "arrived",
                "direction": "toward camera",
            })],
            CAMERA_NAMES,
        )
        line = digest.cameras[0].lines[0].text
        assert "Person in blue jacket" in line
        assert "arrived" in line
        assert "toward camera" in line

    def test_vehicle_label(self):
        digest = compile_story(
            [_ev(object_class="vehicle")],
            CAMERA_NAMES,
        )
        assert "Vehicle" in digest.cameras[0].lines[0].text


# ── Collapse tests ─────────────────────────────────────────────────────

class TestCollapse:
    def test_two_people_collapse(self):
        events = [
            _ev(id="e1", started_at="2026-04-10T14:00:00"),
            _ev(id="e2", started_at="2026-04-10T14:01:00"),
        ]
        digest = compile_story(events, CAMERA_NAMES)
        assert len(digest.cameras[0].lines) == 1
        assert "2 people" in digest.cameras[0].lines[0].text

    def test_street_camera_many_vehicles(self):
        events = [
            _ev(
                id=f"e{i}",
                camera_id="cam2",
                object_class="vehicle",
                started_at=f"2026-04-10T14:{i:02d}:00",
            )
            for i in range(23)
        ]
        digest = compile_story(events, CAMERA_NAMES)
        elm = next(c for c in digest.cameras if c.camera_id == "cam2")
        # All within 5-min groups, so multiple groups but vehicles collapsed.
        total = sum(line.event_count for line in elm.lines)
        assert total == 23
        # Check at least one line says "vehicles passed".
        assert any("vehicles passed" in line.text for line in elm.lines)

    def test_mixed_labels_separate_lines(self):
        events = [
            _ev(id="e1", object_class="person", started_at="2026-04-10T14:00:00"),
            _ev(id="e2", object_class="vehicle", started_at="2026-04-10T14:01:00"),
        ]
        digest = compile_story(events, CAMERA_NAMES)
        cam = digest.cameras[0]
        assert len(cam.lines) == 2
        labels = {line.text.split()[0] for line in cam.lines}
        assert "Person" in labels
        assert "Vehicle" in labels


# ── Time grouping tests ───────────────────────────────────────────────

class TestGrouping:
    def test_events_beyond_gap_split(self):
        events = [
            _ev(id="e1", started_at="2026-04-10T14:00:00"),
            _ev(id="e2", started_at="2026-04-10T14:10:00"),  # 10 min later
        ]
        digest = compile_story(events, CAMERA_NAMES)
        cam = digest.cameras[0]
        # Should be 2 separate lines (2 groups, 1 event each).
        assert len(cam.lines) == 2

    def test_events_within_gap_collapse(self):
        events = [
            _ev(id="e1", started_at="2026-04-10T14:00:00"),
            _ev(id="e2", started_at="2026-04-10T14:04:00"),  # 4 min later
        ]
        digest = compile_story(events, CAMERA_NAMES)
        cam = digest.cameras[0]
        assert len(cam.lines) == 1
        assert cam.lines[0].event_count == 2


# ── Quiet day tests ────────────────────────────────────────────────────

class TestQuietDay:
    def test_no_events(self):
        digest = compile_story([], CAMERA_NAMES, period_label="Today")
        assert digest.is_quiet
        assert "quiet" in digest.overall_summary.lower()
        assert "today" in digest.overall_summary.lower()

    def test_quiet_cameras_listed(self):
        events = [_ev(camera_id="cam1")]
        digest = compile_story(events, CAMERA_NAMES)
        quiet = [c for c in digest.cameras if c.is_quiet]
        # cam2 and cam3 had no events.
        assert len(quiet) == 2

    def test_quiet_cameras_sorted_last(self):
        events = [_ev(camera_id="cam1")]
        digest = compile_story(events, CAMERA_NAMES)
        # Active cameras come first.
        assert not digest.cameras[0].is_quiet
        assert digest.cameras[-1].is_quiet


# ── Overall summary tests ─────────────────────────────────────────────

class TestOverallSummary:
    def test_summary_mentions_camera_name(self):
        events = [_ev(camera_id="cam1")]
        digest = compile_story(events, CAMERA_NAMES)
        assert "Front Door" in digest.overall_summary

    def test_summary_combines_cameras(self):
        events = [
            _ev(id="e1", camera_id="cam1"),
            _ev(id="e2", camera_id="cam2", object_class="vehicle"),
        ]
        digest = compile_story(events, CAMERA_NAMES)
        assert "Front Door" in digest.overall_summary
        assert "Elm St" in digest.overall_summary


# ── Structured data parsing ────────────────────────────────────────────

class TestParseStructured:
    def test_valid_json(self):
        result = parse_structured_field('{"objects": ["car"], "action": "driving"}')
        assert result == {"objects": ["car"], "action": "driving"}

    def test_none_input(self):
        assert parse_structured_field(None) is None

    def test_empty_string(self):
        assert parse_structured_field("") is None

    def test_malformed_json(self):
        assert parse_structured_field("not json") is None

    def test_non_dict_json(self):
        assert parse_structured_field("[1, 2, 3]") is None


# ── Structured action sub-grouping ─────────────────────────────────────

class TestStructuredActions:
    def test_multiple_actions_broken_out(self):
        events = [
            _ev(id="e1", object_class="vehicle", structured={"action": "drove past"}),
            _ev(id="e2", object_class="vehicle", started_at="2026-04-10T14:01:00",
                structured={"action": "drove past"}),
            _ev(id="e3", object_class="vehicle", started_at="2026-04-10T14:02:00",
                structured={"action": "parked"}),
        ]
        digest = compile_story(events, CAMERA_NAMES)
        line = digest.cameras[0].lines[0].text
        # Should mention both actions with counts.
        assert "drove past" in line
        assert "parked" in line
