"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from kinetic.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(
        workspace_root=tmp_path / "workspaces",
        session_root=tmp_path / "sessions",
        audit_log_path=tmp_path / "audit.log",
    )
    s.ensure_directories()
    return s


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "README.md").write_text("# sample\nHello from sample repo.\n")
    (ws / "src").mkdir()
    (ws / "src" / "app.py").write_text("def add(a, b):\n    return a + b\n")
    return ws
