"""An async in-process event bus.

Publishers emit events; subscribers receive them as an async iterator. The bus
keeps a bounded ring buffer so a subscriber that connects mid-stream can still
receive recent history, but memory does not grow unboundedly.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

from kinetic.events.types import Event, EventType, make_event


class EventBus:
    """A bounded async event bus with replay history."""

    def __init__(self, history_size: int = 256) -> None:
        self._history: deque[Event] = deque(maxlen=history_size)
        self._subscribers: list[asyncio.Queue[Event]] = []

    def publish(self, event: Event) -> None:
        self._history.append(event)
        for q in self._subscribers:
            q.put_nowait(event)

    def emit(self, type_: EventType, session_id: str, **data: Any) -> Event:
        """Convenience: build + publish an event, returning it."""
        event = make_event(type_, session_id, **data)
        self.publish(event)
        return event

    async def subscribe(self) -> EventBusSubscription:
        q: asyncio.Queue[Event] = asyncio.Queue()
        # Replay history so a late subscriber sees context.
        for event in self._history:
            q.put_nowait(event)
        self._subscribers.append(q)
        return EventBusSubscription(self, q)

    def _unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    @property
    def history(self) -> list[Event]:
        return list(self._history)


class EventBusSubscription:
    """An async iterator over bus events; removes itself on close."""

    def __init__(self, bus: EventBus, queue: asyncio.Queue[Event]) -> None:
        self._bus = bus
        self._queue = queue

    def __aiter__(self) -> EventBusSubscription:
        return self

    async def __anext__(self) -> Event:
        return await self._queue.get()

    def close(self) -> None:
        self._bus._unsubscribe(self._queue)
