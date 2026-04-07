"""
In-memory backoff tracker for camera authentication attempts.

Without throttling, the discovery scanner will re-interrogate cameras on
every path that treats them as "new" (fresh endpoints, IP changes, cold
backend restarts). Cameras with rate-limited auth endpoints — Tapo and
Eufy are the known offenders on this hardware — interpret rapid repeated
failed-auth attempts as a brute-force probe and extend the lockout
window, so the scan loop can actually make the problem worse over time.

This tracker throttles automatic retries per camera id. Manual reauth
(the /cameras/<id>/auth API, invoked by the user clicking the auth
button) is handled separately in the scanner: it bypasses the wait,
resets the schedule on success, and deliberately does NOT extend the
window on failure so button clicks can never make things worse.

State is in-memory only. Backend restarts clear it by design — the
user's usual recovery path for a stuck camera is "restart the app",
and reloading a stale backoff on startup would defeat that.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..config import AUTH_RETRY_BACKOFF
from ..models import utcnow

logger = logging.getLogger(__name__)


@dataclass
class _BackoffState:
    failure_count: int = 0
    next_attempt_at: datetime = field(default_factory=utcnow)


class AuthBackoffTracker:
    """Throttles automatic auth-retry attempts per camera id."""

    def __init__(self) -> None:
        self._state: dict[str, _BackoffState] = {}

    def should_attempt(self, camera_id: str) -> bool:
        """True if an automatic auth attempt is allowed right now."""
        state = self._state.get(camera_id)
        if state is None:
            return True
        return utcnow() >= state.next_attempt_at

    def seconds_until_next_attempt(self, camera_id: str) -> float:
        """How long until the next automatic attempt is allowed (0 if now)."""
        state = self._state.get(camera_id)
        if state is None:
            return 0.0
        return max(0.0, (state.next_attempt_at - utcnow()).total_seconds())

    def failure_count(self, camera_id: str) -> int:
        """Consecutive failure count for this camera (0 if no record)."""
        state = self._state.get(camera_id)
        return state.failure_count if state else 0

    def record_failure(self, camera_id: str) -> float:
        """
        Record a failed auth attempt and schedule the next allowed retry.
        Returns the delay (seconds) before the next retry is allowed.
        """
        state = self._state.get(camera_id) or _BackoffState()
        # Clamp the index to the last schedule entry so it stays at the cap.
        delay = AUTH_RETRY_BACKOFF[
            min(state.failure_count, len(AUTH_RETRY_BACKOFF) - 1)
        ]
        state.failure_count += 1
        state.next_attempt_at = utcnow() + timedelta(seconds=delay)
        self._state[camera_id] = state
        return float(delay)

    def record_success(self, camera_id: str) -> None:
        """Clear backoff state — auth succeeded."""
        self._state.pop(camera_id, None)

    def reset(self, camera_id: str) -> None:
        """Clear backoff state — user-initiated manual reauth or cred change."""
        self._state.pop(camera_id, None)
