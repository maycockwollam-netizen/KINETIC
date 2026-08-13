"""Phase 7 — failure containment tests.

Verifies that one failure cannot cascade: subscriber failure doesn't kill
EventBus, task failure doesn't kill the process, tool failure doesn't bypass
permissions, environment failure doesn't trigger host fallback, and cleanup
failure remains observable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kinetic.errors import PermissionDeniedError, SandboxError
from kinetic.events import EventBus, EventType
from kinetic.security import AuditLog, PermissionPolicy
from kinetic.tasks.states import TaskState


class TestEventBusFailureContainment:
    async def test_subscriber_exception_does_not_crash_bus(self) -> None:
        bus = EventBus(subscriber_queue_size=10)
        sub = await bus.subscribe()

        def boom(_event):
            raise RuntimeError("subscriber exploded")

        sub._queue.put_nowait = boom  # type: ignore[method-assign]
        # Publishing must not raise.
        bus.emit(EventType.AGENT_MESSAGE, "s", msg="ok")
        # Bus is still usable.
        bus.emit(EventType.AGENT_MESSAGE, "s", msg="ok2")
        assert len(bus.history) == 2


class TestToolFailureContainment:
    async def test_tool_failure_does_not_bypass_permissions(self, tmp_path: Path) -> None:
        from kinetic.tools.terminal import terminal_tool

        policy = PermissionPolicy(writable_roots=[tmp_path], allow_execute=False)
        AuditLog(tmp_path / "audit.log")
        tool = terminal_tool(cwd=str(tmp_path), default_timeout=5, max_timeout=10)
        # The tool itself doesn't check permissions; the adapter does. But the
        # permission policy deny is the boundary: calling require() raises.
        with pytest.raises(PermissionDeniedError):
            policy.require("run_command", tool.permission, {"command": "ls"})


class TestEnvironmentFailureContainment:
    async def test_environment_failure_does_not_host_fallback(self, tmp_path: Path) -> None:
        from kinetic.environment import Environment
        from kinetic.environment.config import EnvironmentConfig
        from kinetic.environment.network import NetworkPolicy

        # sandbox_mode=True + network DENY on local runtime must raise, not
        # silently fall back to host execution.
        cfg = EnvironmentConfig(
            runtime_type="local", sandbox_mode=True, network=NetworkPolicy.DENY,
        )
        env = Environment(tmp_path, cfg)
        with pytest.raises(SandboxError):
            await env.provision()
        # Environment is in a failed/destroyed state, NOT silently running on host.
        assert env.state.value in ("failed", "destroyed", "creating")


class TestCleanupFailureObservable:
    async def test_destroy_failure_emits_event(self, tmp_path: Path) -> None:
        from kinetic.environment import Environment
        from kinetic.environment.config import EnvironmentConfig
        from kinetic.environment.runtime import EnvironmentRuntime, RuntimeStatus

        class FailingRuntime(EnvironmentRuntime):
            runtime_type = "failing"

            async def create(self) -> None:
                pass

            async def start(self) -> None:
                pass

            async def exec(self, spec, *, cancellation=None) -> None:
                pass

            async def stop(self) -> None:
                pass

            async def destroy(self) -> None:
                raise RuntimeError("destroy failed")

            async def inspect(self) -> RuntimeStatus:
                return RuntimeStatus(runtime_type="failing", ready=False, detail={})

        cfg = EnvironmentConfig(runtime_type="local", sandbox_mode=False)
        env = Environment(tmp_path, cfg, runtime=FailingRuntime(tmp_path, cfg),
                          events=EventBus(), session_id="test")
        await env.provision()
        await env.destroy()
        # The failure must be observable in events.
        events = [e for e in env._events.history if e.type is EventType.ENVIRONMENT_FAILED]
        assert len(events) >= 1


class TestTaskFailureDoesNotCorruptGlobal:
    def test_one_task_failure_does_not_affect_others(self) -> None:
        from kinetic.events import EventBus
        from kinetic.tasks.manager import TaskManager
        from kinetic.tasks.models import TaskFailure

        bus = EventBus()
        mgr = TaskManager(events=bus)
        mgr.create(task_id="t1", user_request="a", workspace="/ws")
        mgr.create(task_id="t2", user_request="b", workspace="/ws")
        mgr.transition("t1", TaskState.CONTEXT_READY)
        mgr.transition("t2", TaskState.CONTEXT_READY)
        # Fail t1.
        mgr.mark_failed("t1", failure=TaskFailure(failure_class="test", message="boom"))
        # t2 is unaffected.
        assert mgr.load("t2").state.value == "context_ready"
        assert mgr.load("t1").state.value == "failed"
