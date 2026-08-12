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
