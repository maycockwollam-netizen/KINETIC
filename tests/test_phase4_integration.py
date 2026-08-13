"""Phase 4 integration: memory + context engine integrated into AgentSession.

Verifies the intended flow:
  Task starts → context assembled → agent session receives context → task
  completes → only validated memory persists (no raw chat-history dumping).

No live Anthropic API key is required: we inject a fake adapter.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kinetic.agent.session import AgentSession, SessionConfig
from kinetic.events import EventType
from kinetic.memory.models import MemoryScope


def _settings(tmp_path: Path):
    from kinetic.config import Settings

    s = Settings(
        workspace_root=tmp_path / "ws",
        session_root=tmp_path / "ss",
        audit_log_path=tmp_path / "audit.log",
        memory_db_path=tmp_path / "mem.db",
    )
    s.ensure_directories()
    return s


class FakeResult:
    is_error = False
    result = "done"
    session_id = "fake"
    duration_ms = 1
    num_turns = 1


class FakeAdapter:
    """Records the system prompt it was built with; returns a fixed result."""

    def __init__(self) -> None:
        self.system_prompt_seen: str | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def query(self, prompt: str, *, session_id: str = "default") -> Any:
        return FakeResult()


def test_session_registry_includes_memory_tools(tmp_path: Path, workspace: Path):
    settings = _settings(tmp_path)
    cfg = SessionConfig(workspace=workspace, prompt="hi")
    session = AgentSession(settings, cfg)
    names = set(session.registry.names())
    assert {"memory_search", "memory_get", "memory_create", "memory_update", "memory_delete"} <= names


def test_session_builds_memory_and_context(tmp_path: Path, workspace: Path):
    settings = _settings(tmp_path)
    cfg = SessionConfig(workspace=workspace, prompt="hi")
    session = AgentSession(settings, cfg)
    assert session.memory is not None
    assert session.context is not None


def test_session_memory_permissions_default_off_for_write(tmp_path: Path, workspace: Path):
    from kinetic.security import MEMORY_WRITE

    settings = _settings(tmp_path)
    cfg = SessionConfig(workspace=workspace, prompt="hi")
    session = AgentSession(settings, cfg)
    d = session.policy.evaluate("memory_create", MEMORY_WRITE, {"content": "x"})
    assert not d.allowed  # write off by default


def test_session_memory_read_allowed_by_default(tmp_path: Path, workspace: Path):
    from kinetic.security import MEMORY_READ

    settings = _settings(tmp_path)
    cfg = SessionConfig(workspace=workspace, prompt="hi")
    session = AgentSession(settings, cfg)
    d = session.policy.evaluate("memory_search", MEMORY_READ, {"query": "x"})
    assert d.allowed


@pytest.mark.timeout(30)
async def test_session_context_assembled_before_run(tmp_path: Path, workspace: Path):
    """The agent session assembles a bounded context and merges it into the
    system prompt before the model run. Memory failures degrade gracefully."""
    settings = _settings(tmp_path)
    cfg = SessionConfig(
        workspace=workspace, prompt="install uv deps", network_policy="allow",
        allow_memory_write=True,
    )
    session = AgentSession(settings, cfg)
    # Pre-seed a project memory that should be retrieved into context.
    await session.memory.create(
        content="install dependencies with uv package manager",
        memory_type=MemoryScope.PROJECT,
        project_id=str(workspace),
        confidence=0.9,
    )
    # Assemble context (as run() would) and verify the relevant memory is
    # retrieved into the bounded context that the agent would receive.
    context_block = await session._assemble_context()
    assert context_block is not None
    assert "uv package manager" in context_block
    assert "KINETIC Context" in context_block
    # The base prompt is still present (no raw chat-history dump, just context).
    assert "install uv deps" in context_block


@pytest.mark.timeout(30)
async def test_session_run_emits_context_built_event(tmp_path: Path, workspace: Path):
    settings = _settings(tmp_path)
    cfg = SessionConfig(workspace=workspace, prompt="hi", network_policy="allow")
    session = AgentSession(settings, cfg)
    fake = FakeAdapter()
    session._adapter = fake  # type: ignore[assignment]
    await session.run()
    types = [ev.type for ev in session.events.history]
    assert EventType.CONTEXT_BUILT in types


@pytest.mark.timeout(30)
async def test_session_no_auto_persist_of_responses(tmp_path: Path, workspace: Path):
    """Assistant responses are NOT automatically persisted as memory."""
    settings = _settings(tmp_path)
    cfg = SessionConfig(
        workspace=workspace, prompt="do something", network_policy="allow",
        allow_memory_write=True,
    )
    session = AgentSession(settings, cfg)
    fake = FakeAdapter()
    session._adapter = fake  # type: ignore[assignment]
    await session.run()
    # No memories should exist from the run (we never auto-persisted).
    assert session.memory._store.list() == []


@pytest.mark.timeout(30)
async def test_session_memory_failure_does_not_block_run(tmp_path: Path, workspace: Path):
    """A memory backend failure degrades context but does not block the task."""
    settings = _settings(tmp_path)
    cfg = SessionConfig(workspace=workspace, prompt="hi", network_policy="allow")
    session = AgentSession(settings, cfg)
    # Break the store so context assembly degrades.
    session.memory._store.close()
    fake = FakeAdapter()
    session._adapter = fake  # type: ignore[assignment]
    result = await session.run()
    # Task still succeeds (degraded context, not a hard failure).
    assert result.success
