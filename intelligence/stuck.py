"""Bounded stuck detection.

Detects when a repair loop is not making progress and terminates it with a
bounded failure outcome instead of looping indefinitely. Stuck detection is
pure: it inspects the history of repair attempts (failure signatures) and the
working-tree change set across attempts. No execution.
"""

from __future__ import annotations

from events import EventBus, EventType
from intelligence.models import RepairAttempt, RepairState, StuckSignal


class StuckDetector:
    """Detects non-progress in the repair loop.

    Stuck conditions:
      * the same failure signature repeats across consecutive attempts;
      * the repair budget is exhausted (caller decides the threshold);
      * verification repeatedly returns the same failing result.

    A stuck task must terminate as a bounded failure — never loop forever.
    """

    def __init__(
        self,
        *,
        repeat_threshold: int = 2,
        events: EventBus | None = None,
        session_id: str = "intelligence",
    ) -> None:
        # The same failure repeating `repeat_threshold` times in a row is stuck.
        self._repeat_threshold = max(1, repeat_threshold)
        self._events = events
        self._session_id = session_id

    def evaluate(self, state: RepairState) -> StuckSignal:
        """Inspect the repair history and return a :class:`StuckSignal`."""
        attempts = state.attempts
        if len(attempts) < self._repeat_threshold:
            return StuckSignal(stuck=False, reason="not enough attempts to evaluate")

        # Same failure signature repeated across the last N attempts.
        sigs = [a.analysis and _sig(a) for a in attempts[-self._repeat_threshold:]]
        if None not in sigs and len(set(sigs)) == 1:
            sig = str(sigs[0])
            self._emit_stuck(sig, "identical failure repeated across attempts")
            return StuckSignal(stuck=True, reason="identical failure repeated across attempts", signature=sig)

        # No meaningful change in failure signatures at all (all None / identical).
        if len(attempts) >= self._repeat_threshold and all(
            (a.analysis is None) for a in attempts[-self._repeat_threshold:]
        ):
            self._emit_stuck("(no-analysis)", "no failure analysis produced across attempts")
            return StuckSignal(stuck=True, reason="no failure analysis produced across attempts", signature="(no-analysis)")

        return StuckSignal(stuck=False, reason="progress observed or inconclusive")

    def is_budget_exhausted(self, *, attempts: int, max_attempts: int) -> bool:
        """True when the repair attempt budget is exhausted."""
        return attempts >= max_attempts

    def _emit_stuck(self, signature: str, reason: str) -> None:
        state = StuckSignal(stuck=True, reason=reason, signature=signature)
        if self._events is not None:
            self._events.emit(
                EventType.STUCK_DETECTED,
                self._session_id,
                reason=reason,
                signature=signature,
            )
        # Keep the linter happy about an unused local while recording state.
        _ = state


def _sig(attempt: RepairAttempt) -> str:
    """Return the failure signature for an attempt (empty if none)."""
    if attempt.analysis is None:
        return ""
    from intelligence.analyzer import failure_signature

    return failure_signature(attempt.analysis)
