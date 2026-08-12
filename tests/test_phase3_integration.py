"""Phase 3 integration: session builds a controlled environment.

Verifies that the agent session constructs an Environment with the correct
runtime/network/resource configuration from Settings and SessionConfig, and that
the terminal tool routes through it. No live model call is needed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kinetic.agent.session import AgentSession, SessionConfig
from kinetic.config import Settings
from kinetic.environment import EnvironmentState, NetworkPolicy
from kinetic.events import EventType


def _settings(tmp_path: Path) -> Settings:
    s = Settings(
        workspace_root=tmp_path / "ws",
        session_root=tmp_path / "ss",
        audit_log_path=tmp_path / "audit.log",
    )
    s.ensure_directories()
    return s


def test_session_builds_local_environment_with_defaults(tmp_path: Path, workspace: Path):
    settings = _settings(tmp_path)
    cfg = SessionConfig(workspace=workspace, prompt="x")
    session = AgentSession(settings, cfg)
    env = session.environment
    assert env.config.runtime_type == "local"
    assert env.config.network is NetworkPolicy.DENY
    assert env.state is EnvironmentState.CREATING


def test_session_env_config_overrides_from_session(tmp_path: Path, workspace: Path):
    settings = _settings(tmp_path)
    cfg = SessionConfig(
        workspace=workspace, prompt="x",
        runtime_type="docker", network_policy="allow",
        sandbox_mode=True,
    )
    session = AgentSession(settings, cfg)
    env = session.environment
    assert env.config.runtime_type == "docker"
    assert env.config.network is NetworkPolicy.ALLOW
    assert env.config.sandbox_mode is True


def test_session_settings_environment_config(tmp_path: Path):
    settings = _settings(tmp_path)
    settings.network_policy = "allow"
    settings.runtime_type = "docker"
    cfg = EnvironmentConfig_from_settings(settings)
    assert cfg.network is NetworkPolicy.ALLOW
    assert cfg.runtime_type == "docker"


def EnvironmentConfig_from_settings(settings: Settings):
    return settings.environment_config()


@pytest.mark.timeout(30)
async def test_session_provisions_local_environment_on_run(tmp_path: Path, workspace: Path):
    """Local environment provisions successfully and can execute a command.

    We invoke the registered terminal tool directly (no model) and confirm it
    routes through the environment, which must be RUNNING by then.
    """
    settings = _settings(tmp_path)
    cfg = SessionConfig(workspace=workspace, prompt="x", network_policy="allow")
    session = AgentSession(settings, cfg)
    # Provision the environment manually (run() would try the model).
    await session.environment.provision()
    assert session.environment.state is EnvironmentState.RUNNING
    tool = session.registry.get("run_command")
    result = await tool.func({"command": "echo routed"})
    assert "routed" in result["content"][0]["text"]
    # Process events were emitted through the session's bus.
    assert any(e.type is EventType.PROCESS_STARTED for e in session.events.history)
    await session.environment.stop()
    await session.environment.destroy()


def test_session_policy_gates_environment_exec(tmp_path: Path, workspace: Path):
    settings = _settings(tmp_path)
    cfg = SessionConfig(
        workspace=workspace, prompt="x", allow_environment_exec=False,
    )
    session = AgentSession(settings, cfg)
    # The session policy must deny ENVIRONMENT_EXEC.
    from kinetic.security import ENVIRONMENT_EXEC

    d = session.policy.evaluate("run_command", ENVIRONMENT_EXEC, {"command": "ls"})
    assert not d.allowed


def test_session_policy_denies_environment_network_by_default(tmp_path: Path, workspace: Path):
    settings = _settings(tmp_path)
    cfg = SessionConfig(workspace=workspace, prompt="x")
    session = AgentSession(settings, cfg)
    from kinetic.security import ENVIRONMENT_NETWORK

    d = session.policy.evaluate("set_net", ENVIRONMENT_NETWORK, {})
    assert not d.allowed
