"""Retry, recovery and re-planning policies.

Bounded, deterministic policies. There is no autonomous unlimited retry:
permission/security failures fail immediately unless the permission state
changed; environment failures get a small bounded retry; test/syntax failures
may retry or re-plan within a budget; malformed plans re-plan once or fail.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from errors import PlanError


class FailureClass(StrEnum):
    """How a failure is classified — drives the retry decision.

    Phase 6 extends the set with coding-intelligence-specific classes
    (lint/type-check, dependency, command, cancellation, verification
    inconclusive). Existing members are unchanged so Phase 1–5 callers keep
    working.
    """

    TOOL_FAILURE = "tool_failure"
    TEST_FAILURE = "test_failure"
    BUILD_FAILURE = "build_failure"
    LINT_FAILURE = "lint_failure"
    DEPENDENCY_FAILURE = "dependency_failure"
    COMMAND_FAILURE = "command_failure"
    TIMEOUT = "timeout"
    CANCELLATION = "cancellation"
    PERMISSION_DENIED = "permission_denied"
    ENVIRONMENT_FAILURE = "environment_failure"
    INVALID_PLAN = "invalid_plan"
    VERIFICATION_INCONCLUSIVE = "verification_inconclusive"
    UNKNOWN = "unknown"


class VerificationOutcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


# Classes that should NOT be retried (deterministic security/permission failures,
# and cancellations which are terminal-by-intent).
NON_RETRYABLE: frozenset[FailureClass] = frozenset(
    {FailureClass.PERMISSION_DENIED, FailureClass.INVALID_PLAN, FailureClass.CANCELLATION}
)
# Classes that may benefit from re-planning rather than blind retry.
REPLAN_CANDIDATES: frozenset[FailureClass] = frozenset(
    {FailureClass.TEST_FAILURE, FailureClass.BUILD_FAILURE, FailureClass.INVALID_PLAN}
)


@dataclass(frozen=True)
class RecoveryDecision:
    """The outcome of the recovery policy for one failure."""

    retry: bool
    replan: bool
    fail: bool
    reason: str
    failure_class: FailureClass


class RecoveryPolicy:
    """Decides whether to retry, re-plan, or fail after a failure.

    Boundaries (configurable via :class:`RetryLimits`):
      * a step may be retried at most ``max_step_attempts`` times;
      * a task may be attempted at most ``max_task_attempts`` times;
      * re-planning may happen at most ``max_replans`` times.

    Permission/invalid-plan failures fail immediately (no retry) — retrying a
    deterministic security denial indefinitely is never productive.
    """

    def __init__(self, limits: RetryLimits) -> None:
        self.limits = limits

    def decide(
        self,
        *,
        failure_class: FailureClass,
        step_attempts: int,
        task_attempts: int,
        replans: int,
        permission_state_changed: bool = False,
    ) -> RecoveryDecision:
        # Cancellation is terminal-by-intent: never retry or re-plan.
        if failure_class is FailureClass.CANCELLATION:
            return RecoveryDecision(
                retry=False, replan=False, fail=True,
                reason="task was cancelled; not retrying",
                failure_class=failure_class,
            )
        # Permission failures: fail unless the permission state changed.
        if failure_class is FailureClass.PERMISSION_DENIED:
            if permission_state_changed and step_attempts < self.limits.max_step_attempts:
                return RecoveryDecision(
                    retry=True, replan=False, fail=False,
                    reason="permission state changed; retry allowed",
                    failure_class=failure_class,
                )
            return RecoveryDecision(
                retry=False, replan=False, fail=True,
                reason="permission denied; not retrying a deterministic security failure",
                failure_class=failure_class,
            )

        # Malformed/invalid plan: re-plan once if budget allows, else fail.
        if failure_class is FailureClass.INVALID_PLAN:
            if replans < self.limits.max_replans:
                return RecoveryDecision(
                    retry=False, replan=True, fail=False,
                    reason="invalid plan; re-planning",
                    failure_class=failure_class,
                )
            return RecoveryDecision(
                retry=False, replan=False, fail=True,
                reason="invalid plan and re-plan budget exhausted",
                failure_class=failure_class,
            )

        # Test/build failures: prefer re-planning if budget allows, else retry.
        if failure_class in REPLAN_CANDIDATES and replans < self.limits.max_replans:
            return RecoveryDecision(
                retry=False, replan=True, fail=False,
                reason=f"{failure_class.value}; re-planning with updated observations",
                failure_class=failure_class,
            )

        # Environment failure: bounded retry.
        if failure_class is FailureClass.ENVIRONMENT_FAILURE:
            if step_attempts < self.limits.max_step_attempts:
                return RecoveryDecision(
                    retry=True, replan=False, fail=False,
                    reason="environment failure; bounded retry",
                    failure_class=failure_class,
                )
            if task_attempts < self.limits.max_task_attempts:
                return RecoveryDecision(
                    retry=True, replan=False, fail=False,
                    reason="environment failure; task-level bounded retry",
                    failure_class=failure_class,
                )
            return RecoveryDecision(
                retry=False, replan=False, fail=True,
                reason="environment failure and retry budget exhausted",
                failure_class=failure_class,
            )

        # Generic tool/timeout/unknown: bounded step retry.
        if step_attempts < self.limits.max_step_attempts:
            return RecoveryDecision(
                retry=True, replan=False, fail=False,
                reason=f"{failure_class.value}; bounded step retry",
                failure_class=failure_class,
            )
        if task_attempts < self.limits.max_task_attempts:
            return RecoveryDecision(
                retry=True, replan=False, fail=False,
                reason=f"{failure_class.value}; task-level retry",
                failure_class=failure_class,
            )
        # Last resort: re-plan if still possible, else fail.
        if replans < self.limits.max_replans:
            return RecoveryDecision(
                retry=False, replan=True, fail=False,
                reason=f"{failure_class.value}; retry budget exhausted, re-planning",
                failure_class=failure_class,
            )
        return RecoveryDecision(
            retry=False, replan=False, fail=True,
            reason=f"{failure_class.value}; all budgets exhausted",
            failure_class=failure_class,
        )


@dataclass(frozen=True)
class RetryLimits:
    """Configurable bounds on retries and re-planning."""

    max_step_attempts: int = 2
    max_task_attempts: int = 3
    max_replans: int = 1

    @staticmethod
    def from_settings(s: object) -> RetryLimits:
        return RetryLimits(
            max_step_attempts=getattr(s, "max_step_attempts", 2),
            max_task_attempts=getattr(s, "max_task_attempts", 3),
            max_replans=getattr(s, "max_replans", 1),
        )


def classify_failure(
    *,
    exit_code: int | None = None,
    stderr: str = "",
    error_type: str | None = None,
    error_message: str = "",
    verification_failed: bool = False,
    is_build: bool = False,
) -> FailureClass:
    """Heuristically classify a failure into a :class:`FailureClass`.

    Conservative and pattern-based; callers may override the class explicitly
    when more context is available.
    """
    msg = (error_message or "").lower()
    if error_type:
        et = error_type.lower()
        if "permission" in et or "permissiondenied" in et:
            return FailureClass.PERMISSION_DENIED
        if "sandbox" in et or "runtimeunavailable" in et or "environment" in et:
            return FailureClass.ENVIRONMENT_FAILURE
        if "plan" in et:
            return FailureClass.INVALID_PLAN
        if "timeout" in et or "timedout" in et or "timed_out" in et:
            return FailureClass.TIMEOUT
    if "permission denied" in msg or "is disabled" in msg:
        return FailureClass.PERMISSION_DENIED
    if "timeout" in msg or "timed out" in msg:
        return FailureClass.TIMEOUT
    if verification_failed:
        return FailureClass.TEST_FAILURE if not is_build else FailureClass.BUILD_FAILURE
    if is_build:
        return FailureClass.BUILD_FAILURE
    if exit_code is not None and exit_code != 0:
        return FailureClass.TOOL_FAILURE
    return FailureClass.UNKNOWN


def validate_plan_bounds(steps: list, *, max_steps: int, max_deps: int) -> None:
    """Raise :class:`PlanError` if a plan exceeds configured bounds."""
    if len(steps) > max_steps:
        raise PlanError(
            f"plan exceeds max steps ({len(steps)} > {max_steps})",
            reason="oversized_plan",
        )
    for s in steps:
        if len(getattr(s, "depends_on", [])) > max_deps:
            raise PlanError(
                f"step {getattr(s, 'step_id', '?')} exceeds max dependencies",
                reason="oversized_plan",
            )
