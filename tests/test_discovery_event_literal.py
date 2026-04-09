"""
Invariant: every string literal passed to `event_bus.emit("<type>", ...)`
anywhere in the backend must be a member of `DiscoveryEvent.type`.

Motivation: session 4 (2026-04-08) shipped `recordings_deleted` emit
calls without updating the Literal tuple, silently crashing every
finalize path with a Pydantic validation error for over a day. This
test would have caught it at CI time. It is intentionally kept small
and dependency-free so it runs as part of the normal pytest collect.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import get_args

import pytest

from backend.models import DiscoveryEvent


BACKEND_ROOT = Path(__file__).resolve().parent.parent / "backend"


def _valid_event_types() -> set[str]:
    return set(get_args(DiscoveryEvent.model_fields["type"].annotation))


def _collect_emit_literals() -> list[tuple[Path, int, str]]:
    """Walk every .py under backend/ and collect the first-argument
    literal of every `<something>.emit(...)` call. Ignores non-literal
    first args (we can't check dynamic values at static-analysis time)."""
    hits: list[tuple[Path, int, str]] = []
    for py in BACKEND_ROOT.rglob("*.py"):
        try:
            tree = ast.parse(py.read_text(), filename=str(py))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "emit":
                continue
            if not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                hits.append((py.relative_to(BACKEND_ROOT), node.lineno, first.value))
    return hits


def test_every_emit_literal_is_in_discovery_event_type():
    valid = _valid_event_types()
    hits = _collect_emit_literals()
    # Sanity: we should find at least a handful. If zero, the AST walk
    # is broken and the test is silently passing.
    assert len(hits) > 5, f"suspiciously few emit literals found: {len(hits)}"
    mismatches = [(p, ln, s) for (p, ln, s) in hits if s not in valid]
    if mismatches:
        lines = "\n".join(f"  {p}:{ln}  emit({s!r})" for p, ln, s in mismatches)
        pytest.fail(
            "event_bus.emit() literal not in DiscoveryEvent.type:\n"
            f"{lines}\n\nValid types: {sorted(valid)}"
        )
