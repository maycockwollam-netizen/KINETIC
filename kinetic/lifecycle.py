"""Graceful shutdown coordination for KINETIC.

Brings every long-lived resource under one bounded-timeout shutdown path:

* CLI interruption (SIGINT/SIGTERM) -> cooperative cancellation
* task cancellation (distinct from failure)
* model/adapter interruption
* environment stop + destroy
* event subscription cleanup
* checkpoint persistence

Design:

* :class:`ShutdownCoordinator` registers named cleanup callbacks (async or
  sync). :meth:`shutdown` runs them all within a bounded timeout. A callback
  that overruns is abandoned (its task is cancelled) — resources are cleaned
  exactly-once best-effort; a hung cleanup never hangs the process.
* Cancellation remains distinct from failure: a shutdown triggered by Ctrl-C
  produces a ``cancelled`` outcome, never a ``failed`` one. The coordinator
  does not interpret cancellation as an error.
* :func:`install_signal_handlers` wires SIGINT/SIGTERM to a
  :class:`~kinetic.tools.terminal.CancellationToken` so the running task can
  observe interruption cooperatively. Signal handlers are only installed when
  explicitly requested (never as an import side effect).

This module performs NO subprocess, NO filesystem mutation outside the
registered callbacks, and NO model calls. It only coordinates cancellation and
runs the cleanup callbacks callers registered.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from kinetic.tools.terminal import CancellationToken

logger = logging.getLogger(__name__)

CleanupCallback = Callable[..., Any]


@dataclass
class _Registered:
    name: str
    callback: CleanupCallback
    is_async: bool


@dataclass
class ShutdownResult:
    """Outcome of a coordinated shutdown."""

    completed: list[str] = field(default_factory=list)
    timed_out: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    cancelled: bool = False

    @property
    def success(self) -> bool:
        return not self.timed_out and not self.failed


class ShutdownCoordinator:
    """Runs registered cleanup callbacks within a bounded timeout.

    Callbacks are invoked in reverse registration order (LIFO: last-registered
    resource is torn down first, mirroring ``try/finally`` nesting). Each
    callback gets a slice of the total timeout; a callback that does not finish
    in time is cancelled. A callback raising is recorded but does not stop the
    remaining cleanups (failure containment).
    """

    def __init__(self, *, timeout: float = 30.0) -> None:
        self._timeout = timeout
        self._callbacks: list[_Registered] = []
        self._token = CancellationToken()
        self._shutting_down = False

    @property
    def cancellation_token(self) -> CancellationToken:
        return self._token

    @property
    def is_shutting_down(self) -> bool:
        return self._shutting_down

    def register(self, name: str, callback: CleanupCallback) -> None:
        """Register a cleanup callback (async or sync)."""
        is_async = asyncio.iscoroutinefunction(callback)
        self._callbacks.append(_Registered(name=name, callback=callback, is_async=is_async))

    def request_shutdown(self, *, reason: str = "shutdown requested") -> None:
        """Signal cooperative cancellation (does not run callbacks)."""
        self._shutting_down = True
        self._token.cancel()

    async def shutdown(self, *, reason: str = "shutdown") -> ShutdownResult:
        """Run all registered cleanups within the bounded timeout.

        Cancellation is NOT failure: the result records ``cancelled=True`` only
        as context, never as an error. A cleanup that raises is recorded in
        ``failed``; one that overruns its time slice is in ``timed_out``.
        """
        self._shutting_down = True
        self._token.cancel()
        result = ShutdownResult(cancelled=True)
        # LIFO order.
        per_callback = self._timeout / max(len(self._callbacks), 1)
        for entry in reversed(self._callbacks):
            await self._run_one(entry, per_callback, result)
        return result

    async def _run_one(self, entry: _Registered, timeout: float, result: ShutdownResult) -> None:
        try:
            if entry.is_async:
                await asyncio.wait_for(entry.callback(), timeout=timeout)
            else:
                await asyncio.wait_for(asyncio.to_thread(entry.callback), timeout=timeout)
            result.completed.append(entry.name)
        except TimeoutError:
            result.timed_out.append(entry.name)
            logger.warning("cleanup %s timed out", entry.name)
        except Exception as exc:  # noqa: BLE001 - record, continue
            result.failed.append(entry.name)
            logger.warning("cleanup %s failed: %s", entry.name, exc)


def install_signal_handlers(
    coordinator: ShutdownCoordinator,
    *,
    loop: asyncio.AbstractEventLoop | None = None,
) -> Callable[[], None]:
    """Wire SIGINT/SIGTERM to the coordinator's cancellation token.

    Returns a callable that removes the handlers (call it on clean teardown).
    Only installs on the main thread (signal handlers cannot be set on other
    threads); on platforms without SIGTERM it is skipped gracefully.
    """
    import threading

    if threading.current_thread() is not threading.main_thread():
        return lambda: None
    loop = loop or asyncio.get_running_loop()

    def _handler(*_args: object) -> None:
        coordinator.request_shutdown(reason="signal")

    signals = [signal.SIGINT]
    with suppress(AttributeError):
        signals.append(signal.SIGTERM)
    for sig in signals:
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, _handler)

    def _remove() -> None:
        for sig in signals:
            with suppress(NotImplementedError, ValueError):
                loop.remove_signal_handler(sig)

    return _remove


__all__ = [
    "ShutdownCoordinator",
    "ShutdownResult",
    "install_signal_handlers",
]
