"""Interactive (human-in-the-loop) tool approval registry.

When a session opts into interactive approval, the agent adapter's
``can_use_tool`` gate asks this registry to await a human decision (allow /
deny) for each tool call that the static policy would otherwise allow
automatically. Denied tools and unknown tools are still denied immediately by
the static policy — interactive approval only adds a human checkpoint on top of
the existing automatic gate, it never relaxes it.

The registry is in-memory only. Pending requests are bounded by a timeout so a
forgotten approval cannot stall the agent loop forever. No secrets are stored:
tool inputs are masked by the web serialization layer before they reach the
browser, and the registry itself only holds references, not copies of
credentials.

This module performs NO subprocess, NO filesystem mutation, and NO model calls.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any

from events import EventBus, EventType


@dataclass
class PendingApproval:
    """A single outstanding approval request for one tool call."""

    request_id: str
    task_id: str
    tool: str
    tool_input: dict[str, Any]
    reason: str = ""
    future: asyncio.Future[bool] = field(default_factory=lambda: _new_future())
    created_at: float = field(default_factory=lambda: _loop_time())

    @property
    def resolved(self) -> bool:
        return self.future.done()


def _new_future() -> asyncio.Future[bool]:
    loop = asyncio.get_event_loop()
    return loop.create_future()


def _loop_time() -> float:
    try:
        return asyncio.get_running_loop().time()
    except RuntimeError:
        import time

        return time.monotonic()


class PendingApprovalRegistry:
    """Tracks outstanding approval requests per task.

    The registry is the bridge between the synchronous permission gate inside the
    agent loop and the asynchronous HTTP API that resolves approvals. It is safe
    to share across tasks; lookups never block the caller except via the
    futures the adapter awaits.
    """

    def __init__(self, events: EventBus | None = None) -> None:
        self._pending: dict[str, PendingApproval] = {}
        self._events = events

    def request(
        self,
        *,
        task_id: str,
        tool: str,
        tool_input: dict[str, Any],
        reason: str = "",
    ) -> PendingApproval:
        """Register a new approval request and emit a ``permission_requested`` event."""
        req = PendingApproval(
            request_id=uuid.uuid4().hex,
            task_id=task_id,
            tool=tool,
            tool_input=dict(tool_input),
            reason=reason,
        )
        self._pending[req.request_id] = req
        if self._events is not None:
            self._events.emit(
                EventType.PERMISSION_REQUESTED,
                task_id,
                request_id=req.request_id,
                tool=tool,
                reason=reason,
            )
        return req

    async def await_decision(self, req: PendingApproval, *, timeout: float) -> bool:
        """Wait for a human decision. Returns True=allow, False=deny/timeout."""
        try:
            return await asyncio.wait_for(req.future, timeout=timeout)
        except TimeoutError:
            self._resolve(req, False, decision="timeout")
            return False

    def resolve(self, request_id: str, *, allow: bool, decision: str = "user") -> bool:
        """Resolve a pending request from the API. Returns False if unknown/done."""
        req = self._pending.get(request_id)
        if req is None or req.resolved:
            return False
        self._resolve(req, allow, decision=decision)
        return True

    def _resolve(self, req: PendingApproval, allow: bool, *, decision: str) -> None:
        if req.resolved:
            return
        if not req.future.cancelled():
            req.future.set_result(allow)
        if self._events is not None:
            self._events.emit(
                EventType.PERMISSION_RESOLVED,
                req.task_id,
                request_id=req.request_id,
                tool=req.tool,
                allowed=allow,
                decision=decision,
            )

    def list_pending(self, task_id: str | None = None) -> list[PendingApproval]:
        out = [r for r in self._pending.values() if not r.resolved]
        if task_id is not None:
            out = [r for r in out if r.task_id == task_id]
        return out

    def get(self, request_id: str) -> PendingApproval | None:
        return self._pending.get(request_id)

    def cancel_all(self, task_id: str | None = None) -> None:
        """Deny (and resolve) all pending requests — e.g. on task cancel/shutdown."""
        for req in list(self._pending.values()):
            if task_id is not None and req.task_id != task_id:
                continue
            self._resolve(req, False, decision="cancelled")


__all__ = ["PendingApproval", "PendingApprovalRegistry"]
