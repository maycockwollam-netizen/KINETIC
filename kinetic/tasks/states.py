"""Task lifecycle state machine.

A task moves through explicit states. Invalid transitions fail safely by
raising :class:`~kinetic.errors.TaskStateError` rather than silently mutating
state::

    CREATED -> CONTEXT_READY -> PLANNING -> PLAN_READY -> EXECUTING
        -> VERIFYING -> COMPLETED
                 |               |           |            |
                 +---------------+-----------+------------+--> RECOVERING
                                                                -> RECOVERING
    (any non-terminal state) -> FAILED | CANCELLED

``COMPLETED``, ``FAILED`` and ``CANCELLED`` are terminal. ``CANCELLED`` is not
reachable from a terminal state and is never reinterpreted as failure.

The state machine is the single source of truth for task progression; the
:class:`~kinetic.tasks.manager.TaskManager` is the only writer.
"""

from __future__ import annotations

from enum import StrEnum

from kinetic.errors import TaskStateError


class TaskState(StrEnum):
    CREATED = "created"
    CONTEXT_READY = "context_ready"
    PLANNING = "planning"
    PLAN_READY = "plan_ready"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    RECOVERING = "recovering"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATES: frozenset[TaskState] = frozenset(
    {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED}
)

# Valid forward transitions. Keys are the from-state; values are the set of
# states reachable in one step. Anything not listed raises.
_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.CREATED: frozenset(
        {TaskState.CONTEXT_READY, TaskState.PLANNING, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.CONTEXT_READY: frozenset(
        {TaskState.PLANNING, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.PLANNING: frozenset(
        {TaskState.PLAN_READY, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.PLAN_READY: frozenset(
        {TaskState.EXECUTING, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.EXECUTING: frozenset(
        {TaskState.VERIFYING, TaskState.RECOVERING, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.VERIFYING: frozenset(
        {TaskState.COMPLETED, TaskState.RECOVERING, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.RECOVERING: frozenset(
        {TaskState.EXECUTING, TaskState.PLAN_READY, TaskState.PLANNING, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.COMPLETED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELLED: frozenset(),
}

# States from which re-planning is allowed (the recovery path re-plans).
REPLAN_SOURCE_STATES: frozenset[TaskState] = frozenset({TaskState.RECOVERING})


def transition_allowed(from_: TaskState, to: TaskState) -> bool:
    return to in _TRANSITIONS.get(from_, frozenset())


def require_transition(from_: TaskState, to: TaskState) -> None:
    """Raise :class:`TaskStateError` if ``from_ -> to`` is not a valid step."""
    if not transition_allowed(from_, to):
        raise TaskStateError(from_.value, to.value)


def is_terminal(state: TaskState) -> bool:
    return state in TERMINAL_STATES
