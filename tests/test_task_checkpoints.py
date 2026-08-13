"""Checkpoint tests: create, restore, corruption, safe resume."""

from __future__ import annotations

from pathlib import Path

import pytest

from errors import CheckpointError
from tasks.checkpoints import CheckpointStore, build_checkpoint, restore_checkpoint
from tasks.models import Plan, PlanStep, Task
from tasks.states import TaskState


@pytest.fixture
def store(tmp_path: Path) -> CheckpointStore:
    return CheckpointStore(tmp_path / "ckpts")


@pytest.fixture
def task() -> Task:
    t = Task(id="t1", user_request="do X", workspace="/ws")
    t.state = TaskState.EXECUTING
    t.plan_id = "p1"
    t.current_step = "s2"
    return t


@pytest.fixture
def plan() -> Plan:
    return Plan(
        plan_id="p1", task_id="t1", goal="g",
        steps=[PlanStep(step_id="s1"), PlanStep(step_id="s2", depends_on=["s1"])],
    )


class TestCheckpointStore:
    def test_save_and_load(self, store: CheckpointStore, task: Task, plan: Plan) -> None:
        ckpt = build_checkpoint(task, plan, observations=[{"step_id": "s1"}], completed_step_ids=["s1"])
        store.save(ckpt)
        assert store.exists("t1")
        data = store.load("t1")
        assert data["task_id"] == "t1"
        assert data["plan"]["plan_id"] == "p1"
        assert data["completed_step_ids"] == ["s1"]

    def test_load_missing_raises(self, store: CheckpointStore) -> None:
        with pytest.raises(CheckpointError):
            store.load("nope")

    def test_corrupt_checkpoint_raises(self, store: CheckpointStore) -> None:
        path = store._dir / "t1.json"
        path.write_text("{not valid json")
        with pytest.raises(CheckpointError):
            store.load("t1")

    def test_save_missing_task_id_raises(self, store: CheckpointStore) -> None:
        with pytest.raises(CheckpointError):
            store.save({"plan": {}})

    def test_delete(self, store: CheckpointStore, task: Task, plan: Plan) -> None:
        store.save(build_checkpoint(task, plan))
        assert store.delete("t1") is True
        assert not store.exists("t1")
        assert store.delete("t1") is False

    def test_atomic_write_no_partial(self, store: CheckpointStore, task: Task, plan: Plan) -> None:
        store.save(build_checkpoint(task, plan))
        # No .tmp file left behind.
        assert not list(store._dir.glob("*.tmp"))


class TestRestoreCheckpoint:
    def test_round_trip(self, store: CheckpointStore, task: Task, plan: Plan) -> None:
        ckpt = build_checkpoint(task, plan, observations=[{"step_id": "s1"}])
        store.save(ckpt)
        data = store.load("t1")
        t, p, obs = restore_checkpoint(data)
        assert t.id == "t1"
        assert t.state is TaskState.EXECUTING
        assert p.plan_id == "p1"
        assert p.task_id == "t1"
        assert obs == [{"step_id": "s1"}]

    def test_refuse_terminal_task(self, task: Task, plan: Plan) -> None:
        task.state = TaskState.COMPLETED
        ckpt = build_checkpoint(task, plan)
        with pytest.raises(CheckpointError, match="terminal"):
            restore_checkpoint(ckpt)

    def test_plan_task_id_mismatch_raises(self, task: Task) -> None:
        bad_plan = Plan(plan_id="p1", task_id="other", steps=[PlanStep(step_id="s1")])
        ckpt = build_checkpoint(task, bad_plan)
        with pytest.raises(CheckpointError, match="does not belong"):
            restore_checkpoint(ckpt)

    def test_missing_version_raises(self, task: Task) -> None:
        data = {"task": task.model_dump(mode="json"), "plan": None}
        with pytest.raises(CheckpointError, match="version"):
            restore_checkpoint(data)

    def test_no_plan_allowed(self, task: Task) -> None:
        ckpt = build_checkpoint(task, None)
        t, p, obs = restore_checkpoint(ckpt)
        assert p is None
        assert obs == []

    def test_observations_bounded(self, task: Task, plan: Plan) -> None:
        many = [{"step_id": f"s{i}"} for i in range(100)]
        ckpt = build_checkpoint(task, plan, observations=many)
        assert len(ckpt["observations"]) <= 20
