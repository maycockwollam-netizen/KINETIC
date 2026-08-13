"""TaskManager tests: lifecycle, transitions, cancellation, attempts."""

from __future__ import annotations

from pathlib import Path

import pytest

from errors import TaskStateError
from events import EventBus
from security import AuditLog
from tasks.manager import TaskManager
from tasks.models import TaskFailure
from tasks.states import TaskState


@pytest.fixture
def manager(tmp_path: Path) -> TaskManager:
    return TaskManager(
        events=EventBus(), audit=AuditLog(tmp_path / "audit.log"), session_id="s1",
    )


class TestTaskManager:
    def test_create_and_load(self, manager: TaskManager) -> None:
        t = manager.create(task_id="t1", user_request="do X", workspace="/ws")
        assert t.state is TaskState.CREATED
        assert manager.load("t1") is t
        assert manager.load("t1").user_request == "do X"

    def test_load_unknown_raises(self, manager: TaskManager) -> None:
        with pytest.raises(KeyError):
            manager.load("nope")

    def test_valid_transition(self, manager: TaskManager) -> None:
        manager.create(task_id="t1", user_request="x", workspace="/ws")
        manager.transition("t1", TaskState.PLANNING)
        manager.transition("t1", TaskState.PLAN_READY)
        assert manager.load("t1").state is TaskState.PLAN_READY

    def test_invalid_transition_raises(self, manager: TaskManager) -> None:
        manager.create(task_id="t1", user_request="x", workspace="/ws")
        with pytest.raises(TaskStateError):
            manager.transition("t1", TaskState.EXECUTING)

    def test_cancel(self, manager: TaskManager) -> None:
        manager.create(task_id="t1", user_request="x", workspace="/ws")
        manager.cancel("t1", reason="user")
        t = manager.load("t1")
        assert t.state is TaskState.CANCELLED
        assert t.cancelled is True
        assert t.cancellation_reason == "user"

    def test_cancel_terminal_raises(self, manager: TaskManager) -> None:
        manager.create(task_id="t1", user_request="x", workspace="/ws")
        manager.transition("t1", TaskState.PLANNING)
        manager.mark_failed("t1", failure=TaskFailure(failure_class="unknown", message="boom"))
        with pytest.raises(TaskStateError):
            manager.cancel("t1")

    def test_mark_completed(self, manager: TaskManager) -> None:
        manager.create(task_id="t1", user_request="x", workspace="/ws")
        manager.transition("t1", TaskState.PLANNING)
        manager.transition("t1", TaskState.PLAN_READY)
        manager.transition("t1", TaskState.EXECUTING)
        manager.transition("t1", TaskState.VERIFYING)
        manager.mark_completed("t1")
        assert manager.load("t1").state is TaskState.COMPLETED
        assert manager.load("t1").completed_at is not None

    def test_mark_failed(self, manager: TaskManager) -> None:
        manager.create(task_id="t1", user_request="x", workspace="/ws")
        manager.transition("t1", TaskState.PLANNING)
        manager.mark_failed("t1", failure=TaskFailure(failure_class="test_failure", message="tests failed"))
        t = manager.load("t1")
        assert t.state is TaskState.FAILED
        assert t.failure is not None
        assert t.failure.failure_class == "test_failure"

    def test_mark_failed_on_terminal_records_without_state_change(self, manager: TaskManager) -> None:
        manager.create(task_id="t1", user_request="x", workspace="/ws")
        manager.cancel("t1")
        # Recording a failure on a cancelled task must not reinterpret as failed.
        manager.mark_failed("t1", failure=TaskFailure(failure_class="unknown", message="late"))
        assert manager.load("t1").state is TaskState.CANCELLED
        assert manager.load("t1").failure is not None

    def test_attempt_and_replan_tracking(self, manager: TaskManager) -> None:
        manager.create(task_id="t1", user_request="x", workspace="/ws")
        assert manager.increment_attempt("t1") == 1
        assert manager.increment_attempt("t1") == 2
        assert manager.increment_replan("t1") == 1

    def test_set_plan_and_step(self, manager: TaskManager) -> None:
        manager.create(task_id="t1", user_request="x", workspace="/ws")
        manager.set_plan("t1", "plan-1")
        manager.set_current_step("t1", "s2")
        assert manager.load("t1").plan_id == "plan-1"
        assert manager.load("t1").current_step == "s2"

    def test_cancel_emits_distinct_events(self, manager: TaskManager) -> None:
        manager.create(task_id="t1", user_request="x", workspace="/ws")
        manager.cancel("t1")
        types = [e.type.value for e in manager._events.history]
        assert "task_cancelled" in types
        assert "task_failed" not in types
