"""Recovery policy tests: classification, retry limits, no-retry on permission, re-plan."""

from __future__ import annotations

from kinetic.events import EventBus
from kinetic.security import AuditLog
from kinetic.tasks.observer import Observation
from kinetic.tasks.policies import (
    FailureClass,
    RecoveryPolicy,
    RetryLimits,
    classify_failure,
)
from kinetic.tasks.recovery import RecoveryCoordinator, failure_context_prompt


class TestFailureClassification:
    def test_permission_denied(self) -> None:
        assert classify_failure(error_message="network access is disabled") is FailureClass.PERMISSION_DENIED

    def test_timeout(self) -> None:
        assert classify_failure(error_message="command timed out") is FailureClass.TIMEOUT

    def test_verification_failed(self) -> None:
        assert classify_failure(verification_failed=True) is FailureClass.TEST_FAILURE

    def test_build_failure(self) -> None:
        assert classify_failure(verification_failed=True, is_build=True) is FailureClass.BUILD_FAILURE

    def test_tool_failure_on_exit_code(self) -> None:
        assert classify_failure(exit_code=2) is FailureClass.TOOL_FAILURE

    def test_unknown(self) -> None:
        assert classify_failure() is FailureClass.UNKNOWN

    def test_environment_error_type(self) -> None:
        assert classify_failure(error_type="RuntimeUnavailableError") is FailureClass.ENVIRONMENT_FAILURE

    def test_plan_error_type(self) -> None:
        assert classify_failure(error_type="PlanError") is FailureClass.INVALID_PLAN


class TestRecoveryPolicy:
    def test_permission_denied_fails_immediately(self) -> None:
        policy = RecoveryPolicy(RetryLimits(max_step_attempts=2, max_task_attempts=3, max_replans=1))
        d = policy.decide(
            failure_class=FailureClass.PERMISSION_DENIED, step_attempts=0, task_attempts=0, replans=0,
        )
        assert d.fail is True
        assert d.retry is False

    def test_permission_denied_retries_if_state_changed(self) -> None:
        policy = RecoveryPolicy(RetryLimits(max_step_attempts=2, max_task_attempts=3, max_replans=1))
        d = policy.decide(
            failure_class=FailureClass.PERMISSION_DENIED, step_attempts=0, task_attempts=0,
            replans=0, permission_state_changed=True,
        )
        assert d.retry is True

    def test_invalid_plan_replans_once(self) -> None:
        policy = RecoveryPolicy(RetryLimits(max_step_attempts=2, max_task_attempts=3, max_replans=1))
        d = policy.decide(
            failure_class=FailureClass.INVALID_PLAN, step_attempts=0, task_attempts=0, replans=0,
        )
        assert d.replan is True
        d2 = policy.decide(
            failure_class=FailureClass.INVALID_PLAN, step_attempts=0, task_attempts=0, replans=1,
        )
        assert d2.fail is True

    def test_test_failure_replans(self) -> None:
        policy = RecoveryPolicy(RetryLimits(max_step_attempts=2, max_task_attempts=3, max_replans=1))
        d = policy.decide(
            failure_class=FailureClass.TEST_FAILURE, step_attempts=0, task_attempts=0, replans=0,
        )
        assert d.replan is True

    def test_environment_failure_bounded_retry(self) -> None:
        policy = RecoveryPolicy(RetryLimits(max_step_attempts=2, max_task_attempts=3, max_replans=1))
        d = policy.decide(
            failure_class=FailureClass.ENVIRONMENT_FAILURE, step_attempts=0, task_attempts=0, replans=0,
        )
        assert d.retry is True
        # After step budget exhausted, task-level retry.
        d = policy.decide(
            failure_class=FailureClass.ENVIRONMENT_FAILURE, step_attempts=2, task_attempts=0, replans=0,
        )
        assert d.retry is True
        # After all budgets, fail.
        d = policy.decide(
            failure_class=FailureClass.ENVIRONMENT_FAILURE, step_attempts=2, task_attempts=3, replans=1,
        )
        assert d.fail is True

    def test_generic_tool_failure_step_retry(self) -> None:
        policy = RecoveryPolicy(RetryLimits(max_step_attempts=2, max_task_attempts=3, max_replans=1))
        d = policy.decide(
            failure_class=FailureClass.TOOL_FAILURE, step_attempts=0, task_attempts=0, replans=0,
        )
        assert d.retry is True

    def test_all_budgets_exhausted_fails(self) -> None:
        policy = RecoveryPolicy(RetryLimits(max_step_attempts=2, max_task_attempts=3, max_replans=1))
        d = policy.decide(
            failure_class=FailureClass.UNKNOWN, step_attempts=2, task_attempts=3, replans=1,
        )
        assert d.fail is True


class TestRecoveryCoordinator:
    async def test_evaluate_emits_events_and_returns_decision(self, tmp_path) -> None:
        events = EventBus()
        coord = RecoveryCoordinator(
            policy=RecoveryPolicy(RetryLimits()), events=events,
            audit=AuditLog(tmp_path / "audit.log"), session_id="s1",
        )
        obs = Observation(step_id="s1", exit_code=1, stderr_summary="permission denied", success=False)
        fclass, decision = coord.evaluate(observation=obs, step_attempts=0, task_attempts=0, replans=0)
        assert fclass is FailureClass.PERMISSION_DENIED
        assert decision.fail is True
        types = [e.type.value for e in events.history]
        assert "task_recovery_started" in types
        assert "task_recovery_completed" in types

    def test_failure_context_prompt_is_bounded(self) -> None:
        obs = Observation(step_id="s1", exit_code=1, stderr_summary="big " * 50, errors=["e1"], success=False)
        prompt = failure_context_prompt(
            failure_class=FailureClass.TEST_FAILURE, observation=obs, plan_goal="fix",
        )
        assert "test_failure" in prompt
        assert "s1" in prompt
        assert "chain-of-thought" not in prompt.lower()
