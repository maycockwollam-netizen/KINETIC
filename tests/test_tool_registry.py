"""Unit tests for the tool registry."""

from __future__ import annotations

import pytest

from kinetic.security.policy import READ_ONLY
from kinetic.tools.base import ToolDefinition, ToolRegistry, tool_result


async def _noop(args):
    return tool_result("ok")


def _make(name: str) -> ToolDefinition:
    return ToolDefinition(name=name, description="d", input_schema={}, permission=READ_ONLY, func=_noop)


def test_register_and_get():
    reg = ToolRegistry()
    reg.register(_make("a"))
    assert reg.get("a").name == "a"
    assert reg.names() == ["a"]
    assert len(reg) == 1
    assert "a" in reg


def test_duplicate_register_raises():
    reg = ToolRegistry()
    reg.register(_make("a"))
    with pytest.raises(ValueError, match="already registered"):
        reg.register(_make("a"))


def test_tool_result_shape():
    r = tool_result("hello")
    assert r["content"][0]["text"] == "hello"
    assert r["isError"] is False


def test_allowed_tool_name():
    d = _make("foo")
    assert d.allowed_tool_name("kinetic") == "mcp__kinetic__foo"
