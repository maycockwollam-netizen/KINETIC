"""Phase 7 — resource limits + extreme-value tests.

Verifies every untrusted or model-generated quantity has a bound, and that
extreme values are rejected or capped rather than causing unbounded behavior.
"""

from __future__ import annotations

import pydantic
import pytest

from config import Settings


class TestExtremeConfigValues:
    """Extreme configuration values must fail early."""

    @pytest.mark.parametrize("field", [
        "max_step_attempts", "max_task_attempts", "max_replans",
        "max_repair_attempts", "max_verification_attempts",
        "max_total_recovery_attempts",
    ])
    def test_huge_retry_rejected(self, field: str) -> None:
        with pytest.raises((pydantic.ValidationError, ValueError, Exception)):
            Settings(**{field: 1_000_000})

    @pytest.mark.parametrize("field", [
        "max_plan_steps", "max_plan_dependencies",
        "observation_max_stdout_chars", "context_max_characters",
        "memory_candidate_limit", "embedding_dimensions",
    ])
    def test_huge_limit_rejected(self, field: str) -> None:
        with pytest.raises((pydantic.ValidationError, ValueError, Exception)):
            Settings(**{field: 1_000_000_000})

    def test_huge_timeout_rejected(self) -> None:
        with pytest.raises((pydantic.ValidationError, ValueError, Exception)):
            Settings(default_command_timeout=999_999)

    def test_negative_budget_rejected(self) -> None:
        with pytest.raises((pydantic.ValidationError, ValueError, Exception)):
            Settings(max_budget_usd=-5.0)


class TestPlanBounds:
    """Plans must be bounded by max_steps and max_dependencies."""

    def test_oversized_plan_rejected(self) -> None:
        from errors import PlanError
        from tasks.models import Plan, PlanStep
        from tasks.planner import validate_plan

        steps = [PlanStep(step_id=f"s{i}", description="x") for i in range(20)]
        plan = Plan(plan_id="p", task_id="t", goal="g", steps=steps)
        with pytest.raises(PlanError):
            validate_plan(plan, max_steps=10, max_deps=8)

    def test_too_many_deps_rejected(self) -> None:
        from errors import PlanError
        from tasks.models import Plan, PlanStep
        from tasks.planner import validate_plan

        deps = [f"s{i}" for i in range(10)]
        step = PlanStep(step_id="s1", description="x", depends_on=deps)
        plan = Plan(plan_id="p", task_id="t", goal="g", steps=[step])
        with pytest.raises(PlanError):
            validate_plan(plan, max_steps=12, max_deps=5)

    def test_cycle_detected(self) -> None:
        from errors import PlanError
        from tasks.models import Plan, PlanStep
        from tasks.planner import validate_plan

        s1 = PlanStep(step_id="s1", description="x", depends_on=["s2"])
        s2 = PlanStep(step_id="s2", description="x", depends_on=["s1"])
        plan = Plan(plan_id="p", task_id="t", goal="g", steps=[s1, s2])
        with pytest.raises(PlanError):
            validate_plan(plan, max_steps=12, max_deps=8)

    def test_unknown_dependency_rejected(self) -> None:
        from errors import PlanError
        from tasks.models import Plan, PlanStep
        from tasks.planner import validate_plan

        step = PlanStep(step_id="s1", description="x", depends_on=["nonexistent"])
        plan = Plan(plan_id="p", task_id="t", goal="g", steps=[step])
        with pytest.raises(PlanError):
            validate_plan(plan, max_steps=12, max_deps=8)


class TestObservationBounds:
    """Observations must be bounded."""

    def test_stdout_truncated(self) -> None:
        from tasks.observer import Observer

        obs = Observer(max_stdout_chars=100, max_stderr_chars=100)
        result = obs.observe(
            step_id="s1", stdout="x" * 10_000, stderr="", success=True,
        )
        assert len(result.stdout_summary) <= 200  # bounded + suffix

    def test_stderr_truncated(self) -> None:
        from tasks.observer import Observer

        obs = Observer(max_stdout_chars=100, max_stderr_chars=50)
        result = obs.observe(
            step_id="s1", stdout="", stderr="y" * 10_000, success=False,
        )
        assert len(result.stderr_summary) <= 100


class TestCheckpointBounds:
    """Checkpoints must be bounded."""

    def test_observations_bounded_in_checkpoint(self) -> None:
        from tasks.checkpoints import build_checkpoint
        from tasks.models import Plan, Task

        task = Task(id="t1", user_request="do", workspace="/ws")
        plan = Plan(plan_id="p", task_id="t1", goal="g", steps=[])
        # 100 observations — only the last 20 should be persisted.
        observations = [{"step_id": f"s{i}"} for i in range(100)]
        ckpt = build_checkpoint(task, plan, observations=observations)
        assert len(ckpt["observations"]) <= 20


class TestEventPayloadBounds:
    """Event payloads must be bounded."""

    def test_large_payload_truncated(self) -> None:
        from events import EventBus, EventType

        bus = EventBus(max_payload_bytes=500)
        bus.emit(EventType.AGENT_MESSAGE, "s", blob="x" * 100_000)
        event = bus.history[-1]
        import json
        assert len(json.dumps(event.data).encode("utf-8")) <= 500
