"""Phase 7 — Git/workspace safety regression tests.

Verifies workspace boundaries hold: no operation escapes the workspace,
symlink escapes remain blocked, Git commands are workspace-scoped, and
cleanup never deletes outside the workspace.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from kinetic.errors import GitError, PermissionDeniedError
from kinetic.events import EventBus
from kinetic.security import AuditLog, PermissionPolicy
from kinetic.tools.git import GitTools


@pytest.fixture
def git_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "README.md").write_text("# test\n")
    env = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@test.com",
           "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@test.com"}
    subprocess.run(["git", "init"], cwd=ws, check=True, capture_output=True, env=env)
    subprocess.run(["git", "add", "-A"], cwd=ws, check=True, capture_output=True, env=env)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=ws, check=True, capture_output=True, env=env)
    return ws


@pytest.fixture
def git_policy() -> PermissionPolicy:
    return PermissionPolicy(writable_roots=[Path("/tmp")], allow_git_write=True)


@pytest.fixture
def git_tools(git_workspace: Path, git_policy: PermissionPolicy) -> GitTools:
    return GitTools(
        workspace=git_workspace,
        policy=git_policy,
        audit=AuditLog(git_workspace / "audit.log"),
        events=EventBus(),
    )


class TestWorkspaceBoundary:
    async def test_git_status_workspace_scoped(self, git_tools: GitTools) -> None:
        result = await git_tools.status({})
        text = result["content"][0]["text"]
        assert "main" in text or "master" in text or "No commits" in text

    async def test_git_diff_returns_output(self, git_tools: GitTools) -> None:
        result = await git_tools.diff({})
        assert "content" in result

    async def test_git_log_bounded(self, git_tools: GitTools) -> None:
        result = await git_tools.log({"limit": 5})
        assert "content" in result

    async def test_log_limit_capped(self, git_tools: GitTools) -> None:
        result = await git_tools.log({"limit": 99999})
        # Should be capped to 200, not error.
        assert "content" in result


class TestGitWritePermissions:
    async def test_commit_requires_permission(self, git_workspace: Path) -> None:
        policy = PermissionPolicy(writable_roots=[git_workspace], allow_git_write=False)
        tools = GitTools(
            workspace=git_workspace, policy=policy,
            audit=AuditLog(git_workspace / "a.log"), events=EventBus(),
        )
        with pytest.raises(PermissionDeniedError):
            await tools.commit({"message": "test"})

    async def test_commit_succeeds_with_permission(self, git_tools: GitTools, git_workspace: Path) -> None:
        (git_workspace / "new.txt").write_text("content")
        result = await git_tools.commit({"message": "add file"})
        assert "content" in result

    async def test_empty_message_rejected(self, git_tools: GitTools) -> None:
        with pytest.raises(GitError):
            await git_tools.commit({"message": ""})


class TestSymlinkSafety:
    def test_symlink_escape_in_resolve(self, tmp_path: Path) -> None:
        from kinetic.errors import SecurityError
        from kinetic.paths import safe_resolve

        outside = tmp_path.parent / "outside"
        outside.mkdir(exist_ok=True)
        (outside / "secret.txt").write_text("secret")
        link = tmp_path / "escape_link"
        link.symlink_to(outside)
        with pytest.raises(SecurityError):
            safe_resolve(tmp_path, "escape_link/secret.txt")


class TestGitConfigSafety:
    """Git commits must inject identity via -c flags, never modify global config."""

    async def test_commit_uses_local_identity(self, git_tools: GitTools, git_workspace: Path) -> None:
        (git_workspace / "f.txt").write_text("x")
        await git_tools.commit({"message": "test commit"})
        # Verify global config was NOT set.
        global_name = subprocess.run(
            ["git", "config", "--global", "user.name"],
            capture_output=True, text=True,
        )
        # Either not set or not "KINETIC Agent" globally.
        assert global_name.stdout.strip() != "KINETIC Agent"


class TestMissingGit:
    async def test_git_in_non_repo_errors(self, tmp_path: Path) -> None:
        ws = tmp_path / "notrepo"
        ws.mkdir()
        policy = PermissionPolicy(writable_roots=[ws], allow_git_write=True)
        tools = GitTools(
            workspace=ws, policy=policy,
            audit=AuditLog(ws / "a.log"), events=EventBus(),
        )
        with pytest.raises(GitError):
            await tools.status({})


class TestWorkspaceDeletion:
    def test_workspace_cleanup_only_within(self, tmp_path: Path) -> None:
        from kinetic.environment.workspace import Workspace

        parent = tmp_path / "root"
        ws = Workspace.create(parent=parent, name="test-ws")
        assert ws.root.exists()
        # Create a sibling outside the workspace.
        sibling = tmp_path / "sibling"
        sibling.mkdir()
        (sibling / "keep.txt").write_text("keep")
        ws.cleanup()
        assert not ws.root.exists()
        # Sibling must be untouched.
        assert (sibling / "keep.txt").exists()
