"""Deterministic final review.

Before a task is marked COMPLETED, a bounded, deterministic review is performed.
This is NOT a subjective "AI code quality score" — it is a set of mechanical
checks over the change analysis, verification result, and workspace state. The
checks are deterministic wherever possible so the outcome is reproducible.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from events import EventBus, EventType
from intelligence.models import ChangeAnalysis, ReviewCheck, ReviewResult
from tasks.policies import VerificationOutcome

if TYPE_CHECKING:
    from tasks.verifier import VerificationResult


class FinalReviewer:
    """Performs the deterministic pre-completion review."""

    def __init__(
        self,
        *,
        events: EventBus | None = None,
        session_id: str = "intelligence",
    ) -> None:
        self._events = events
        self._session_id = session_id

    def review(
        self,
        *,
        change: ChangeAnalysis,
        verification: VerificationResult | None,
        workspace_valid: bool = True,
        changes_expected: bool = True,
    ) -> ReviewResult:
        """Run all deterministic checks; return the aggregate result."""
        if self._events is not None:
            self._events.emit(EventType.FINAL_REVIEW_STARTED, self._session_id)

        checks: list[ReviewCheck] = []

        # 1. Workspace is valid.
        checks.append(ReviewCheck(
            name="workspace_valid",
            passed=workspace_valid,
            detail="workspace path exists and is accessible" if workspace_valid else "workspace invalid",
        ))

        # 2. Verification passed.
        v_passed = verification is not None and verification.outcome is VerificationOutcome.PASS
        checks.append(ReviewCheck(
            name="verification_passed",
            passed=v_passed,
            detail=verification.reason if verification else "no verification result",
        ))

        # 3. No unresolved failure remains (encoded by verification outcome).
        no_unresolved = verification is not None and verification.outcome is not VerificationOutcome.FAIL
        checks.append(ReviewCheck(
            name="no_unresolved_failure",
            passed=no_unresolved,
            detail="verification did not report a failure" if no_unresolved else "verification reported a failure",
        ))

        # 4. Diff is non-empty when changes were expected.
        diff_nonempty = not change.empty
        diff_ok = (diff_nonempty if changes_expected else change.empty)
        checks.append(ReviewCheck(
            name="diff_coherent",
            passed=diff_ok,
            detail=f"{'non-' if not diff_nonempty else ''}empty diff, changes_expected={changes_expected}",
        ))

        # 5. No changes outside the workspace.
        checks.append(ReviewCheck(
            name="no_outside_workspace_changes",
            passed=not change.outside_workspace,
            detail=f"{len(change.outside_workspace)} file(s) outside workspace" if change.outside_workspace else "all changes within workspace",
        ))

        # 6. No suspiciously broad change.
        checks.append(ReviewCheck(
            name="change_scope_bounded",
            passed=not change.broad,
            detail=f"{len(change.changed)} changed file(s)" + (" (broad)" if change.broad else ""),
        ))

        # 7. No obvious generated files committed (advisory; does not fail alone).
        # Generated files are flagged but do not by themselves fail the review,
        # since a build artifact may legitimately be tracked in some repos.
        checks.append(ReviewCheck(
            name="no_obvious_generated_files",
            passed=not change.generated,
            detail=f"{len(change.generated)} likely-generated file(s)" if change.generated else "no obvious generated files",
        ))

        passed = all(c.passed for c in checks)
        # Generated-files check is advisory: do not fail the whole review on it alone.
        if not passed:
            hard_failures = [c for c in checks if not c.passed and c.name != "no_obvious_generated_files"]
            passed = not hard_failures

        result = ReviewResult(
            passed=passed,
            checks=checks,
            reason="all hard checks passed" if passed else "one or more hard checks failed",
        )
        if self._events is not None:
            self._events.emit(
                EventType.FINAL_REVIEW_COMPLETED,
                self._session_id,
                passed=passed,
                checks=len(checks),
            )
        return result
