"""Unit tests for the Environment abstraction: lifecycle, runtime, exec, events."""

from __future__ import annotations

from pathlib import Path

import pytest

from environment import (
    Environment,
    EnvironmentConfig,
    EnvironmentState,
    NetworkPolicy,
    ProcessSpec,
)
from environment.local import LocalRuntime
from environment.runtime import EnvironmentRuntime, RuntimeStatus
from errors import EnvironmentStateError, SandboxError
from events import EventBus, EventType


def _local_config(**kw) -> EnvironmentConfig:
    base = dict(runtime_type="local", sandbox_mode=False, network=NetworkPolicy.ALLOW)
    base.update(kw)
    return EnvironmentConfig(**base)


async def test_environment_lifecycle_local(tmp_path: Path):
    env = Environment.create(tmp_path / "ws", _local_config(), session_id="s1")
    assert env.state is EnvironmentState.CREATING
    await env.provision()
    assert env.state is EnvironmentState.RUNNING
    assert env.is_running()
    res = await env.exec(ProcessSpec(command="echo hi", timeout=10))
    assert res.succeeded
    assert "hi" in res.stdout
    await env.stop()
    assert env.state is EnvironmentState.STOPPED
    await env.destroy()
    assert env.state is EnvironmentState.DESTROYED


async def test_environment_exec_requires_running(tmp_path: Path):
    env = Environment.create(tmp_path / "ws", _local_config())
    # Not provisioned yet -> exec must fail (state guard).
    with pytest.raises(EnvironmentStateError):
        await env.exec(ProcessSpec(command="echo hi", timeout=5))


async def test_environment_destroy_is_idempotent(tmp_path: Path):
    env = Environment.create(tmp_path / "ws", _local_config())
    await env.provision()
    await env.destroy()
    await env.destroy()  # second call is a no-op
    assert env.state is EnvironmentState.DESTROYED


async def test_environment_stop_from_non_running_is_noop(tmp_path: Path):
    env = Environment.create(tmp_path / "ws", _local_config())
    await env.provision()
    await env.stop()
    assert env.state is EnvironmentState.STOPPED
    # stop again should not raise (already stopped)
    await env.stop()
    assert env.state is EnvironmentState.STOPPED


async def test_environment_exec_failure_sets_state(tmp_path: Path):
    env = Environment.create(tmp_path / "ws", _local_config())
    await env.provision()
    res = await env.exec(ProcessSpec(command="exit 7", timeout=10))
    assert res.exit_code == 7
    assert not res.succeeded
    # environment stays running even if a process fails
    assert env.state is EnvironmentState.RUNNING


async def test_environment_timeout_event(tmp_path: Path):
    bus = EventBus()
    env = Environment.create(tmp_path / "ws", _local_config(), events=bus, session_id="s1")
    await env.provision()
    res = await env.exec(ProcessSpec(command="sleep 5", timeout=0.3))
    assert res.timed_out
    assert res.state is __import__("environment.process", fromlist=["ProcessState"]).ProcessState.TIMED_OUT
    assert any(e.type is EventType.PROCESS_TIMEOUT for e in bus.history)


async def test_environment_emits_lifecycle_events(tmp_path: Path):
    bus = EventBus()
    env = Environment.create(tmp_path / "ws", _local_config(), events=bus, session_id="s1")
    await env.provision()
    await env.exec(ProcessSpec(command="echo ok", timeout=5))
    await env.stop()
    await env.destroy()
    types = [e.type for e in bus.history]
    assert EventType.ENVIRONMENT_CREATED in types
    assert EventType.ENVIRONMENT_STARTED in types
    assert EventType.PROCESS_STARTED in types
    assert EventType.PROCESS_FINISHED in types
    assert EventType.ENVIRONMENT_STOPPED in types
    assert EventType.ENVIRONMENT_DESTROYED in types


async def test_environment_audit_records_exec(tmp_path: Path):
    from security import AuditLog

    audit = AuditLog(tmp_path / "audit.log")
    env = Environment.create(tmp_path / "ws", _local_config(), audit=audit, session_id="s1")
    await env.provision()
    await env.exec(ProcessSpec(command="echo audited", timeout=5))
    entries = audit.read()
    assert any(e["action"] == "process_exec" for e in entries)


# --- runtime abstraction ------------------------------------------------------


async def test_local_runtime_probe_always_true():
    assert await LocalRuntime.probe() is True


async def test_local_runtime_exec_workspace_bound(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "marker.txt").write_text("here")
    rt = LocalRuntime(ws, _local_config())
    await rt.create()
    await rt.start()
    res = await rt.exec(ProcessSpec(command="cat marker.txt", timeout=10))
    assert "here" in res.stdout
    await rt.stop()
    await rt.destroy()


async def test_local_runtime_cwd_traversal_rejected(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    rt = LocalRuntime(ws, _local_config())
    await rt.create()
    await rt.start()
    with pytest.raises(Exception, match="traversal"):
        await rt.exec(ProcessSpec(command="ls", cwd="../../../etc", timeout=5))
    await rt.destroy()


async def test_local_runtime_not_started_rejects_exec(tmp_path: Path):
    rt = LocalRuntime(tmp_path / "ws", _local_config())
    await rt.create()
    with pytest.raises(SandboxError, match="not started"):
        await rt.exec(ProcessSpec(command="echo hi", timeout=5))


async def test_environment_inspect(tmp_path: Path):
    env = Environment.create(tmp_path / "ws", _local_config())
    await env.provision()
    status = await env.inspect()
    assert isinstance(status, RuntimeStatus)
    assert status.runtime_type == "local"
    assert status.ready is True
    await env.destroy()


async def test_unknown_runtime_type_raises(tmp_path: Path):
    cfg = EnvironmentConfig(runtime_type="bogus")
    with pytest.raises(SandboxError, match="unknown runtime type"):
        Environment.create(tmp_path / "ws", cfg)


# --- runtime is abstract ------------------------------------------------------


def test_environment_runtime_is_abstract():
    with pytest.raises(TypeError):
        EnvironmentRuntime(Path("/tmp"), _local_config())  # type: ignore[abstract]
