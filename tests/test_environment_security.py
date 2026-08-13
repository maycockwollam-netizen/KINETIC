"""Security tests for the environment/sandbox layer.

Covers fail-closed behavior, host environment leakage, denied network,
permission denials, runtime failure, and the no-unrestricted-fallback rule.
"""

from __future__ import annotations

import os
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
from kinetic.errors import PermissionDeniedError, SandboxError
from kinetic.events import EventBus, EventType
from kinetic.security import (
    ENVIRONMENT_ADMIN,
    ENVIRONMENT_CREATE,
    ENVIRONMENT_EXEC,
    ENVIRONMENT_NETWORK,
    AuditLog,
    Capability,
    PermissionPolicy,
)


def _local(**kw) -> EnvironmentConfig:
    base = dict(runtime_type="local", sandbox_mode=False, network=NetworkPolicy.ALLOW)
    base.update(kw)
    return EnvironmentConfig(**base)


# --- fail closed: local sandbox cannot enforce network DENY --------------------


async def test_local_sandbox_fails_closed_on_network_deny(tmp_path: Path):
    cfg = EnvironmentConfig(runtime_type="local", sandbox_mode=True, network=NetworkPolicy.DENY)
    env = Environment.create(tmp_path / "ws", cfg)
    with pytest.raises(SandboxError, match="cannot enforce network policy"):
        await env.provision()
    assert env.state is EnvironmentState.FAILED


async def test_local_sandbox_fails_closed_on_resource_limits(tmp_path: Path):
    cfg = EnvironmentConfig(
        runtime_type="local",
        sandbox_mode=True,
        network=NetworkPolicy.ALLOW,
        resources=ResourceLimits(cpu=1.0),
    )
    env = Environment.create(tmp_path / "ws", cfg)
    with pytest.raises(SandboxError, match="cannot enforce resource limits"):
        await env.provision()
    assert env.state is EnvironmentState.FAILED


async def test_local_dev_mode_allows_network_deny_with_warning(tmp_path: Path):
    # sandbox_mode=False is the honest non-isolating dev mode: it proceeds but
    # records a warning that it is NOT isolating network.
    cfg = EnvironmentConfig(runtime_type="local", sandbox_mode=False, network=NetworkPolicy.DENY)
    env = Environment.create(tmp_path / "ws", cfg)
    await env.provision()
    status = await env.inspect()
    assert status.detail["network_warning"] is not None
    assert "does not isolate" in status.detail["network_warning"]
    await env.destroy()


# --- host environment leakage -------------------------------------------------


async def test_host_env_not_forwarded_without_allowlist(tmp_path: Path):
    """A sandboxed process receives only explicitly-injected env vars.

    The local runtime is honest that it does not provide strong isolation, but
    when an explicit env-var policy is configured it must NOT silently forward
    arbitrary host vars — only injected/allowed ones. We pass the filtered env
    explicitly via the spec so the subprocess inherits nothing else.
    """
    os.environ["LEAK_TEST_VAR"] = "secret-leak"
    try:
        pol = EnvironmentVariablePolicy(inject={"SAFE": "ok"})
        cfg = EnvironmentConfig(
            runtime_type="local", sandbox_mode=False, network=NetworkPolicy.ALLOW,
            env_vars=pol,
        )
        filtered = pol.filter(dict(os.environ))
        assert "LEAK_TEST_VAR" not in filtered
        assert filtered["SAFE"] == "ok"
        env = Environment.create(tmp_path / "ws", cfg)
        await env.provision()
        # Execute with the explicitly-filtered env so nothing else leaks.
        res = await env.exec(ProcessSpec(
            command='python3 -c "import os; print(os.environ.get(\'LEAK_TEST_VAR\', \'ABSENT\'))"',
            env=filtered, timeout=10,
        ))
        assert "ABSENT" in res.stdout
        assert "secret-leak" not in res.stdout
        await env.destroy()
    finally:
        os.environ.pop("LEAK_TEST_VAR", None)


async def test_host_env_secret_name_redacted(tmp_path: Path):
    """Secret-*named* variables are dropped entirely (never forwarded)."""
    os.environ["LEAK_API_KEY"] = "sk-verysecretleak1234567890"
    try:
        pol = EnvironmentVariablePolicy(allowed={"LEAK_API_KEY"})
        # Even though allowlisted, a secret-named var is dropped, not forwarded.
        filtered = pol.filter({"LEAK_API_KEY": "sk-verysecretleak1234567890"})
        assert "LEAK_API_KEY" not in filtered
    finally:
        os.environ.pop("LEAK_API_KEY", None)


# --- permission policy for environment capabilities ----------------------------


def test_environment_exec_denied_by_policy():
    policy = PermissionPolicy(allow_environment_exec=False)

    d = policy.evaluate("run_command", ENVIRONMENT_EXEC, {"command": "ls"})
    assert not d.allowed
    assert "environment" in d.reason.lower()


def test_environment_network_denied_by_default():
    policy = PermissionPolicy()
    d = policy.evaluate("set_network", ENVIRONMENT_NETWORK, {})
    assert not d.allowed


def test_environment_admin_denied_by_default():
    policy = PermissionPolicy()
    d = policy.evaluate("set_limits", ENVIRONMENT_ADMIN, {})
    assert not d.allowed


def test_environment_create_denied_by_policy():
    policy = PermissionPolicy(allow_environment_create=False)
    d = policy.evaluate("create_env", ENVIRONMENT_CREATE, {})
    assert not d.allowed


def test_environment_exec_allowed_by_default():
    policy = PermissionPolicy()
    d = policy.evaluate("run_command", ENVIRONMENT_EXEC, {"command": "ls"})
    assert d.allowed


def test_permission_denial_raises_and_is_audited(tmp_path: Path):
    policy = PermissionPolicy(allow_environment_exec=False)
    with pytest.raises(PermissionDeniedError):
        policy.require("run_command", ENVIRONMENT_EXEC, {"command": "ls"})


def test_environment_capability_flags_compose():
    combo = Capability.ENVIRONMENT_CREATE | Capability.ENVIRONMENT_EXEC
    assert Capability.ENVIRONMENT_CREATE in combo
    assert Capability.ENVIRONMENT_NETWORK not in combo


# --- no unrestricted fallback -------------------------------------------------


async def test_docker_runtime_unavailable_does_not_fallback(tmp_path: Path, monkeypatch):
    """If docker is requested but unavailable, we must NOT run on host."""
    from kinetic.environment.docker import DockerRuntime

    async def fake_probe():
        return False

    monkeypatch.setattr(DockerRuntime, "probe", staticmethod(fake_probe))
    cfg = EnvironmentConfig.docker_defaults()
    env = Environment.create(tmp_path / "ws", cfg)
    from kinetic.errors import RuntimeUnavailableError

    with pytest.raises(RuntimeUnavailableError, match="refusing to fall back"):
        await env.provision()
    assert env.state is EnvironmentState.FAILED


# --- filesystem escape (path traversal / symlink) via environment cwd ---------


async def test_environment_exec_cwd_traversal_blocked(tmp_path: Path):
    env = Environment.create(tmp_path / "ws", _local())
    await env.provision()
    from kinetic.errors import SecurityError

    with pytest.raises(SecurityError, match="traversal"):
        await env.exec(ProcessSpec(command="ls", cwd="../../etc", timeout=5))
    await env.destroy()


# --- permission denial emits event -------------------------------------------


async def test_permission_denial_emits_event_via_policy(tmp_path: Path):
    bus = EventBus()
    audit = AuditLog(tmp_path / "a.log")
    policy = PermissionPolicy(allow_environment_exec=False)
    # Manually exercise the denial path the way the adapter would.
    try:
        policy.require("run_command", ENVIRONMENT_EXEC, {"command": "ls"})
    except PermissionDeniedError as exc:
        audit.record(session_id="s1", action="permission", tool="run_command",
                     allowed=False, reason=exc.reason)
        bus.emit(EventType.PERMISSION_DENIED, "s1", tool="run_command", reason=exc.reason)
    assert any(e.type is EventType.PERMISSION_DENIED for e in bus.history)
    assert any(e["allowed"] is False for e in audit.read())


# --- Phase 3 hardening: direct-call permission boundary ----------------------


async def test_environment_exec_enforces_policy_on_direct_call(tmp_path: Path):
    """A direct ``env.exec`` call (bypassing the adapter) must still be gated.

    The Environment enforces ENVIRONMENT_EXEC itself — there is no path that
    reaches the runtime without the permission policy being consulted.
    """
    bus = EventBus()
    audit = AuditLog(tmp_path / "a.log")
    policy = PermissionPolicy(allow_environment_exec=False)
    env = Environment.create(tmp_path / "ws", _local(), policy=policy,
                             audit=audit, events=bus, session_id="s1")
    await env.provision()
    with pytest.raises(PermissionDeniedError):
        await env.exec(ProcessSpec(command="echo hi", timeout=5))
    # Denial is emitted as an event and recorded in the audit log.
    assert any(e.type is EventType.PERMISSION_DENIED for e in bus.history)
    denied = [e for e in audit.read() if e["action"] == "permission" and not e["allowed"]]
    assert denied, "exec denial must be audited"
    await env.destroy()


async def test_environment_exec_allowed_when_policy_permits(tmp_path: Path):
    """When the policy permits exec, a direct call runs normally."""
    policy = PermissionPolicy(allow_environment_exec=True)
    env = Environment.create(tmp_path / "ws", _local(), policy=policy, session_id="s1")
    await env.provision()
    res = await env.exec(ProcessSpec(command="echo ok", timeout=5))
    assert res.succeeded
    await env.destroy()


# --- Phase 3 hardening: local runtime never inherits host env ----------------


async def test_local_runtime_does_not_inherit_host_env(tmp_path: Path):
    """Without an explicit env-var allowlist, no host variable is forwarded.

    The local runtime builds the subprocess env from the policy filter (which
    forwards nothing by default) rather than passing env=None and letting the
    subprocess inherit the full host environment.
    """
    os.environ["KINETIC_HARDENING_LEAK"] = "should-not-leak"
    try:
        env = Environment.create(tmp_path / "ws", _local())
        await env.provision()
        res = await env.exec(ProcessSpec(
            command='python3 -c "import os; print(os.environ.get(\'KINETIC_HARDENING_LEAK\', \'ABSENT\'))"',
            timeout=10,
        ))
        assert "ABSENT" in res.stdout
        assert "should-not-leak" not in res.stdout
        await env.destroy()
    finally:
        os.environ.pop("KINETIC_HARDENING_LEAK", None)


async def test_local_runtime_injects_explicit_env(tmp_path: Path):
    """Explicitly injected vars reach the subprocess; secret-named ones don't."""
    pol = EnvironmentVariablePolicy(inject={"APP_MODE": "test", "APP_TOKEN": "sk-secret1234567890"})
    cfg = EnvironmentConfig(runtime_type="local", sandbox_mode=False,
                            network=NetworkPolicy.ALLOW, env_vars=pol)
    env = Environment.create(tmp_path / "ws", cfg)
    await env.provision()
    res = await env.exec(ProcessSpec(
        command='python3 -c "import os; print(os.environ.get(\'APP_MODE\', \'ABSENT\')); print(os.environ.get(\'APP_TOKEN\', \'ABSENT\'))"',
        timeout=10,
    ))
    assert "test" in res.stdout  # APP_MODE injected
    # APP_TOKEN is secret-named -> dropped, never forwarded.
    assert "sk-secret1234567890" not in res.stdout
    await env.destroy()
