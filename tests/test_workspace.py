"""Unit tests for the Workspace abstraction."""

from __future__ import annotations

from pathlib import Path

import pytest

from kinetic.environment import Workspace, WorkspaceStatus
from kinetic.errors import SecurityError, WorkspaceError


def test_create_workspace(tmp_path: Path):
    ws = Workspace.create(tmp_path, name="proj")
    assert ws.root == (tmp_path / "proj").resolve()
    assert ws.status == WorkspaceStatus.CREATED
    assert ws.root.is_dir()


def test_create_workspace_generates_name(tmp_path: Path):
    ws = Workspace.create(tmp_path)
    assert ws.root.parent == tmp_path.resolve()


def test_create_workspace_already_exists(tmp_path: Path):
    Workspace.create(tmp_path, name="dup")
    with pytest.raises(WorkspaceError, match="already exists"):
        Workspace.create(tmp_path, name="dup")


def test_open_existing_workspace(tmp_path: Path):
    (tmp_path / "existing").mkdir()
    ws = Workspace.open(tmp_path / "existing")
    assert ws.status == WorkspaceStatus.OPENED
    assert not ws.readonly


def test_open_missing_workspace(tmp_path: Path):
    with pytest.raises(WorkspaceError, match="does not exist"):
        Workspace.open(tmp_path / "nope")


def test_open_file_not_dir(tmp_path: Path):
    (tmp_path / "file").write_text("x")
    with pytest.raises(WorkspaceError, match="not a directory"):
        Workspace.open(tmp_path / "file")


def test_validate_path_relative(tmp_path: Path):
    ws = Workspace.open(tmp_path)
    p = ws.validate_path("src/app.py")
    assert p == (ws.root / "src" / "app.py").resolve()


def test_validate_path_traversal_rejected(tmp_path: Path):
    ws = Workspace.open(tmp_path)
    with pytest.raises(SecurityError, match="traversal"):
        ws.validate_path("../../../etc/passwd")


def test_validate_path_absolute_outside_rejected(tmp_path: Path):
    ws = Workspace.open(tmp_path)
    with pytest.raises(SecurityError):
        ws.validate_path("/etc/passwd")


def test_validate_path_absolute_inside_allowed(tmp_path: Path):
    ws = Workspace.open(tmp_path)
    inside = ws.root / "a.txt"
    assert ws.validate_path(inside) == inside.resolve()


def test_symlink_escape_rejected(tmp_path: Path):
    # Workspace is a subdirectory; target is genuinely outside it.
    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    ws = Workspace.open(ws_root)
    target = tmp_path / "outside.txt"  # sibling of workspace -> outside
    target.write_text("secret")
    link = ws_root / "escape.txt"
    link.symlink_to(target)
    with pytest.raises(SecurityError, match="traversal"):
        ws.validate_path("escape.txt")


def test_symlink_inside_allowed(tmp_path: Path):
    ws = Workspace.open(tmp_path)
    (ws.root / "real.txt").write_text("ok")
    link = ws.root / "link.txt"
    link.symlink_to(ws.root / "real.txt")
    assert ws.validate_path("link.txt") == (ws.root / "real.txt").resolve()


def test_readonly_workspace_rejects_writes(tmp_path: Path):
    ws = Workspace.open(tmp_path, readonly=True)
    with pytest.raises(WorkspaceError, match="read-only"):
        ws.assert_writable()


def test_cleanup_removes_workspace(tmp_path: Path):
    ws = Workspace.create(tmp_path, name="goner")
    (ws.root / "file.txt").write_text("x")
    ws.cleanup()
    assert not ws.root.exists()
    assert ws.status == WorkspaceStatus.DELETED


def test_cleanup_idempotent(tmp_path: Path):
    ws = Workspace.create(tmp_path, name="once")
    ws.cleanup()
    ws.cleanup()  # second call is a no-op


def test_cleanup_without_parent_rejected(tmp_path: Path):
    # Open (not create) -> no parent recorded -> cannot cleanup.
    (tmp_path / "manual").mkdir()
    ws = Workspace.open(tmp_path / "manual")
    with pytest.raises(WorkspaceError, match="no recorded parent"):
        ws.cleanup()


def test_metadata(tmp_path: Path):
    ws = Workspace.create(tmp_path, name="meta")
    m = ws.metadata()
    assert m.root == ws.root
    assert m.status == "created"
    assert str(m.parent) == str(tmp_path.resolve())
    assert m.readonly is False
    assert isinstance(m.to_dict(), dict)


def test_workspace_emits_events(tmp_path: Path):
    from kinetic.events import EventBus, EventType

    bus = EventBus()
    ws = Workspace.create(tmp_path, name="evt", events=bus, session_id="s1")
    assert any(e.type is EventType.WORKSPACE_CREATED for e in bus.history)
    ws.cleanup()
    assert any(e.type is EventType.WORKSPACE_DELETED for e in bus.history)
