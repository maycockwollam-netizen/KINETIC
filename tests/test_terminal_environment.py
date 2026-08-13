"""Tests for TerminalTool routing through the Environment abstraction."""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from environment import Environment, EnvironmentConfig, NetworkPolicy
from errors import ToolError
from tools.terminal import TerminalTool, terminal_tool


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


# --- Phase 3 hardening: prompt cancellation + no orphans ---------------------


async def test_run_command_cancellation_is_prompt(tmp_path: Path):
    """Cancellation must kill the process promptly, not after it self-exits.

    Previously the cancellation token was only checked in a ``finally`` block
    that ran *after* ``communicate`` returned, so a long sleep ran to
    completion. Now a watcher kills the whole process group on cancel.
    """
    import asyncio
    import time

    from tools.terminal import CancellationToken, run_command

    cancel = CancellationToken()
    start = time.monotonic()

    async def _cancel_after() -> None:
        await asyncio.sleep(0.4)
        cancel.cancel()

    async with anyio.create_task_group() as tg:
        tg.start_soon(_cancel_after)
        res = await run_command("sleep 30", timeout=60, cancellation=cancel)

    elapsed = time.monotonic() - start
    assert res.timed_out
    assert res.exit_code == -1
    assert elapsed < 5, f"cancellation was not prompt: {elapsed:.1f}s"


async def test_run_command_no_orphan_after_timeout(tmp_path: Path):
    """A timed-out child (e.g. ``sleep``) must not survive as an orphan."""
    import time

    from tools.terminal import run_command

    start = time.monotonic()
    # Use a distinctive duration so the orphan check can target it without
    # matching its own grep command line.
    res = await run_command("sleep 37", timeout=0.4)
    elapsed = time.monotonic() - start
    assert res.timed_out
    assert elapsed < 5, f"timeout was not prompt: {elapsed:.1f}s"
    # Give the reaper a moment; the sleep child must be gone. Match only the
    # exact `sleep 37` invocation (the [s] trick avoids self-matching grep).
    import anyio

    await anyio.sleep(0.2)
    check = await run_command("pgrep -f '[s]leep 37' || echo NO_ORPHAN", timeout=5)
    assert "NO_ORPHAN" in check.stdout, "orphan process survived timeout"
