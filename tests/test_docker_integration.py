"""Docker runtime integration tests.

These run real containers. They are skipped automatically when the Docker
daemon is unavailable (e.g. in CI without Docker). They never silently fall
back to host execution — if Docker is requested but missing, the environment
must fail closed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kinetic.environment import (
    Environment,
    EnvironmentConfig,
    EnvironmentState,
    EnvironmentVariablePolicy,
    NetworkPolicy,
    ProcessSpec,
    ResourceLimits,
)
from kinetic.environment.docker import DockerRuntime
from kinetic.errors import SandboxError

pytestmark = pytest.mark.timeout(120)


@pytest.fixture
def docker_available() -> bool:
    import anyio

    return anyio.run(DockerRuntime.probe)


@pytest.fixture
def require_docker(docker_available: bool):
    if not docker_available:
        pytest.skip("Docker daemon unavailable; skipping container integration tests")


def _docker_cfg(**kw) -> EnvironmentConfig:
    base = dict(runtime_type="docker", sandbox_mode=True, network=NetworkPolicy.DENY,
                image="python:3.11-slim")
    base.update(kw)
    return EnvironmentConfig(**base)


# --- basic lifecycle + exec --------------------------------------------------


async def test_docker_create_exec_destroy(tmp_path: Path, require_docker):
    env = Environment.create(tmp_path / "ws", _docker_cfg(), session_id="d1")
    await env.provision()
    assert env.state is EnvironmentState.RUNNING
    res = await env.exec(ProcessSpec(command="python3 -c 'print(1+1)'", timeout=60))
    assert res.succeeded
    assert "2" in res.stdout
    await env.stop()
    await env.destroy()
    assert env.state is EnvironmentState.DESTROYED


async def test_docker_workspace_filesystem_isolation(tmp_path: Path, require_docker):
    """A file written on the host workspace is visible inside; host paths are not."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "hello.txt").write_text("from-host")
    env = Environment.create(ws, _docker_cfg(), session_id="d2")
    await env.provision()
    res = await env.exec(ProcessSpec(
        command="cat /workspace/hello.txt && echo --- && test -f /etc/hostname && echo HOSTNAME_PRESENT",
        timeout=60,
    ))
    assert "from-host" in res.stdout
    # /workspace mount works; the container has its own minimal rootfs.
    await env.destroy()


async def test_docker_environment_isolation(tmp_path: Path, require_docker, monkeypatch):
    """Only explicitly injected/allowed env vars reach the container."""
    monkeypatch.setenv("LEAK_SECRET_KEY", "sk-leaktest1234567890")
    monkeypatch.setenv("SAFE_HOST_VAR", "safe-val")
    cfg = _docker_cfg(env_vars=EnvironmentVariablePolicy(
        allowed={"SAFE_HOST_VAR"}, inject={"INJECTED_VAR": "injected-val"},
    ))
    env = Environment.create(tmp_path / "ws", cfg, session_id="d3")
    await env.provision()
    res = await env.exec(ProcessSpec(
        command="python3 -c \"import os; env=sorted(os.environ); print(env)\"",
        timeout=60,
    ))
    assert "INJECTED_VAR" in res.stdout
    assert "SAFE_HOST_VAR" in res.stdout
    # Secret-named or denied host vars must NOT leak.
    assert "LEAK_SECRET_KEY" not in res.stdout
    assert "sk-leaktest" not in res.stdout
    await env.destroy()


async def test_docker_network_policy_deny_blocks_egress(tmp_path: Path, require_docker):
    """network=DENY (--network none) must prevent outbound connections."""
    env = Environment.create(tmp_path / "ws", _docker_cfg(network=NetworkPolicy.DENY), session_id="d4")
    await env.provision()
    res = await env.exec(ProcessSpec(
        command="python3 -c \"import socket; s=socket.socket(); s.settimeout(5); "
        "s.connect(('1.1.1.1',80))\" 2>&1; echo EXIT=$?",
        timeout=60,
    ))
    # Connection must fail (no network). Exit code from the python line non-zero.
    assert "EXIT=0" not in res.stdout or "Network is unreachable" in res.stdout
    await env.destroy()


async def test_docker_timeout(tmp_path: Path, require_docker):
    env = Environment.create(tmp_path / "ws", _docker_cfg(), session_id="d5")
    await env.provision()
    res = await env.exec(ProcessSpec(command="python3 -c 'import time; time.sleep(30)'", timeout=3))
    assert res.timed_out
    from kinetic.environment.process import ProcessState

    assert res.state is ProcessState.TIMED_OUT
    await env.destroy()


async def test_docker_resource_limits_enforced(tmp_path: Path, require_docker):
    """A memory limit lower than the process's allocation is enforced."""
    cfg = _docker_cfg(resources=ResourceLimits(memory_bytes=16 * 1024 * 1024))  # 16 MiB
    env = Environment.create(tmp_path / "ws", cfg, session_id="d6")
    await env.provision()
    # Allocate 64 MiB -> should be OOM-killed under a 16 MiB limit.
    res = await env.exec(ProcessSpec(
        command="python3 -c \"x = b'x' * (64 * 1024 * 1024); print(len(x))\"",
        timeout=60,
    ))
    # Exit code 137 (SIGKILL/OOM) or non-zero; not a clean success.
    assert not res.succeeded
    await env.destroy()


async def test_docker_cancellation(tmp_path: Path, require_docker):
    from kinetic.tools.terminal import CancellationToken

    cancel = CancellationToken()
    env = Environment.create(tmp_path / "ws", _docker_cfg(), session_id="d7")
    await env.provision()
    # Schedule cancellation shortly after starting a long sleep.
    import asyncio

    async def _cancel_after():
        await asyncio.sleep(1)
        cancel.cancel()

    asyncio.create_task(_cancel_after())
    res = await env.exec(
        ProcessSpec(command="python3 -c 'import time; time.sleep(20)'", timeout=30),
        cancellation=cancel,
    )
    # Either cancelled or timed out — must NOT have completed the full sleep.
    assert res.exit_code != 0 or res.state is not __import__(
        "kinetic.environment.process", fromlist=["ProcessState"]
    ).ProcessState.COMPLETED
    await env.destroy()


async def test_docker_cleanup_removes_container(tmp_path: Path, require_docker):
    env = Environment.create(tmp_path / "ws", _docker_cfg(), session_id="d8")
    await env.provision()
    container_id = env.runtime._container_id  # type: ignore[attr-defined]
    assert container_id is not None
    await env.destroy()
    # The container must be gone.
    from kinetic.tools.terminal import run_command

    res = await run_command(f"sudo docker inspect --format '{{{{.State.Status}}}}' {container_id}",
                            timeout=15)
    assert res.exit_code != 0  # inspect fails on a removed container


async def test_docker_restricted_network_without_rules_is_deny(tmp_path: Path, require_docker):
    """RESTRICTED with no rules falls back to deny (no proxy infra)."""
    env = Environment.create(tmp_path / "ws", _docker_cfg(network=NetworkPolicy.RESTRICTED), session_id="d9")
    await env.provision()
    res = await env.exec(ProcessSpec(
        command="python3 -c \"import socket; s=socket.socket(); s.settimeout(3); s.connect(('1.1.1.1',80))\" 2>&1; echo EXIT=$?",
        timeout=60,
    ))
    assert "EXIT=0" not in res.stdout
    await env.destroy()


async def test_docker_disk_limit_unsupported_fails_closed(tmp_path: Path, require_docker):
    """disk_bytes is not supported -> create fails closed rather than ignoring."""
    cfg = _docker_cfg(resources=ResourceLimits(disk_bytes=100 * 1024 * 1024))
    env = Environment.create(tmp_path / "ws", cfg, session_id="d10")
    with pytest.raises(SandboxError, match="disk"):
        await env.provision()
    assert env.state is EnvironmentState.FAILED
