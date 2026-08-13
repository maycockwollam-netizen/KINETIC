"""Planner tests: validation, dependency ordering, model-plan parsing."""

from __future__ import annotations

import pytest

from errors import PlanError
from tasks.models import Plan, PlanStep, StepStatus
from tasks.planner import (
    invalidate_remaining_steps,
    next_executable_step,
    parse_model_plan,
    reset_failed_steps,
    topological_order,
    validate_plan,
)


def _plan(steps: list[PlanStep], **kw) -> Plan:
    return Plan(plan_id="p1", task_id="t1", steps=steps, **kw)


class TestPlanValidation:
    def test_valid_plan(self) -> None:
        plan = _plan([
            PlanStep(step_id="a", description="inspect"),
            PlanStep(step_id="b", depends_on=["a"]),
            PlanStep(step_id="c", depends_on=["b"]),
        ])
        validate_plan(plan, max_steps=12, max_deps=8)

    def test_empty_plan_rejected(self) -> None:
        with pytest.raises(PlanError, match="no steps"):
            validate_plan(_plan([]), max_steps=12, max_deps=8)

    def test_duplicate_step_ids(self) -> None:
        with pytest.raises(PlanError, match="duplicate"):
            validate_plan(_plan([PlanStep(step_id="a"), PlanStep(step_id="a")]), max_steps=12, max_deps=8)

    def test_unknown_dependency(self) -> None:
        with pytest.raises(PlanError, match="unknown"):
            validate_plan(_plan([PlanStep(step_id="a", depends_on=["zzz"])]), max_steps=12, max_deps=8)

    def test_self_dependency(self) -> None:
        with pytest.raises(PlanError, match="depends on itself"):
            validate_plan(_plan([PlanStep(step_id="a", depends_on=["a"])]), max_steps=12, max_deps=8)

    def test_circular_dependency(self) -> None:
        with pytest.raises(PlanError, match="cycle"):
            validate_plan(
                _plan([
                    PlanStep(step_id="a", depends_on=["b"]),
                    PlanStep(step_id="b", depends_on=["a"]),
                ]),
                max_steps=12, max_deps=8,
            )

    def test_oversized_plan_rejected(self) -> None:
        steps = [PlanStep(step_id=f"s{i}") for i in range(5)]
        with pytest.raises(PlanError, match="max steps"):
            validate_plan(_plan(steps), max_steps=4, max_deps=8)

    def test_too_many_dependencies(self) -> None:
        deps = [f"d{i}" for i in range(3)]
        steps = [PlanStep(step_id=f"d{i}") for i in range(3)] + [
            PlanStep(step_id="s", depends_on=deps)
        ]
        with pytest.raises(PlanError, match="max dependencies"):
            validate_plan(_plan(steps), max_steps=12, max_deps=2)


class TestDependencyOrdering:
    def test_topological_order_respects_dependencies(self) -> None:
        plan = _plan([
            PlanStep(step_id="c", depends_on=["b"]),
            PlanStep(step_id="b", depends_on=["a"]),
            PlanStep(step_id="a"),
        ])
        order = [s.step_id for s in topological_order(plan)]
        assert order.index("a") < order.index("b") < order.index("c")

    def test_next_executable_step(self) -> None:
        plan = _plan([
            PlanStep(step_id="a"),
            PlanStep(step_id="b", depends_on=["a"]),
        ])
        assert next_executable_step(plan).step_id == "a"
        plan.get_step("a").status = StepStatus.COMPLETED
        assert next_executable_step(plan).step_id == "b"
        plan.get_step("b").status = StepStatus.COMPLETED
        assert next_executable_step(plan) is None

    def test_reset_failed_steps(self) -> None:
        plan = _plan([PlanStep(step_id="a")])
        plan.get_step("a").status = StepStatus.FAILED
        reset_failed_steps(plan)
        assert plan.get_step("a").status is StepStatus.PENDING

    def test_invalidate_remaining_steps(self) -> None:
        plan = _plan([
            PlanStep(step_id="a"),
            PlanStep(step_id="b", depends_on=["a"]),
            PlanStep(step_id="c", depends_on=["b"]),
        ])
        plan.get_step("a").status = StepStatus.COMPLETED
        plan.get_step("b").status = StepStatus.COMPLETED
        plan.get_step("c").status = StepStatus.COMPLETED
        invalidate_remaining_steps(plan, from_step_id="b")
        # b and c reset (c depends on b), a stays completed.
        assert plan.get_step("a").status is StepStatus.COMPLETED
        assert plan.get_step("b").status is StepStatus.PENDING
        assert plan.get_step("c").status is StepStatus.PENDING


class TestModelPlanParsing:
    def test_parse_valid_model_plan(self) -> None:
        raw = {
            "plan_id": "p1", "goal": "fix bug",
            "steps": [
                {"step_id": "s1", "description": "inspect"},
                {"id": "s2", "dependencies": ["s1"], "verification": "pytest"},
            ],
            "verification_criteria": "tests pass",
        }
        plan = parse_model_plan(raw, task_id="t1", max_steps=12, max_deps=8)
        assert plan.plan_id == "p1"
        assert len(plan.steps) == 2
        assert plan.steps[1].depends_on == ["s1"]

    def test_parse_rejects_non_dict(self) -> None:
        with pytest.raises(PlanError):
            parse_model_plan([], task_id="t1", max_steps=12, max_deps=8)

    def test_parse_rejects_malformed_steps(self) -> None:
        with pytest.raises(PlanError):
            parse_model_plan({"steps": "not a list"}, task_id="t1", max_steps=12, max_deps=8)

    def test_parse_rejects_cycle(self) -> None:
        raw = {"steps": [
            {"step_id": "a", "depends_on": ["b"]},
            {"step_id": "b", "depends_on": ["a"]},
        ]}
        with pytest.raises(PlanError, match="cycle"):
            parse_model_plan(raw, task_id="t1", max_steps=12, max_deps=8)

    def test_parse_validates_bounds(self) -> None:
        raw = {"steps": [{"step_id": f"s{i}"} for i in range(5)]}
        with pytest.raises(PlanError, match="max steps"):
            parse_model_plan(raw, task_id="t1", max_steps=3, max_deps=8)

    def test_parse_auto_assigns_step_ids(self) -> None:
        raw = {"steps": [{"description": "no id"}, {"description": "also none"}]}
        plan = parse_model_plan(raw, task_id="t1", max_steps=12, max_deps=8)
        assert plan.steps[0].step_id == "step-1"
        assert plan.steps[1].step_id == "step-2"
