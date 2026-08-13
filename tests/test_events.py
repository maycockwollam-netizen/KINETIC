"""Unit tests for the event bus."""

from __future__ import annotations

import pytest

from kinetic.events import EventBus, EventType


@pytest.mark.timeout(5)
async def test_publish_and_subscribe_replays_history():
    bus = EventBus(history_size=8)
    bus.emit(EventType.AGENT_STARTED, "s1", prompt="hi")
    sub = await bus.subscribe()
    bus.emit(EventType.AGENT_MESSAGE, "s1", text="world")

    first = await sub.__anext__()
    assert first.type is EventType.AGENT_STARTED  # replayed history first
    second = await sub.__anext__()
    assert second.type is EventType.AGENT_MESSAGE
    assert second.data["text"] == "world"


@pytest.mark.timeout(5)
async def test_history_is_bounded():
    bus = EventBus(history_size=2)
    for i in range(5):
        bus.emit(EventType.AGENT_MESSAGE, "s", n=i)
    assert len(bus.history) == 2
    assert bus.history[-1].data["n"] == 4


def test_event_to_dict_is_serializable():
    bus = EventBus()
    ev = bus.emit(EventType.TOOL_STARTED, "s1", tool="run_command")
    d = ev.to_dict()
    assert d["type"] == "tool_started"
    assert d["session_id"] == "s1"
    assert d["data"]["tool"] == "run_command"
    assert isinstance(d["timestamp"], str)


@pytest.mark.timeout(5)
async def test_subscription_close_removes_subscriber():
    bus = EventBus()
    sub = await bus.subscribe()
    sub.close()
    bus.emit(EventType.AGENT_STARTED, "s1")
    assert len(bus.history) == 1


# --- Phase 4: memory/context events --------------------------------------


def test_memory_event_types_exist():
    assert EventType.MEMORY_CREATED.value == "memory_created"
    assert EventType.MEMORY_UPDATED.value == "memory_updated"
    assert EventType.MEMORY_DELETED.value == "memory_deleted"
    assert EventType.MEMORY_INVALIDATED.value == "memory_invalidated"
    assert EventType.MEMORY_RETRIEVED.value == "memory_retrieved"
    assert EventType.MEMORY_CONSOLIDATED.value == "memory_consolidated"
    assert EventType.CONTEXT_BUILT.value == "context_built"
    assert EventType.CONTEXT_BUDGET_EXCEEDED.value == "context_budget_exceeded"


def test_memory_events_round_trip():
    bus = EventBus()
    ev = bus.emit(EventType.MEMORY_CREATED, "s1", memory_id="m1", memory_type="project")
    d = ev.to_dict()
    assert d["type"] == "memory_created"
    assert d["data"]["memory_id"] == "m1"
    assert d["data"]["memory_type"] == "project"


def test_context_events_round_trip():
    bus = EventBus()
    bus.emit(EventType.CONTEXT_BUILT, "s1", memory_count=3)
    bus.emit(EventType.CONTEXT_BUDGET_EXCEEDED, "s1", omissions=2)
    types = [e.type for e in bus.history]
    assert EventType.CONTEXT_BUILT in types
    assert EventType.CONTEXT_BUDGET_EXCEEDED in types
