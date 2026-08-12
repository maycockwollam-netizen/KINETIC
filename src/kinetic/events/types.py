"""Structured, serializable runtime events.

The event bus is the single stream the Web UI (Phase 6) will consume. Events are
plain dataclasses that serialize to JSON, so consumers never depend on internal
agent implementation details.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class EventType(StrEnum):
    AGENT_STARTED = "agent_started"
    AGENT_MESSAGE = "agent_message"
    AGENT_ERROR = "agent_error"
    TOOL_STARTED = "tool_started"
    TOOL_OUTPUT = "tool_output"
    TOOL_FINISHED = "tool_finished"
    COMMAND_STARTED = "command_started"
    COMMAND_FINISHED = "command_finished"
    FILE_CHANGED = "file_changed"
    TEST_STARTED = "test_started"
    TEST_FINISHED = "test_finished"
    TASK_COMPLETED = "task_completed"
    SESSION_COMPACTED = "session_compacted"


@dataclass
class Event:
    """A single runtime event."""

    type: EventType
    session_id: str
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["type"] = self.type.value
        return d


def make_event(type_: EventType, session_id: str, **data: Any) -> Event:
    """Construct an event with typed payload data."""
    return Event(type=type_, session_id=session_id, data=dict(data))
