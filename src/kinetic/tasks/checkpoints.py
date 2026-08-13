"""Lightweight local task checkpoints.

A checkpoint captures enough deterministic state to resume a task safely:
task snapshot, plan snapshot, completed steps, attempt counters, and the
bounded observations needed for continuation. Checkpoints are stored as JSON
files under the configured directory — no distributed persistence.

Restoration is fail-closed: a missing or corrupt checkpoint raises
:class:`~kinetic.errors.CheckpointError` rather than silently continuing with
possibly-corrupted state.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kinetic.errors import CheckpointError
from kinetic.tasks.models import Plan, PlanStep, Task, TaskFailure
from kinetic.tasks.states import TaskState


class CheckpointStore:
    """JSON-file-backed checkpoint store.

    Each checkpoint is one file named ``<task_id>.json``. Writes are atomic
    (write temp + replace) so an interrupted write cannot corrupt an existing
    checkpoint.
    """

    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, task_id: str) -> Path:
        return self._dir / f"{task_id}.json"

    def save(self, checkpoint: dict[str, Any]) -> None:
        task_id = checkpoint.get("task_id")
        if not task_id:
            raise CheckpointError("checkpoint missing task_id")
        path = self._path(str(task_id))
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(checkpoint, default=str, indent=2), encoding="utf-8")
        tmp.replace(path)

    def load(self, task_id: str) -> dict[str, Any]:
        path = self._path(task_id)
        if not path.exists():
            raise CheckpointError(f"no checkpoint for task {task_id}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CheckpointError(f"corrupt checkpoint for task {task_id}: {exc}") from exc
        return data

    def exists(self, task_id: str) -> bool:
        return self._path(task_id).exists()

    def delete(self, task_id: str) -> bool:
        path = self._path(task_id)
        if path.exists():
            path.unlink()
            return True
        return False


def build_checkpoint(
    task: Task,
    plan: Plan | None,
    *,
    observations: list[dict[str, Any]] | None = None,
    completed_step_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Build a serializable checkpoint from a task + plan + observations."""
    return {
        "task_id": task.id,
        "task": task.model_dump(mode="json"),
        "plan": plan.model_dump(mode="json") if plan else None,
        "observations": list(observations or [])[-20:],  # bounded
        "completed_step_ids": list(completed_step_ids or []),
        "version": 1,
    }


def restore_checkpoint(data: dict[str, Any]) -> tuple[Task, Plan | None, list[dict[str, Any]]]:
    """Restore a task + plan + observations from checkpoint data.

    Fail-closed: validates the checkpoint structure and raises
    :class:`CheckpointError` if it is incomplete or inconsistent.
    """
    if not isinstance(data, dict):
        raise CheckpointError("checkpoint is not a dict")
    if data.get("version") is None:
        raise CheckpointError("checkpoint missing version")
    raw_task = data.get("task")
    if not isinstance(raw_task, dict):
        raise CheckpointError("checkpoint missing task")
    # Refuse to resume a terminal task — nothing to resume.
    state = raw_task.get("state")
    if state and TaskState(state) in (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED):
        raise CheckpointError(
            f"cannot resume task in terminal state {state}"
        )
    try:
        task = Task.model_validate(raw_task)
    except Exception as exc:  # noqa: BLE001
        raise CheckpointError(f"invalid task in checkpoint: {exc}") from exc
    plan: Plan | None = None
    raw_plan = data.get("plan")
    if isinstance(raw_plan, dict):
        try:
            plan = Plan.model_validate(raw_plan)
        except Exception as exc:  # noqa: BLE001
            raise CheckpointError(f"invalid plan in checkpoint: {exc}") from exc
        # Consistency: plan must belong to this task.
        if plan.task_id != task.id:
            raise CheckpointError("checkpoint plan does not belong to this task")
    observations = data.get("observations")
    if observations is not None and not isinstance(observations, list):
        raise CheckpointError("checkpoint observations must be a list")
    return task, plan, list(observations or [])


__all__ = [
    "CheckpointStore",
    "build_checkpoint",
    "restore_checkpoint",
    "Task",
    "Plan",
    "PlanStep",
    "TaskFailure",
]
