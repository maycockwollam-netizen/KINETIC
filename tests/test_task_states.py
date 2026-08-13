"""Task state machine tests: valid/invalid transitions, terminal, cancellation."""

from __future__ import annotations

import pytest

from kinetic.errors import TaskStateError
from kinetic.tasks.states import (
    TERMINAL_STATES,
    TaskState,
    is_terminal,
    require_transition,
    transition_allowed,
)


class TestTaskStateMachine:
    def test_happy_path_transitions(self) -> None:
        assert transition_allowed(TaskState.CREATED, TaskState.PLANNING)
        assert transition_allowed(TaskState.PLANNING, TaskState.PLAN_READY)
        assert transition_allowed(TaskState.PLAN_READY, TaskState.EXECUTING)
        assert transition_allowed(TaskState.EXECUTING, TaskState.VERIFYING)
        assert transition_allowed(TaskState.VERIFYING, TaskState.COMPLETED)

    def test_invalid_transition_raises(self) -> None:
        with pytest.raises(TaskStateError):
            require_transition(TaskState.COMPLETED, TaskState.EXECUTING)
        with pytest.raises(TaskStateError):
            require_transition(TaskState.CREATED, TaskState.EXECUTING)

    def test_any_state_can_fail_or_cancel(self) -> None:
        for s in [
            TaskState.CREATED, TaskState.CONTEXT_READY, TaskState.PLANNING,
            TaskState.PLAN_READY, TaskState.EXECUTING, TaskState.VERIFYING,
            TaskState.RECOVERING,
        ]:
            assert transition_allowed(s, TaskState.FAILED), s
            assert transition_allowed(s, TaskState.CANCELLED), s

    def test_recovering_can_replan_or_resume(self) -> None:
        assert transition_allowed(TaskState.RECOVERING, TaskState.PLANNING)
        assert transition_allowed(TaskState.RECOVERING, TaskState.PLAN_READY)
        assert transition_allowed(TaskState.RECOVERING, TaskState.EXECUTING)

    def test_terminal_states(self) -> None:
        for t in (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED):
            assert is_terminal(t)
            assert t in TERMINAL_STATES
            assert not transition_allowed(t, TaskState.EXECUTING)

    def test_cancelled_is_not_failed(self) -> None:
        # CANCELLED and FAILED are distinct terminals; neither transitions to the other.
        assert not transition_allowed(TaskState.CANCELLED, TaskState.FAILED)
        assert not transition_allowed(TaskState.FAILED, TaskState.CANCELLED)

    def test_error_carries_states(self) -> None:
        try:
            require_transition(TaskState.CREATED, TaskState.COMPLETED)
        except TaskStateError as e:
            assert e.from_state == "created"
            assert e.to_state == "completed"


class TestTaskModel:
    def test_task_summary_excludes_private_reasoning(self) -> None:
        from kinetic.tasks.models import Task

        t = Task(id="t1", user_request="do X", workspace="/ws")
        s = t.summary()
        assert "user_request" not in s  # no raw request in public summary
        assert s["state"] == "created"
        assert s["cancelled"] is False

    def test_empty_request_rejected(self) -> None:
        from kinetic.tasks.models import Task

        with pytest.raises(ValueError):
            Task(id="t1", user_request="   ", workspace="/ws")

    def test_touch_updates_timestamp(self) -> None:
        from kinetic.tasks.models import Task

        t = Task(id="t1", user_request="x", workspace="/ws")
        before = t.updated_at
        t.touch()
        assert t.updated_at >= before

    def test_step_dedupes_dependencies(self) -> None:
        from kinetic.tasks.models import PlanStep

        s = PlanStep(step_id="s1", depends_on=["s2", "s2", "s3"])
        assert s.depends_on == ["s2", "s3"]

    def test_step_empty_id_rejected(self) -> None:
        from kinetic.tasks.models import PlanStep

        with pytest.raises(ValueError):
            PlanStep(step_id="  ")
