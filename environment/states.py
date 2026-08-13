"""Environment lifecycle state machine.

A sandbox moves through explicit states. Invalid transitions fail safely by
raising ``EnvironmentStateError`` rather than silently mutating state::

    CREATING -> READY -> RUNNING -> STOPPING -> STOPPED -> DESTROYED
                 |          |          |          |
                 +----------+----------+----------+--> FAILED
                 (any non-DESTROYED state may go FAILED)

``DESTROYED`` and ``FAILED`` are terminal (FAILED may still be destroyed for
cleanup). ``CREATING`` is the only valid entry state.
"""

from __future__ import annotations

from enum import StrEnum

from errors import EnvironmentStateError


class EnvironmentState(StrEnum):
    CREATING = "creating"
    READY = "ready"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
    DESTROYED = "destroyed"


# Valid forward transitions. Keys are the from-state; values are the set of
# states reachable in one step. Anything not listed raises.
_TRANSITIONS: dict[EnvironmentState, frozenset[EnvironmentState]] = {
    EnvironmentState.CREATING: frozenset(
        {EnvironmentState.READY, EnvironmentState.FAILED}
    ),
    EnvironmentState.READY: frozenset(
        {EnvironmentState.RUNNING, EnvironmentState.STOPPED, EnvironmentState.FAILED}
    ),
    EnvironmentState.RUNNING: frozenset(
        {EnvironmentState.STOPPING, EnvironmentState.FAILED}
    ),
    EnvironmentState.STOPPING: frozenset(
        {EnvironmentState.STOPPED, EnvironmentState.FAILED}
    ),
    EnvironmentState.STOPPED: frozenset(
        {EnvironmentState.READY, EnvironmentState.DESTROYED, EnvironmentState.FAILED}
    ),
    EnvironmentState.FAILED: frozenset({EnvironmentState.DESTROYED}),
    EnvironmentState.DESTROYED: frozenset(),
}

TERMINAL_STATES: frozenset[EnvironmentState] = frozenset(
    {EnvironmentState.DESTROYED}
)


def transition_allowed(from_: EnvironmentState, to: EnvironmentState) -> bool:
    return to in _TRANSITIONS.get(from_, frozenset())


def require_transition(from_: EnvironmentState, to: EnvironmentState) -> None:
    """Raise ``EnvironmentStateError`` if ``from_ -> to`` is not a valid step."""
    if not transition_allowed(from_, to):
        raise EnvironmentStateError(
            f"invalid environment state transition: {from_.value} -> {to.value}"
        )
