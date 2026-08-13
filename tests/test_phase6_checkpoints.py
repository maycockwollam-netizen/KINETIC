"""Tests for Phase 6 checkpoint extension (repair state persistence)."""

from __future__ import annotations

import pytest

from kinetic.errors import CheckpointError
from kinetic.intelligence.models import RepairState, StuckSignal
from kinetic.tasks.checkpoints import (
    CheckpointStore,
    build_checkpoint,
    restore_checkpoint,
    restore_repair_state,
)
from kinetic.tasks.models import Plan, Task
from kinetic.tasks.states import TaskState


def _task(tmp_path) -> Task:
    return Task(id="t1", user_request="x", workspace=str(tmp_path), state=TaskState.EXECUTING)


def _plan() -> Plan:
    from kinetic.tasks.models import PlanStep

    return Plan(plan_id="p1", task_id="t1", steps=[PlanStep(step_id="s1")])


class TestRepairStateCheckpoint:
    def test_build_and_restore_repair_state(self, tmp_path) -> None:
        task = _task(tmp_path)
        plan = _plan()
        rs = RepairState(
            verification_attempts=2, total_recovery_attempts=3,
            last_failure_signature="sig", stuck=StuckSignal(stuck=True, reason="r"),
            regression_detected=True,
        ).to_dict()
        ckpt = build_checkpoint(task, plan, repair_state=rs)
        assert ckpt["version"] == 2
        task2, plan2, obs = restore_checkpoint(ckpt)
        assert task2.id == "t1"
        restored_rs = restore_repair_state(ckpt)
        assert restored_rs is not None
        assert restored_rs["verification_attempts"] == 2
        assert restored_rs["regression_detected"] is True
        assert restored_rs["stuck"]["stuck"] is True

    def test_restore_repair_state_none_for_v1_checkpoint(self, tmp_path) -> None:
        task = _task(tmp_path)
        plan = _plan()
        ckpt = build_checkpoint(task, plan)
        # Simulate a Phase 1-5 checkpoint without repair_state.
        ckpt.pop("repair_state", None)
        assert restore_repair_state(ckpt) is None

    def test_restore_repair_state_null_is_none(self, tmp_path) -> None:
        task = _task(tmp_path)
        ckpt = build_checkpoint(task, _plan(), repair_state=None)
        assert restore_repair_state(ckpt) is None

    def test_corrupt_repair_state_raises(self, tmp_path) -> None:
        ckpt = build_checkpoint(_task(tmp_path), _plan(), repair_state={"verification_attempts": 2})
        ckpt["repair_state"] = "not a dict"
        with pytest.raises(CheckpointError):
            restore_repair_state(ckpt)

    def test_corrupt_repair_state_wrong_types_raise(self, tmp_path) -> None:
        ckpt = build_checkpoint(_task(tmp_path), _plan())
        ckpt["repair_state"] = {"verification_attempts": "bad", "total_recovery_attempts": 0, "attempts": []}
        with pytest.raises(CheckpointError):
            restore_repair_state(ckpt)

    def test_store_round_trip_with_repair_state(self, tmp_path) -> None:
        store = CheckpointStore(tmp_path / "ckpt")
        task = _task(tmp_path)
        plan = _plan()
        rs = RepairState(verification_attempts=1, total_recovery_attempts=1).to_dict()
        ckpt = build_checkpoint(task, plan, repair_state=rs)
        store.save(ckpt)
        loaded = store.load("t1")
        assert restore_repair_state(loaded) is not None
        assert restore_repair_state(loaded)["verification_attempts"] == 1

    def test_terminal_task_still_refused_for_resume(self, tmp_path) -> None:
        task = Task(id="t1", user_request="x", workspace=str(tmp_path), state=TaskState.COMPLETED)
        ckpt = build_checkpoint(task, _plan())
        with pytest.raises(CheckpointError):
            restore_checkpoint(ckpt)
        # But repair_state is still restorable (inspect terminal tasks).
        assert restore_repair_state(ckpt) is None
