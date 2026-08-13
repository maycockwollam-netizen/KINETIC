"""Tool registry and base tool protocol.

A tool is a callable with a stable interface: a name, a description, a JSON
input schema, a permission policy, and an async ``run`` method. The registry
collects tools and exposes them to the Claude Agent SDK as an in-process MCP
server.

Design note: we do NOT re-implement the agent loop or the MCP transport — the
SDK's ``create_sdk_mcp_server`` does that. We only provide the tool functions
and permission metadata, and a small registry to assemble them.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from security.policy import ToolPermission


@runtime_checkable
class ToolFunc(Protocol):
    def __call__(self, args: Any) -> Awaitable[dict[str, Any]]: ...


@dataclass(frozen=True)
class ToolDefinition:
    """A registered tool: its callable plus declarative metadata."""

    name: str
    description: str
    input_schema: dict[str, Any]
    permission: ToolPermission
    func: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

    def allowed_tool_name(self, server_name: str) -> str:
        """The name the SDK uses to expose this tool on an MCP server."""
        return f"mcp__{server_name}__{self.name}"


class ToolRegistry:
    """A collection of tools that can be exposed to the agent."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def all(self) -> list[ToolDefinition]:
        return list(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:  # pragma: no cover - trivial
        return name in self._tools


def tool_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    """Build the content dict the SDK MCP tool protocol expects."""
    return {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }
