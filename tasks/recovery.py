"""Recovery: bounded failure handling.

When verification fails (or a step errors), the recovery layer:
  1. classifies the failure (:class:`FailureClass`),
  2. asks the :class:`RecoveryPolicy` for a decision,
  3. emits recovery events + audit,
  4. returns the decision so the executor can retry / re-plan / fail.

There is no autonomous unlimited retry here — all budgets are bounded and
deterministic permission/security failures fail immediately.
"""

from __future__ import annotations

from events import EventBus, EventType
from security import AuditLog
from tasks.observer import Observation
from tasks.policies import FailureClass, RecoveryDecision, RecoveryPolicy, classify_failure


class RecoveryCoordinator:
    """Coordinates failure classification + recovery decisions + audit/events."""

    def __init__(
        self,
        *,
        policy: RecoveryPolicy,
        events: EventBus,
        audit: AuditLog | None = None,
        session_id: str = "tasks",
    ) -> None:
        self._policy = policy
        self._events = events
        self._audit = audit
        self._session_id = session_id

    def evaluate(
        self,
        *,
        observation: Observation,
        step_attempts: int,
        task_attempts: int,
        replans: int,
        permission_state_changed: bool = False,
        is_build: bool = False,
        verification_failed: bool = False,
    ) -> tuple[FailureClass, RecoveryDecision]:
        """Classify + decide. Returns the class and the recovery decision."""
        failure_class = classify_failure(
            exit_code=observation.exit_code,
            stderr=observation.stderr_summary,
            error_message=" ".join(observation.errors) if observation.errors else observation.stderr_summary,
            verification_failed=verification_failed,
            is_build=is_build,
        )
        # If errors contain an explicit type, hint at permission/env/timeout.
        for err in observation.errors:
            el = err.lower()
            if "permission" in el or "is disabled" in el:
                failure_class = FailureClass.PERMISSION_DENIED
            elif "timeout" in el or "timed out" in el:
                failure_class = FailureClass.TIMEOUT
            elif "sandbox" in el or "runtime" in el or "environment" in el:
                failure_class = FailureClass.ENVIRONMENT_FAILURE

        decision = self._policy.decide(
            failure_class=failure_class,
            step_attempts=step_attempts,
            task_attempts=task_attempts,
            replans=replans,
            permission_state_changed=permission_state_changed,
        )
        self._events.emit(
            EventType.TASK_RECOVERY_STARTED,
            self._session_id,
            failure_class=failure_class.value,
            step_id=observation.step_id,
        )
        self._audit_recovery(decision, observation.step_id)
        self._events.emit(
            EventType.TASK_RECOVERY_COMPLETED,
            self._session_id,
            failure_class=failure_class.value,
            retry=decision.retry,
            replan=decision.replan,
            fail=decision.fail,
            reason=decision.reason,
        )
        return failure_class, decision

    def _audit_recovery(self, decision: RecoveryDecision, step_id: str | None) -> None:
        if self._audit is None:
            return
        self._audit.record(
            session_id=self._session_id,
            action="task_recovery",
            allowed=True,
            reason=decision.reason,
            detail={
                "retry": decision.retry,
                "replan": decision.replan,
                "fail": decision.fail,
                "failure_class": decision.failure_class.value,
                "step_id": step_id,
            },
        )


def failure_context_prompt(
    *,
    failure_class: FailureClass,
    observation: Observation,
    plan_goal: str = "",
    previous_observations: list[Observation] | None = None,
) -> str:
    """Build a bounded, secret-free prompt context for a recovery/re-plan attempt.

    Includes the failure class, a bounded observation summary, and (optionally)
    the previous step outcomes — enough for the model to act, without dumping
    raw conversation history or chain-of-thought.
    """
    lines = [
        f"Recovery context — failure class: {failure_class.value}",
        f"Step: {observation.step_id}",
        f"Exit code: {observation.exit_code}",
    ]
    if plan_goal:
        lines.append(f"Plan goal: {plan_goal}")
    if observation.stderr_summary:
        lines.append(f"stderr (bounded):\n{observation.stderr_summary}")
    if observation.errors:
        lines.append("Errors:\n" + "\n".join(f"- {e}" for e in observation.errors))
    if previous_observations:
        prev = "\n".join(
            f"- {o.step_id}: {o.exit_status} ({o.verification})"
            for o in previous_observations[-6:]
        )
        lines.append(f"Previous step outcomes:\n{prev}")
    lines.append(
        "Adjust the approach based on the above. Do not repeat the exact failing action."
    )
    return "\n".join(lines)
