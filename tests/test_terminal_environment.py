"""Tests for TerminalTool routing through the Environment abstraction."""

from __future__ import annotations

from pathlib import Path

import pytest

from kinetic.environment import Environment, EnvironmentConfig, NetworkPolicy
from kinetic.errors import ToolError
from kinetic.tools.terminal import TerminalTool, terminal_tool


def _local_cfg(**kw) -> EnvironmentConfig:
    base = dict(runtime_type="local", sandbox_mode=False, network=NetworkPolicy.ALLOW)
    base.update(kw)
    return EnvironmentConfig(**base)


async def test_terminal_tool_uses_environment(tmp_path: Path):
    env = Environment.create(tmp_path / "ws", _local_cfg(), session_id="t1")
    await env.provision()
    tt = TerminalTool(cwd=str(env.workspace), default_timeout=10, max_timeout=20, environment=env)
    r = await tt.run({"command": "echo via-env"})
    text = r["content"][0]["text"]
    assert "[exit 0]" in text
    assert "via-env" in text
    await env.destroy()


async def test_terminal_tool_without_environment_preserves_legacy(tmp_path: Path):
    """No environment -> low-level run_command path (backwards compatible)."""
    tt = TerminalTool(cwd=str(tmp_path), default_timeout=10, max_timeout=20)
    r = await tt.run({"command": "echo legacy"})
    assert "legacy" in r["content"][0]["text"]


async def test_terminal_tool_missing_command(tmp_path: Path):
    tt = TerminalTool(cwd=str(tmp_path), default_timeout=5, max_timeout=10)
    with pytest.raises(ToolError):
        await tt.run({"command": ""})


async def test_terminal_tool_timeout_via_environment(tmp_path: Path):
    env = Environment.create(tmp_path / "ws", _local_cfg(), session_id="t2")
    await env.provision()
    tt = TerminalTool(cwd=str(env.workspace), default_timeout=0.3, max_timeout=10, environment=env)
    r = await tt.run({"command": "sleep 5"})
    assert "TIMED OUT" in r["content"][0]["text"]
    assert r["isError"]
    await env.destroy()


async def test_terminal_tool_nonzero_exit_via_environment(tmp_path: Path):
    env = Environment.create(tmp_path / "ws", _local_cfg(), session_id="t3")
    await env.provision()
    tt = TerminalTool(cwd=str(env.workspace), default_timeout=10, max_timeout=20, environment=env)
    r = await tt.run({"command": "exit 3"})
    assert "[exit 3]" in r["content"][0]["text"]
    assert r["isError"]
    await env.destroy()


async def test_terminal_tool_factory_with_environment(tmp_path: Path):
    env = Environment.create(tmp_path / "ws", _local_cfg(), session_id="t4")
    await env.provision()
    defn = terminal_tool(cwd=str(env.workspace), default_timeout=10, max_timeout=20, environment=env)
    r = await defn.func({"command": "echo factory"})
    assert "factory" in r["content"][0]["text"]
    await env.destroy()
