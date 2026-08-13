"""TaskManager: the authoritative task state machine.

The manager owns task lifecycle: create, load, transition, cancel, mark
completed/failed, and track attempts. It is the ONLY writer of task state —
Planner/Executor/Recovery never mutate state directly, they call the manager.

State transitions are validated against :mod:`kinetic.tasks.states`; invalid
transitions raise :class:`~kinetic.errors.TaskStateError`. Cancellation is
distinct from failure: a cancelled task enters ``CANCELLED`` and is never
reinterpreted as failed.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from kinetic.errors import TaskStateError
from kinetic.events import EventBus, EventType
from kinetic.security import AuditLog
from kinetic.tasks.models import Task, TaskFailure
from kinetic.tasks.states import TaskState, is_terminal, require_transition, transition_allowed

if TYPE_CHECKING:
    from kinetic.observability import MetricsCollector


class TaskManager:
    """The authoritative state machine for tasks.

    Tasks are kept in memory and persisted to a checkpoint store when enabled.
    The manager emits structured events and writes audit records for
    security-sensitive transitions (cancellation, failure, completion).
    """

    def __init__(
        self,
        *,
        events: EventBus,
        audit: AuditLog | None = None,
        session_id: str = "tasks",
        store: Any | None = None,
        metrics: MetricsCollector | None = None,
    ) -> None:
        self._events = events
        self._audit = audit
        self._session_id = session_id
        self._store = store
        self._metrics = metrics
        self._tasks: dict[str, Task] = {}

    # --- creation / loading ------------------------------------------------

    def create(
        self,
        *,
        task_id: str,
        user_request: str,
        workspace: str,
        project_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Task:
        task = Task(
            id=task_id,
            user_request=user_request,
            workspace=workspace,
            project_id=project_id,
            metadata=dict(metadata or {}),
        )
        self._tasks[task.id] = task
        self._emit(EventType.TASK_CREATED, task_id=task.id, workspace=workspace)
        if self._metrics is not None:
            from kinetic.observability.metrics import METRIC_TASKS_STARTED

            self._metrics.inc(METRIC_TASKS_STARTED)
        return task

    def load(self, task_id: str) -> Task:
        if task_id not in self._tasks:
            raise KeyError(f"unknown task: {task_id}")
        return self._tasks[task_id]

    def all_tasks(self) -> list[Task]:
        return list(self._tasks.values())

    # --- state transitions -------------------------------------------------

    def transition(self, task_id: str, to: TaskState, *, reason: str = "") -> Task:
        """Move a task from its current state to ``to`` (validated)."""
        task = self.load(task_id)
        require_transition(task.state, to)
        from_state = task.state
        task.state = to
        task.touch()
        self._emit(
            EventType.TASK_STATE_CHANGED,
            task_id=task.id, from_state=from_state.value, to_state=to.value, reason=reason,
        )
        if to is TaskState.CANCELLED:
            self._audit_cancel(task, reason)
        return task

    def cancel(self, task_id: str, *, reason: str = "") -> Task:
        """Cancel a task. Terminal tasks cannot be cancelled."""
        task = self.load(task_id)
        if is_terminal(task.state):
            raise TaskStateError(task.state.value, TaskState.CANCELLED.value)
        task.cancelled = True
        task.cancellation_reason = reason or "cancelled by user"
        return self.transition(task_id, TaskState.CANCELLED, reason=task.cancellation_reason)

    def mark_completed(self, task_id: str) -> Task:
        task = self.transition(task_id, TaskState.COMPLETED, reason="completed")
        task.completed_at = task.updated_at
        self._emit(EventType.TASK_COMPLETED, task_id=task.id)
        self._audit_record("task_completed", task, allowed=True)
        if self._metrics is not None:
            from kinetic.observability.metrics import METRIC_TASK_DURATION, METRIC_TASKS_COMPLETED

            self._metrics.inc(METRIC_TASKS_COMPLETED)
            duration = _task_duration_seconds(task)
            if duration is not None:
                self._metrics.set_gauge(METRIC_TASK_DURATION, duration)
        return task

    def mark_failed(self, task_id: str, *, failure: TaskFailure) -> Task:
        task = self.load(task_id)
        if transition_allowed(task.state, TaskState.FAILED):
            task.failure = failure
            self.transition(task_id, TaskState.FAILED, reason=failure.message)
        else:
            # Already terminal (e.g. cancelled): record failure info without
            # changing state so we never reinterpret cancellation as failure.
            task.failure = failure
            task.touch()
        self._emit(
            EventType.TASK_FAILED,
            task_id=task.id, failure_class=failure.failure_class, message=failure.message,
        )
        self._audit_record("task_failed", task, allowed=False, reason=failure.message)
        if self._metrics is not None:
            from kinetic.observability.metrics import METRIC_TASKS_FAILED

            self._metrics.inc(METRIC_TASKS_FAILED)
        return task

    # --- attempt tracking --------------------------------------------------

    def increment_attempt(self, task_id: str) -> int:
        task = self.load(task_id)
        task.attempt_count += 1
        task.touch()
        return task.attempt_count

    def increment_replan(self, task_id: str) -> int:
        task = self.load(task_id)
        task.replan_count += 1
        task.touch()
        return task.replan_count

    def set_plan(self, task_id: str, plan_id: str) -> Task:
        task = self.load(task_id)
        task.plan_id = plan_id
        task.touch()
        return task

    def set_current_step(self, task_id: str, step_id: str | None) -> Task:
        task = self.load(task_id)
        task.current_step = step_id
        task.touch()
        return task

    # --- checkpoint persistence -------------------------------------------

    def snapshot(self, task_id: str, checkpoint: dict[str, Any]) -> None:
        """Persist a checkpoint (built by the executor) and emit an event."""
        self.load(task_id)  # ensure known
        if self._store is not None:
            self._store.save(checkpoint)
        self._emit(EventType.TASK_CHECKPOINT_CREATED, task_id=task_id)

    # --- internals ---------------------------------------------------------

    def _emit(self, event_type: EventType, **data: Any) -> None:
        self._events.emit(event_type, self._session_id, **data)

    def _audit_cancel(self, task: Task, reason: str) -> None:
        self._emit(EventType.TASK_CANCELLED, task_id=task.id, reason=reason)
        self._audit_record("task_cancelled", task, allowed=False, reason=reason)
        if self._metrics is not None:
            from kinetic.observability.metrics import METRIC_TASKS_CANCELLED

            self._metrics.inc(METRIC_TASKS_CANCELLED)

    def _audit_record(
        self, action: str, task: Task, *, allowed: bool, reason: str | None = None
    ) -> None:
        if self._audit is None:
            return
        self._audit.record(
            session_id=self._session_id, action=action, allowed=allowed,
            reason=reason, detail={"task_id": task.id, "state": task.state.value},
        )


def default_task_id() -> str:
    from uuid import uuid4

    return uuid4().hex


def workspace_project_id(workspace: str | Path) -> str:
    return str(Path(str(workspace)).resolve())


def _task_duration_seconds(task: Task) -> float | None:
    """Seconds between a task's creation and completion (None if unknown)."""
    from datetime import UTC, datetime

    created = task.created_at
    completed = task.completed_at or task.updated_at
    if not created or not completed:
        return None
    if isinstance(created, str):
        created = datetime.fromisoformat(created)
    if isinstance(completed, str):
        completed = datetime.fromisoformat(completed)
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    if completed.tzinfo is None:
        completed = completed.replace(tzinfo=UTC)
    return max(0.0, (completed - created).total_seconds())
