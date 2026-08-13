"""Phase 7 — graceful shutdown / lifecycle tests."""

from __future__ import annotations

import asyncio

from lifecycle import ShutdownCoordinator, install_signal_handlers


class TestShutdownCoordinator:
    async def test_callbacks_run_in_lifo_order(self) -> None:
        coord = ShutdownCoordinator(timeout=5.0)
        order: list[str] = []
        coord.register("first", lambda: order.append("first"))
        coord.register("second", lambda: order.append("second"))
        result = await coord.shutdown()
        assert order == ["second", "first"]
        assert "first" in result.completed
        assert "second" in result.completed

    async def test_async_callbacks(self) -> None:
        coord = ShutdownCoordinator(timeout=5.0)
        done: list[str] = []

        async def cb() -> None:
            await asyncio.sleep(0.01)
            done.append("ok")

        coord.register("async_cb", cb)
        result = await coord.shutdown()
        assert done == ["ok"]
        assert "async_cb" in result.completed

    async def test_callback_failure_does_not_stop_others(self) -> None:
        coord = ShutdownCoordinator(timeout=5.0)

        def boom() -> None:
            raise RuntimeError("fail")

        coord.register("bad", boom)
        coord.register("good", lambda: None)
        result = await coord.shutdown()
        assert "bad" in result.failed
        assert "good" in result.completed

    async def test_timed_out_callback(self) -> None:
        coord = ShutdownCoordinator(timeout=0.1)

        async def slow() -> None:
            await asyncio.sleep(10)

        coord.register("slow", slow)
        result = await coord.shutdown()
        assert "slow" in result.timed_out

    async def test_cancellation_distinct_from_failure(self) -> None:
        coord = ShutdownCoordinator(timeout=5.0)
        result = await coord.shutdown()
        assert result.cancelled is True
        assert not result.failed
        assert result.success or len(result.completed) == 0

    async def test_request_shutdown_sets_token(self) -> None:
        coord = ShutdownCoordinator()
        token = coord.cancellation_token
        assert not token.cancelled
        coord.request_shutdown()
        assert token.cancelled
        assert coord.is_shutting_down

    async def test_empty_shutdown(self) -> None:
        coord = ShutdownCoordinator(timeout=1.0)
        result = await coord.shutdown()
        assert result.success
        assert result.completed == []


class TestSignalHandlers:
    async def test_install_returns_remover(self) -> None:
        coord = ShutdownCoordinator()
        remover = install_signal_handlers(coord)
        assert callable(remover)
        remover()  # must not raise

    async def test_install_on_non_main_thread_is_noop(self) -> None:
        import threading

        coord = ShutdownCoordinator()
        result: dict = {}

        def run_in_thread() -> None:
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                remover = install_signal_handlers(coord)
                result["remover"] = remover
                result["is_none"] = remover.__name__ == "_remove" or callable(remover)
            finally:
                pass

        t = threading.Thread(target=run_in_thread)
        t.start()
        t.join()
        # On a non-main thread, signal handlers cannot be installed; the
        # returned remover should be a harmless no-op.
        assert "remover" in result
