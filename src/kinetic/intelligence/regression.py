"""Regression detection.

After a repair succeeds, the broader verification command is re-run and the
result is compared against the pre-repair failure state. The goal is to never
declare success merely because one failing test passes — a repair that breaks
something else is a regression and must be treated as a failure.

The checker reuses the existing :class:`~kinetic.tasks.verifier.Verifier`
(which routes through ``Environment.exec`` and the permission boundary), and
the Phase 2 :func:`~kinetic.tasks.verifier.command_for_manifest` to resolve the
project's broader verification command. No new execution path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kinetic.events import EventBus, EventType
from kinetic.intelligence.models import RegressionResult
from kinetic.tasks.policies import VerificationOutcome

if TYPE_CHECKING:
    from kinetic.tasks.verifier import Verifier


class RegressionChecker:
    """Runs a broader verification after a repair and detects regressions."""

    def __init__(
        self,
        *,
        verifier: Verifier,
        events: EventBus | None = None,
        session_id: str = "intelligence",
    ) -> None:
        self._verifier = verifier
        self._events = events
        self._session_id = session_id

    async def check(
        self,
        *,
        before_passed: bool,
        command: str | None = None,
    ) -> RegressionResult:
        """Run the broader verification and compare to the pre-repair state.

        ``before_passed`` is the verification outcome *before* the repair. A
        regression is when the broader verification fails after a repair that
        appeared to fix the original failure.
        """
        result = await self._verifier.verify(command=command)
        after_passed = result.outcome is VerificationOutcome.PASS
        # After a successful repair, the broader verification is expected to
        # pass. A FAIL (not INCONCLUSIVE) after a repair that fixed the original
        # failure is a regression — the repair broke something else, or did not
        # fully fix the project. INCONCLUSIVE is not treated as a regression
        # (no command / cannot run) to avoid false failures.
        after_failed = result.outcome is VerificationOutcome.FAIL
        regressed = after_failed
        # If the broader command was passing before, a FAIL is definitely a
        # regression; if it was already failing, a continued FAIL still means
        # the task is not genuinely complete, so it is still reported as a
        # regression (repair did not achieve a clean broader verification).
        _ = before_passed
        rr = RegressionResult(
            regressed=regressed,
            before_passed=before_passed,
            after_passed=after_passed,
            command=result.command or command or "",
            reason=result.reason,
        )
        if regressed and self._events is not None:
            self._events.emit(
                EventType.REGRESSION_DETECTED,
                self._session_id,
                command=rr.command,
                reason=rr.reason,
            )
        return rr
