"""Tests for the per-track Beta-Bernoulli confidence + state machine."""

from __future__ import annotations

from backend.motion.confidence import TrackConfidence


def test_fresh_track_is_pending_not_emittable() -> None:
    tc = TrackConfidence()
    assert tc.state == "pending"
    assert tc.emittable is False
    assert 0.0 < tc.p_hat < 1.0
    # Beta(1,1) mean is exactly 0.5.
    assert tc.p_hat == 0.5


def test_monotonic_high_matches_reach_confirmed() -> None:
    tc = TrackConfidence()
    for _ in range(20):
        tc.update("high", 0.9)
    assert tc.state == "confirmed"
    assert tc.emittable is True
    assert tc.p_hat > 0.70


def test_drop_to_doubt_suspends_emit_then_recovers() -> None:
    tc = TrackConfidence()
    # Drive to confirmed with strong evidence.
    for _ in range(30):
        tc.update("high", 0.95)
    assert tc.state == "confirmed"
    assert tc.emittable is True

    # Coast: enough "none" frames for p_hat to decay past the 0.35 doubt
    # threshold. With FORGET_ALPHA=0.98 the decay has a ~50-frame time
    # constant so we need ~30 frames; 40 leaves comfortable margin.
    for _ in range(40):
        tc.update("none")
    assert tc.state == "doubt"
    assert tc.emittable is False

    # Recover: strong matches must pull p_hat back above the 0.50
    # reconfirm threshold AND pull p_hat_lo above the 0.5 emit floor.
    # After 40 coast frames alpha/beta are both depressed so sigma is
    # large (the lower-bound is pessimistic). We need enough recovery
    # evidence to overcome both thresholds.
    for _ in range(40):
        tc.update("high", 0.95)
    assert tc.state == "confirmed"
    assert tc.emittable is True


def test_once_confirmed_never_goes_pending() -> None:
    tc = TrackConfidence()
    for _ in range(30):
        tc.update("high", 0.95)
    assert tc.state == "confirmed"

    # Oscillate doubt/recovery several times.
    for _ in range(3):
        for _ in range(20):
            tc.update("none")
            assert tc.state != "pending"
        for _ in range(15):
            tc.update("high", 0.95)
            assert tc.state != "pending"

    assert tc.state in ("confirmed", "doubt")


def test_p_hat_lo_below_p_hat_and_nonnegative() -> None:
    tc = TrackConfidence()
    observations = [
        ("high", 0.9),
        ("low", 0.6),
        ("none", 0.0),
        ("high", 0.8),
        ("high", 0.95),
        ("none", 0.0),
        ("low", 0.55),
    ]
    for match_type, score in observations:
        tc.update(match_type, score)  # type: ignore[arg-type]
        assert 0.0 <= tc.p_hat_lo <= tc.p_hat <= 1.0
