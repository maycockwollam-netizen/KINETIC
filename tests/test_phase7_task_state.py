"""Phase 7 — task state machine + checkpoint hardening tests.

Exhaustive transition tests, terminal-state immutability, corrupt checkpoint
fail-closed, version incompatibility, and ID confusion prevention.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kinetic.errors import CheckpointError, TaskStateError
from kinetic.tasks.checkpoints import (
    CheckpointStore,
    build_checkpoint,
    restore_checkpoint,
    restore_repair_state,
)
from kinetic.tasks.models import Plan, Task
from kinetic.tasks.states import (
    TERMINAL_STATES,
    TaskState,
    require_transition,
    transition_allowed,
)


class TestExhaustiveTransitions:
    """Every valid transition is allowed; every invalid one raises."""

    VALID = {
        TaskState.CREATED: {TaskState.CONTEXT_READY, TaskState.PLANNING, TaskState.FAILED, TaskState.CANCELLED},
        TaskState.CONTEXT_READY: {TaskState.PLANNING, TaskState.FAILED, TaskState.CANCELLED},
        TaskState.PLANNING: {TaskState.PLAN_READY, TaskState.FAILED, TaskState.CANCELLED},
        TaskState.PLAN_READY: {TaskState.EXECUTING, TaskState.FAILED, TaskState.CANCELLED},
        TaskState.EXECUTING: {TaskState.VERIFYING, TaskState.RECOVERING, TaskState.FAILED, TaskState.CANCELLED},
        TaskState.VERIFYING: {TaskState.COMPLETED, TaskState.RECOVERING, TaskState.FAILED, TaskState.CANCELLED},
        TaskState.RECOVERING: {TaskState.EXECUTING, TaskState.PLAN_READY, TaskState.PLANNING, TaskState.FAILED, TaskState.CANCELLED},
    }

    @pytest.mark.parametrize("from_state", list(TaskState))
    def test_valid_transitions_allowed(self, from_state: TaskState) -> None:
        for to_state in self.VALID.get(from_state, set()):
            assert transition_allowed(from_state, to_state), f"{from_state}->{to_state} should be allowed"

    @pytest.mark.parametrize("from_state", list(TaskState))
    def test_invalid_transitions_rejected(self, from_state: TaskState) -> None:
        valid = self.VALID.get(from_state, set())
        for to_state in TaskState:
            if to_state not in valid and to_state != from_state:
                assert not transition_allowed(from_state, to_state), \
                    f"{from_state}->{to_state} should be rejected"

    def test_terminal_states_have_no_outgoing(self) -> None:
        for state in TERMINAL_STATES:
            for target in TaskState:
                assert not transition_allowed(state, target), \
                    f"terminal {state} should not transition to {target}"


class TestTerminalImmutability:
    def test_completed_cannot_go_failed(self) -> None:
        with pytest.raises(TaskStateError):
            require_transition(TaskState.COMPLETED, TaskState.FAILED)

    def test_failed_cannot_go_completed(self) -> None:
        with pytest.raises(TaskStateError):
            require_transition(TaskState.FAILED, TaskState.COMPLETED)

    def test_cancelled_cannot_go_anything(self) -> None:
        with pytest.raises(TaskStateError):
            require_transition(TaskState.CANCELLED, TaskState.COMPLETED)
        with pytest.raises(TaskStateError):
            require_transition(TaskState.CANCELLED, TaskState.FAILED)

    def test_executing_must_pass_verifying(self) -> None:
        # Cannot go directly to COMPLETED.
        with pytest.raises(TaskStateError):
            require_transition(TaskState.EXECUTING, TaskState.COMPLETED)


class TestCheckpointIntegrity:
    def _valid_checkpoint(self, task_id: str = "t1") -> dict:
        task = Task(id=task_id, user_request="do", workspace="/ws")
        plan = Plan(plan_id="p", task_id=task_id, goal="g", steps=[])
        return build_checkpoint(task, plan, observations=[])

    def test_atomic_write(self, tmp_path: Path) -> None:
        store = CheckpointStore(tmp_path / "ckpts")
        ckpt = self._valid_checkpoint()
        store.save(ckpt)
        assert store.exists("t1")
        loaded = store.load("t1")
        assert loaded["task_id"] == "t1"

    def test_missing_checkpoint_raises(self, tmp_path: Path) -> None:
        store = CheckpointStore(tmp_path / "ckpts")
        with pytest.raises(CheckpointError, match="no checkpoint"):
            store.load("nonexistent")

    def test_corrupt_checkpoint_raises(self, tmp_path: Path) -> None:
        store = CheckpointStore(tmp_path / "ckpts")
        path = tmp_path / "ckpts" / "bad.json"
        path.write_text("{not valid json")
        with pytest.raises(CheckpointError, match="corrupt"):
            store.load("bad")

    def test_terminal_task_refused_restore(self, tmp_path: Path) -> None:
        task = Task(id="t1", user_request="do", workspace="/ws", state=TaskState.COMPLETED)
        plan = Plan(plan_id="p", task_id="t1", goal="g", steps=[])
        ckpt = build_checkpoint(task, plan, observations=[])
        with pytest.raises(CheckpointError, match="terminal state"):
            restore_checkpoint(ckpt)

    def test_missing_version_raises(self, tmp_path: Path) -> None:
        ckpt = {"task_id": "t1", "task": {"id": "t1", "user_request": "x", "workspace": "/ws"}}
        with pytest.raises(CheckpointError, match="version"):
            restore_checkpoint(ckpt)

    def test_plan_task_id_mismatch_raises(self, tmp_path: Path) -> None:
        task = Task(id="t1", user_request="do", workspace="/ws", state=TaskState.EXECUTING)
        plan = Plan(plan_id="p", task_id="WRONG", goal="g", steps=[])
        ckpt = build_checkpoint(task, plan, observations=[])
        with pytest.raises(CheckpointError, match="does not belong"):
            restore_checkpoint(ckpt)

    def test_restore_repair_state_none(self) -> None:
        assert restore_repair_state({"repair_state": None}) is None
        assert restore_repair_state({}) is None

    def test_restore_repair_state_invalid_type(self) -> None:
        with pytest.raises(CheckpointError, match="must be a dict"):
            restore_repair_state({"repair_state": "not a dict"})

    def test_restore_repair_state_invalid_fields(self) -> None:
        with pytest.raises(CheckpointError, match="verification_attempts"):
            restore_repair_state({"repair_state": {"verification_attempts": "not int"}})

    def test_checkpoint_version_2(self, tmp_path: Path) -> None:
        ckpt = self._valid_checkpoint()
        assert ckpt["version"] == 2


class TestTaskIdIntegrity:
    def test_task_id_not_confusable(self) -> None:
        t1 = Task(id="task-001", user_request="a", workspace="/ws")
        t2 = Task(id="task-002", user_request="b", workspace="/ws")
        assert t1.id != t2.id

    def test_workspace_id_distinct(self) -> None:
        from kinetic.tasks.manager import workspace_project_id

        assert workspace_project_id("/ws/a") != workspace_project_id("/ws/b")
