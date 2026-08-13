"""Unit tests for the agent adapter (no live model call).

Uses fakes for the SDK client + transport so we can verify the adapter wires
tools, the permission gate, and events correctly without an API key.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.adapter import AgentAdapter
from events import EventBus
from security import AuditLog, PermissionPolicy
from tools.base import ToolRegistry
from tools.filesystem import filesystem_tools
from tools.terminal import terminal_tool


def _registry(workspace: Path) -> ToolRegistry:
    reg = ToolRegistry()
    for t in filesystem_tools(workspace):
        reg.register(t)
    reg.register(terminal_tool(cwd=str(workspace), default_timeout=5, max_timeout=10))
    return reg


def test_adapter_builds_options_with_mcp_server(workspace: Path, settings):
    # Patch the SDK symbols to fakes so _build_options doesn't need a key.
    import agent.adapter as mod

    calls = {}

    def _fake_tool(name, description, schema, annotations=None):
        calls.setdefault("tools", []).append(name)

        def _decorator(fn):
            class _Inner:
                def __init__(self, func):
                    self.func = func

                async def __call__(self, *args, **kwargs):
                    return await self.func(*args, **kwargs)

            return _Inner(fn)

        return _decorator

    class _FakeServer:
        def __init__(self, **kw):
            calls["server"] = kw

    captured = {}

    class _FakeOptions:
        def __init__(self, **kw):
            captured.update(kw)

    mod.ClaudeAgentOptions = _FakeOptions
    mod.create_sdk_mcp_server = _FakeServer
    mod.sdk_tool = _fake_tool
    try:
        reg = _registry(workspace)
        AgentAdapter(
            registry=reg,
            policy=PermissionPolicy(writable_roots=[workspace]),
            audit=AuditLog(settings.audit_log_path),
            events=EventBus(),
            cwd=workspace,
            max_turns=5,
        )
        assert "mcp_servers" in captured
        assert "kinetic" in captured["mcp_servers"]
        assert captured["max_turns"] == 5
        assert "run_command" in captured["allowed_tools"][0] or any(
            "run_command" in t for t in captured["allowed_tools"]
        )
        # Six tools registered -> six allowed tool names.
        assert len(captured["allowed_tools"]) == 6
    finally:
        # Restore real symbols.
        from claude_agent_sdk import (
            ClaudeAgentOptions,
        )
        from claude_agent_sdk import (
            create_sdk_mcp_server as real_create,
        )
        from claude_agent_sdk import (
            tool as real_tool,
        )

        mod.ClaudeAgentOptions = ClaudeAgentOptions
        mod.create_sdk_mcp_server = real_create
        mod.sdk_tool = real_tool


@pytest.mark.timeout(10)
async def test_can_use_tool_gates_and_audits(workspace: Path, settings):
    """The runtime permission gate must audit allow/deny decisions."""
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

    reg = _registry(workspace)
    events = EventBus()
    adapter = AgentAdapter(
        registry=reg,
        policy=PermissionPolicy(writable_roots=[workspace], allow_network=False),
        audit=AuditLog(settings.audit_log_path),
        events=events,
        cwd=workspace,
        max_turns=5,
    )

    # write_file inside workspace -> allowed.
    decision = await adapter._can_use_tool(
        "mcp__kinetic__write_file", {"path": "a.txt", "content": "x"}, None
    )
    assert isinstance(decision, PermissionResultAllow)
    # read_file -> allowed.
    decision = await adapter._can_use_tool("mcp__kinetic__read_file", {"path": "a.txt"}, None)
    assert isinstance(decision, PermissionResultAllow)
    # unknown tool -> denied.
    decision = await adapter._can_use_tool("mcp__kinetic__nope", {}, None)
    assert isinstance(decision, PermissionResultDeny)

    entries = adapter._audit.read()
    # Three permission decisions recorded.
    assert len(entries) == 3
    assert entries[0]["allowed"] is True
    assert entries[-1]["allowed"] is False
