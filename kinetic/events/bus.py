"""An async in-process event bus.

Publishers emit events; subscribers receive them as an async iterator. The bus
keeps a bounded ring buffer so a subscriber that connects mid-stream can still
receive recent history, but memory does not grow unboundedly.

Hardening (Phase 7):

* **bounded subscriber queues** — each subscriber has a ``maxsize`` queue. A slow
  consumer that cannot keep up does NOT block the publisher: a full queue drops
  the oldest event (the publisher calls ``put_nowait`` and catches ``QueueFull``).
* **subscriber failure isolation** — a put that raises (e.g. a closed event loop)
  is caught and the subscriber is dropped, so one bad subscriber never crashes
  the producer.
* **bounded payloads** — event ``data`` is size-capped; an oversize payload is
  truncated (with a marker) so a runaway tool cannot grow memory via events.
* **JSON serializable** — events are validated to be JSON-serializable at emit
  time (a non-serializable payload is replaced with a string fallback) so a
  consumer never crashes on deserialization.
* **no secrets** — the same secret detector used elsewhere masks credential-like
  values in event data before publication.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections import deque
from typing import Any

from kinetic.events.types import Event, EventType, make_event

#: Maximum number of events retained for replay (ring buffer).
DEFAULT_HISTORY_SIZE = 256
#: Maximum queue depth per subscriber before the oldest events are dropped.
DEFAULT_SUBSCRIBER_QUEUE_SIZE = 512
#: Maximum serialized size (bytes) of an event's ``data`` payload.
DEFAULT_MAX_PAYLOAD_BYTES = 65_536
#: Marker inserted when a payload is truncated.
_TRUNCATED_MARKER = "__truncated__"


class EventBus:
    """A bounded async event bus with replay history."""

    def __init__(
        self,
        *,
        history_size: int = DEFAULT_HISTORY_SIZE,
        subscriber_queue_size: int = DEFAULT_SUBSCRIBER_QUEUE_SIZE,
        max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
    ) -> None:
        self._history: deque[Event] = deque(maxlen=history_size)
        self._subscribers: list[EventBusSubscription] = []
        self._subscriber_queue_size = subscriber_queue_size
        self._max_payload_bytes = max_payload_bytes

    def publish(self, event: Event) -> None:
        """Publish an event to all subscribers (best-effort, non-blocking).

        A slow subscriber whose queue is full has its oldest event dropped
        rather than blocking the publisher. A subscriber whose event loop is
        closed or errored is dropped entirely. The publisher never raises due
        to a subscriber.
        """
        event = self._sanitize(event)
        self._history.append(event)
        for sub in list(self._subscribers):
            sub.deliver(event)

    def emit(self, type_: EventType, session_id: str, **data: Any) -> Event:
        """Convenience: build + publish an event, returning it."""
        event = make_event(type_, session_id, **data)
        self.publish(event)
        return event

    async def subscribe(self) -> EventBusSubscription:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._subscriber_queue_size)
        # Replay history so a late subscriber sees context.
        dropped = 0
        for event in self._history:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dropped += 1
        sub = EventBusSubscription(self, q, dropped_on_replay=dropped)
        self._subscribers.append(sub)
        return sub

    def _unsubscribe(self, sub: EventBusSubscription) -> None:
        if sub in self._subscribers:
            self._subscribers.remove(sub)

    @property
    def history(self) -> list[Event]:
        return list(self._history)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    # --- payload safety ----------------------------------------------------

    def _sanitize(self, event: Event) -> Event:
        """Cap payload size, ensure JSON-serializable, and mask secrets."""
        data = _redact_secrets(event.data)
        data = _ensure_json_serializable(data)
        data = _cap_payload(data, self._max_payload_bytes)
        if data is event.data:
            return event
        return Event(type=event.type, session_id=event.session_id,
                     timestamp=event.timestamp, data=data)


def _redact_secrets(data: dict[str, Any]) -> dict[str, Any]:
    """Mask credential-like values in event data."""
    from kinetic.memory.metadata import DEFAULT_SECRET_DETECTOR

    def redact(value: Any) -> Any:
        if isinstance(value, str):
            result = value
            for m in DEFAULT_SECRET_DETECTOR.detect(value):
                if m.original and m.original in result:
                    result = result.replace(m.original, "<redacted>")
            return result
        if isinstance(value, dict):
            return {k: redact(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [redact(v) for v in value]
        return value

    return redact(data)


def _ensure_json_serializable(data: dict[str, Any]) -> dict[str, Any]:
    """Replace any non-JSON-serializable value with its string form."""
    out: dict[str, Any] = {}
    for k, v in data.items():
        try:
            json.dumps(v)
            out[k] = v
        except (TypeError, ValueError):
            out[k] = repr(v)
    return out


def _cap_payload(data: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    """Truncate the serialized payload to ``max_bytes`` if needed."""
    try:
        encoded = json.dumps(data, default=str)
    except (TypeError, ValueError):
        encoded = json.dumps({k: repr(v) for k, v in data.items()})
    if len(encoded.encode("utf-8")) <= max_bytes:
        return data
    # Drop the largest keys until it fits, then mark truncated.
    capped = dict(data)
    while capped:
        # Remove the last-added key (heuristic: often the bulkiest).
        key = list(capped)[-1]
        capped.pop(key)
        capped[_TRUNCATED_MARKER] = True
        try:
            encoded = json.dumps(capped, default=str)
        except (TypeError, ValueError):
            continue
        if len(encoded.encode("utf-8")) <= max_bytes:
            return capped
    return {_TRUNCATED_MARKER: True}


class EventBusSubscription:
    """An async iterator over bus events; removes itself on close."""

    def __init__(
        self,
        bus: EventBus,
        queue: asyncio.Queue[Event],
        *,
        dropped_on_replay: int = 0,
    ) -> None:
        self._bus = bus
        self._queue = queue
        self._dropped = dropped_on_replay
        self._total_dropped = dropped_on_replay

    def __aiter__(self) -> EventBusSubscription:
        return self

    async def __anext__(self) -> Event:
        return await self._queue.get()

    def deliver(self, event: Event) -> None:
        """Non-blocking delivery; drops oldest on full queue, drops sub on error."""
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            # Drop the oldest to make room for the newest.
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
            try:
                self._queue.put_nowait(event)
                self._dropped += 1
                self._total_dropped += 1
            except asyncio.QueueFull:
                pass
        except RuntimeError:
            # Event loop closed — drop this subscriber silently.
            self._bus._unsubscribe(self)  # noqa: SLF001

    @property
    def dropped(self) -> int:
        """Number of events dropped because this subscriber was too slow."""
        return self._dropped

    def close(self) -> None:
        self._bus._unsubscribe(self)  # noqa: SLF001
