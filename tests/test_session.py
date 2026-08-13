"""Unit tests for session assembly."""

from __future__ import annotations

from pathlib import Path

from agent.session import AgentSession, SessionConfig, default_tools_for


def test_session_builds_registry(workspace: Path, settings):
    cfg = SessionConfig(workspace=workspace, prompt="hi")
    session = AgentSession(settings, cfg)
    assert set(session.registry.names()) >= {
        # Phase 1
        "read_file",
        "write_file",
        "edit_file",
        "list_dir",
        "search_files",
        "run_command",
        # Phase 2
        "git_status",
        "git_diff",
        "git_log",
        "git_branch",
        "git_show",
        "git_checkout",
        "git_commit",
        "scan_project",
        "detect_dependencies",
        "install_dependencies",
        # Phase 4 — memory tools
        "memory_search",
        "memory_get",
        "memory_create",
        "memory_update",
        "memory_delete",
    }
    assert session.audit is not None
    assert session.events is not None
    assert session.policy is not None


def test_build_adapter_returns_adapter(workspace: Path, settings):
    cfg = SessionConfig(workspace=workspace, prompt="hi")
    session = AgentSession(settings, cfg)
    adapter = session.build_adapter()
    assert adapter is not None
    assert adapter.options is not None


def test_default_tools_for(workspace: Path, settings):
    tools = default_tools_for(workspace, settings)
    assert len(tools) == 16
    assert any(t.name == "run_command" for t in tools)
    assert any(t.name == "git_status" for t in tools)
    assert any(t.name == "scan_project" for t in tools)


def test_session_id_is_unique(workspace: Path, settings):
    cfg = SessionConfig(workspace=workspace, prompt="hi")
    a = AgentSession(settings, cfg)
    b = AgentSession(settings, cfg)
    assert a.session_id != b.session_id


def test_workspace_in_writable_roots(workspace: Path, settings):
    cfg = SessionConfig(workspace=workspace, prompt="hi")
    session = AgentSession(settings, cfg)
    # write_file within workspace must be allowed by policy.
    from security.policy import FILE_WRITE

    decision = session.policy.evaluate("write_file", FILE_WRITE, {"path": str(workspace / "x.txt")})
    assert decision.allowed
