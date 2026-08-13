"""Phase 7 — EventBus hardening + stress tests.

Verifies bounded subscriber queues, slow-consumer non-blocking, subscriber
failure isolation, bounded payloads, JSON serializability, secret redaction,
and stress behavior.
"""

from __future__ import annotations

import asyncio

from kinetic.events import EventBus, EventType


class TestBoundedQueues:
    async def test_slow_consumer_does_not_block_publisher(self) -> None:
        bus = EventBus(subscriber_queue_size=4, history_size=10)
        sub = await bus.subscribe()
        # Publish many events rapidly without consuming.
        for i in range(100):
            bus.emit(EventType.AGENT_MESSAGE, "s", index=i)
        # Publisher must have returned (test would hang otherwise).
        # Subscriber got at most queue_size recent events; rest dropped.
        received = []
        while not sub._queue.empty():  # noqa: SLF001
            received.append(sub._queue.get_nowait())  # noqa: SLF001
        assert len(received) <= 4
        assert sub.dropped > 0

    async def test_fast_consumer_gets_all(self) -> None:
        bus = EventBus(subscriber_queue_size=100)
        sub = await bus.subscribe()
        for i in range(10):
            bus.emit(EventType.AGENT_MESSAGE, "s", index=i)
        received = []
        for _ in range(10):
            received.append(await asyncio.wait_for(sub.__anext__(), timeout=1))
        assert len(received) == 10

    async def test_bounded_history(self) -> None:
        bus = EventBus(history_size=5)
        for i in range(20):
            bus.emit(EventType.AGENT_MESSAGE, "s", index=i)
        assert len(bus.history) == 5
        # Most recent 5 retained.
        assert bus.history[-1].data["index"] == 19
        assert bus.history[0].data["index"] == 15


class TestSubscriberIsolation:
    async def test_closed_loop_subscriber_dropped(self) -> None:
        bus = EventBus(subscriber_queue_size=10)
        sub = await bus.subscribe()
        # Simulate a closed loop by replacing the queue's put_nowait.
        original = sub._queue.put_nowait  # noqa: SLF001

        def boom(_event):
            raise RuntimeError("event loop closed")

        sub._queue.put_nowait = boom  # type: ignore[method-assign]  # noqa: SLF001
        # Publishing must not raise.
        bus.emit(EventType.AGENT_MESSAGE, "s")
        sub._queue.put_nowait = original  # type: ignore[method-assign]  # noqa: SLF001
        # The subscriber should have been removed.
        assert sub not in bus._subscribers  # noqa: SLF001

    async def test_one_failing_subscriber_does_not_block_others(self) -> None:
        bus = EventBus(subscriber_queue_size=10)
        bad_sub = await bus.subscribe()
        good_sub = await bus.subscribe()

        def boom(_event):
            raise RuntimeError("closed")

        bad_sub._queue.put_nowait = boom  # type: ignore[method-assign]  # noqa: SLF001
        bus.emit(EventType.AGENT_MESSAGE, "s", msg="hi")
        # Good subscriber still got the event.
        ev = await asyncio.wait_for(good_sub.__anext__(), timeout=1)
        assert ev.data["msg"] == "hi"


class TestPayloadSafety:
    def test_oversize_payload_truncated(self) -> None:
        bus = EventBus(max_payload_bytes=200)
        big = "x" * 10_000
        bus.emit(EventType.AGENT_MESSAGE, "s", blob=big)
        event = bus.history[-1]
        import json
        encoded = json.dumps(event.data).encode("utf-8")
        assert len(encoded) <= 200

    def test_non_serializable_replaced(self) -> None:
        bus = EventBus()

        class Custom:
            def __repr__(self) -> str:
                return "<Custom>"

        bus.emit(EventType.AGENT_MESSAGE, "s", obj=Custom())
        event = bus.history[-1]
        import json
        # Must be JSON serializable.
        json.dumps(event.to_dict())

    def test_secret_redacted_in_events(self) -> None:
        bus = EventBus()
        secret = "api_key=sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        bus.emit(EventType.AGENT_MESSAGE, "s", text=secret)
        event = bus.history[-1]
        import json
        raw = json.dumps(event.to_dict())
        assert "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" not in raw
        assert "<redacted>" in raw


class TestStress:
    async def test_many_events(self) -> None:
        bus = EventBus(history_size=1000, subscriber_queue_size=500)
        sub = await bus.subscribe()
        for i in range(5000):
            bus.emit(EventType.AGENT_MESSAGE, "s", index=i)
        assert len(bus.history) == 1000
        # Subscriber queue bounded.
        assert sub._queue.qsize() <= 500  # noqa: SLF001

    async def test_concurrent_publishers(self) -> None:
        bus = EventBus(history_size=500, subscriber_queue_size=200)
        await bus.subscribe()

        async def publish(count: int) -> None:
            for i in range(count):
                bus.emit(EventType.AGENT_MESSAGE, "s", src=count, idx=i)

        await asyncio.gather(*(publish(100) for _ in range(5)))
        assert len(bus.history) == 500

    async def test_replay_drops_on_overflow(self) -> None:
        bus = EventBus(history_size=100, subscriber_queue_size=10)
        for i in range(100):
            bus.emit(EventType.AGENT_MESSAGE, "s", index=i)
        sub = await bus.subscribe()
        # Replay fills the queue (10) and drops the rest.
        assert sub._queue.qsize() <= 10  # noqa: SLF001
        assert sub.dropped > 0

    async def test_cancelled_consumer_cleanup(self) -> None:
        bus = EventBus()
        sub = await bus.subscribe()
        bus.emit(EventType.AGENT_MESSAGE, "s")
        assert bus.subscriber_count == 1
        sub.close()
        assert bus.subscriber_count == 0
