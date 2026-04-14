"""Per-track Beta-Bernoulli confidence with a recoverable-doubt state machine.

Layer 4 of the Detection Pipeline v2 false-positive stack: each track maintains a
Beta(alpha, beta) posterior over "is this track a real detection?" Every frame we
fold in one observation sourced from the tracker's match result:

    MATCHED_HIGH  -> s = det_score         (pass-1 high-score match)
    MATCHED_LOW   -> s = 0.5 * det_score   (pass-2 low-score match, half weight)
    UNMATCHED     -> s = 0                 (track coasted; evidence against)

    alpha += s
    beta  += (1 - s)

Then exponential forgetting keeps the window finite so ancient evidence can't
pin the posterior:

    alpha = 0.98 * alpha + 0.02
    beta  = 0.98 * beta  + 0.02

We expose the posterior mean ``p_hat`` and its one-sigma lower bound
``p_hat_lo`` (using the analytic Beta stddev). Callers surface a track to event
creation when ``p_hat_lo > 0.5``.

Layer 5 is a three-state recoverable-doubt machine on ``p_hat`` (not the lower
bound, so the hysteresis is based on the central estimate):

    pending   --(p_hat >= 0.70)--> confirmed
    confirmed --(p_hat <  0.35)--> doubt       (caller pauses event emission)
    doubt     --(p_hat >= 0.50)--> confirmed   (caller resumes)

Once a track reaches ``confirmed`` it never returns to ``pending``.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from typing import Literal

MatchType = Literal["high", "low", "none"]
State = Literal["pending", "confirmed", "doubt"]

FORGET_ALPHA = 0.98
FORGET_BASELINE = 0.02
CONFIRM_THRESHOLD = 0.70
DOUBT_THRESHOLD = 0.35
RECONFIRM_THRESHOLD = 0.50
LOW_MATCH_WEIGHT = 0.5
EMIT_PROB_FLOOR = 0.5


@dataclass(slots=True)
class TrackConfidence:
    """Beta-Bernoulli confidence + recoverable-doubt state for one track."""

    alpha: float = 1.0
    beta: float = 1.0
    state: State = "pending"

    def update(self, match_type: MatchType, det_score: float = 0.0) -> None:
        """Fold one observation into the posterior, then tick the state machine."""
        if match_type == "high":
            s = det_score
        elif match_type == "low":
            s = LOW_MATCH_WEIGHT * det_score
        elif match_type == "none":
            s = 0.0
        else:  # pragma: no cover - defensive; Literal narrows at type-check time
            raise ValueError(f"unknown match_type: {match_type!r}")

        # Clamp s into [0, 1] so a misbehaving upstream score can't push alpha/beta
        # negative or explode the posterior.
        if s < 0.0:
            s = 0.0
        elif s > 1.0:
            s = 1.0

        self.alpha += s
        self.beta += 1.0 - s

        self.alpha = FORGET_ALPHA * self.alpha + FORGET_BASELINE
        self.beta = FORGET_ALPHA * self.beta + FORGET_BASELINE

        self._tick_state()

    def _tick_state(self) -> None:
        p = self.p_hat
        if self.state == "pending":
            if p >= CONFIRM_THRESHOLD:
                self.state = "confirmed"
        elif self.state == "confirmed":
            if p < DOUBT_THRESHOLD:
                self.state = "doubt"
        else:  # doubt
            if p >= RECONFIRM_THRESHOLD:
                self.state = "confirmed"

    @property
    def p_hat(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def p_hat_lo(self) -> float:
        a, b = self.alpha, self.beta
        n = a + b
        sigma = sqrt((a * b) / (n * n * (n + 1.0)))
        lo = a / n - sigma
        return lo if lo > 0.0 else 0.0

    @property
    def emittable(self) -> bool:
        """Gate for event creation: confirmed AND lower-bound above the floor."""
        return self.state == "confirmed" and self.p_hat_lo > EMIT_PROB_FLOOR
