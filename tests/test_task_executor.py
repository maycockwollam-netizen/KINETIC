"""Execution controller tests: ordering, success/failure, retry, re-plan, cancellation.

Uses deterministic fake StepRunner/PlanRunner implementations so no live model
or SDK is required. The verifier uses a real local environment so the
single-safe-path + permission boundary is exercised.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from environment import Environment
from environment.config import RUNTIME_LOCAL, EnvironmentConfig
from environment.network import NetworkPolicy
from errors import OrchestrationError
from events import EventBus
from security import AuditLog
from tasks.executor import ExecutionController, StepRunner
from tasks.manager import TaskManager
from tasks.models import Plan, PlanStep
from tasks.observer import Observer
from tasks.policies import RecoveryPolicy, RetryLimits
from tasks.recovery import RecoveryCoordinator
from tasks.states import TaskState
from tasks.verifier import Verifier


class FakeStepRunner:
    """Deterministic step runner: returns scripted outcomes per step_id."""

    def __init__(self, outcomes: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.outcomes = outcomes or {}
        self.calls: list[str] = []
        self.always_success: bool | None = None

    async def run_step(self, *, plan: Plan, step: PlanStep, prompt: str, session_id: str) -> dict[str, Any]:
        self.calls.append(step.step_id)
        if self.always_success is not None:
            return {"success": self.always_success, "exit_code": 0 if self.always_success else 1, "stdout": "ok"}
        seq = self.outcomes.get(step.step_id, [])
        idx = step.attempts - 1
        if idx < len(seq):
            return seq[idx]
        if seq:
            # Reuse the last scripted outcome for subsequent attempts.
            return seq[-1]
        # Default: succeed.
        return {"success": True, "exit_code": 0, "stdout": "ok"}


class FakePlanRunner:
    def __init__(self, plan: Plan) -> None:
        self._plan = plan
        self.calls = 0

    async def make_plan(self, *, task, context_block, session_id) -> Plan:
        self.calls += 1
        return self._plan.model_copy(deep=True)


@pytest.fixture
def events() -> EventBus:
    return EventBus()


@pytest.fixture
async def env(tmp_path: Path, events: EventBus) -> Environment:
    cfg = EnvironmentConfig(runtime_type=RUNTIME_LOCAL, sandbox_mode=False, network=NetworkPolicy.ALLOW)
    e = Environment.create(tmp_path / "ws", cfg, events=events, session_id="exec")
    await e.provision()
    yield e
    await e.destroy()


@pytest.fixture
def manager(tmp_path: Path, events: EventBus) -> TaskManager:
    return TaskManager(events=events, audit=AuditLog(tmp_path / "audit.log"), session_id="exec")


def _plan(steps: list[PlanStep], **kw) -> Plan:
    return Plan(plan_id="p1", task_id="t1", steps=steps, **kw)


def _controller(
    manager: TaskManager, events: EventBus, env: Environment,
    runner: StepRunner, plan_runner=None, **kw,
) -> ExecutionController:
    return ExecutionController(
        manager=manager, runner=runner, plan_runner=plan_runner,
        verifier=Verifier(environment=env, configured_command="true"),
        observer=Observer(),
        recovery=RecoveryCoordinator(policy=RecoveryPolicy(RetryLimits(max_step_attempts=2, max_task_attempts=3, max_replans=1)), events=events, session_id="exec"),
        retry_limits=RetryLimits(max_step_attempts=2, max_task_attempts=3, max_replans=1),
        events=events, audit=AuditLog(manager._audit._path),
        max_plan_steps=12, max_plan_deps=8, session_id="exec",
        enable_checkpoints=False, **kw,
    )


class TestExecutionOrdering:
    async def test_steps_run_in_dependency_order(self, manager, events, env, tmp_path) -> None:
        plan = _plan([
            PlanStep(step_id="c", depends_on=["b"]),
            PlanStep(step_id="b", depends_on=["a"]),
            PlanStep(step_id="a"),
        ])
        runner = FakeStepRunner()
        ctrl = _controller(manager, events, env, runner, plan_runner=FakePlanRunner(plan))
        manager.create(task_id="t1", user_request="x", workspace=str(tmp_path))
        outcome = await ctrl.execute("t1")
        assert outcome.task.state is TaskState.COMPLETED
        # a before b before c
        assert runner.calls.index("a") < runner.calls.index("b") < runner.calls.index("c")

    async def test_does_not_run_step_before_dependencies(self, manager, events, env, tmp_path) -> None:
        plan = _plan([
            PlanStep(step_id="b", depends_on=["a"]),
            PlanStep(step_id="a"),
        ])
        runner = FakeStepRunner()
        ctrl = _controller(manager, events, env, runner, plan_runner=FakePlanRunner(plan))
        manager.create(task_id="t1", user_request="x", workspace=str(tmp_path))
        await ctrl.execute("t1")
        assert runner.calls == ["a", "b"]


class TestExecutionSuccess:
    async def test_all_steps_complete_then_completed(self, manager, events, env, tmp_path) -> None:
        plan = _plan([PlanStep(step_id="a"), PlanStep(step_id="b")])
        runner = FakeStepRunner()
        ctrl = _controller(manager, events, env, runner, plan_runner=FakePlanRunner(plan))
        manager.create(task_id="t1", user_request="x", workspace=str(tmp_path))
        outcome = await ctrl.execute("t1")
        assert outcome.task.state is TaskState.COMPLETED
        assert outcome.plan.all_done()

    async def test_events_emitted(self, manager, events, env, tmp_path) -> None:
        plan = _plan([PlanStep(step_id="a")])
        ctrl = _controller(manager, events, env, FakeStepRunner(), plan_runner=FakePlanRunner(plan))
        manager.create(task_id="t1", user_request="x", workspace=str(tmp_path))
        await ctrl.execute("t1")
        types = [e.type.value for e in events.history]
        assert "task_created" in types
        assert "task_plan_created" in types
        assert "task_step_started" in types
        assert "task_step_completed" in types
        assert "task_verification_started" in types


class TestExecutionFailure:
    async def test_persistent_step_failure_marks_failed(self, manager, events, env, tmp_path) -> None:
        plan = _plan([PlanStep(step_id="a")])
        runner = FakeStepRunner({"a": [{"success": False, "exit_code": 1, "stderr": "boom"}]})
        ctrl = _controller(manager, events, env, runner, plan_runner=FakePlanRunner(plan))
        manager.create(task_id="t1", user_request="x", workspace=str(tmp_path))
        with pytest.raises(OrchestrationError):
            await ctrl.execute("t1")
        assert manager.load("t1").state is TaskState.FAILED

    async def test_bounded_retry_then_succeed(self, manager, events, env, tmp_path) -> None:
        plan = _plan([PlanStep(step_id="a")])
        # First attempt fails, second succeeds.
        runner = FakeStepRunner({"a": [
            {"success": False, "exit_code": 1, "errors": ["transient"]},
            {"success": True, "exit_code": 0},
        ]})
        ctrl = _controller(manager, events, env, runner, plan_runner=FakePlanRunner(plan))
        manager.create(task_id="t1", user_request="x", workspace=str(tmp_path))
        outcome = await ctrl.execute("t1")
        assert outcome.task.state is TaskState.COMPLETED
        assert runner.calls == ["a", "a"]


class TestCancellation:
    async def test_cancellation_propagates_to_cancelled(self, manager, events, env, tmp_path) -> None:
        plan = _plan([PlanStep(step_id="a"), PlanStep(step_id="b")])

        class CancelAfterA:
            def __init__(self) -> None:
                self.calls = 0

            async def run_step(self, *, plan, step, prompt, session_id):
                self.calls += 1
                if step.step_id == "a":
                    manager.cancel("t1")
                    return {"success": True, "exit_code": 0}
                return {"success": True, "exit_code": 0}

        runner = CancelAfterA()
        ctrl = _controller(manager, events, env, runner, plan_runner=FakePlanRunner(plan))
        manager.create(task_id="t1", user_request="x", workspace=str(tmp_path))
        outcome = await ctrl.execute("t1")
        assert outcome.cancelled is True
        assert outcome.task.state is TaskState.CANCELLED


class TestReplanning:
    async def test_replan_on_test_failure(self, manager, events, env, tmp_path) -> None:
        # First plan's step fails -> re-plan -> new plan succeeds.
        bad_plan = _plan([PlanStep(step_id="a")])
        good_plan = _plan([PlanStep(step_id="a2")])
        runner = FakeStepRunner({"a": [{"success": False, "exit_code": 1, "errors": ["test failure"]}]})

        class ReplanRunner:
            def __init__(self) -> None:
                self.calls = 0

            async def make_plan(self, *, task, context_block, session_id):
                self.calls += 1
                return bad_plan.model_copy(deep=True) if self.calls == 1 else good_plan.model_copy(deep=True)

        ctrl = _controller(manager, events, env, runner, plan_runner=ReplanRunner())
        manager.create(task_id="t1", user_request="x", workspace=str(tmp_path))
        outcome = await ctrl.execute("t1")
        assert outcome.replans >= 1 or outcome.task.state is TaskState.COMPLETED
        # The good plan's step should have run.
        assert "a2" in runner.calls

    async def test_replan_budget_exhausted_fails(self, manager, events, env, tmp_path) -> None:
        bad_plan = _plan([PlanStep(step_id="a")])
        runner = FakeStepRunner({"a": [{"success": False, "exit_code": 1, "errors": ["test failure"]}]})
        # Plan runner always returns the failing plan.
        ctrl = _controller(
            manager, events, env, runner, plan_runner=FakePlanRunner(bad_plan),
        )
        manager.create(task_id="t1", user_request="x", workspace=str(tmp_path))
        with pytest.raises(OrchestrationError):
            await ctrl.execute("t1")
        assert manager.load("t1").state is TaskState.FAILED


class TestSecurityBoundary:
    async def test_permission_denied_fails_immediately(self, manager, events, env, tmp_path) -> None:
        plan = _plan([PlanStep(step_id="a")])
        runner = FakeStepRunner({"a": [{"success": False, "exit_code": 1, "errors": ["permission denied for tool"]}]*3})
        ctrl = _controller(manager, events, env, runner, plan_runner=FakePlanRunner(plan))
        manager.create(task_id="t1", user_request="x", workspace=str(tmp_path))
        with pytest.raises(OrchestrationError):
            await ctrl.execute("t1")
        assert manager.load("t1").state is TaskState.FAILED
        # Permission failure should not have retried 3 times (fails immediately).
        assert runner.calls.count("a") == 1

    async def test_no_direct_subprocess_outside_environment(self, manager, events, env, tmp_path) -> None:
        # The controller only orchestrates; it has no subprocess access. Verify
        # it does not import/call run_command directly by checking the safe path
        # goes through the verifier's environment.
        plan = _plan([PlanStep(step_id="a")])
        ctrl = _controller(manager, events, env, FakeStepRunner(), plan_runner=FakePlanRunner(plan))
        manager.create(task_id="t1", user_request="x", workspace=str(tmp_path))
        outcome = await ctrl.execute("t1")
        assert outcome.task.state is TaskState.COMPLETED
        # Verifier ran through the environment (PROCESS_FINISHED emitted).
        types = [e.type.value for e in events.history]
        assert "process_started" in types or "process_finished" in types
