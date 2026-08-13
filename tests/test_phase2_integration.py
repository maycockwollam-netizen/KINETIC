"""Phase 2 integration test on a temporary sample repository.

Exercises the full stack without a live Claude model:
project scan -> workspace -> git -> dependency detect -> file modify -> diff ->
commit -> verify.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agent.session import AgentSession, SessionConfig
from config import Settings
from dependencies import detect_dependencies
from errors import PermissionDeniedError
from events import EventType
from project import scan_project
from security import AuditLog, PermissionPolicy
from tools.git import GitTools


def _git(root: Path, *args: str) -> None:
    env = {"GIT_AUTHOR_NAME": "x", "GIT_AUTHOR_EMAIL": "x@y.z",
           "GIT_COMMITTER_NAME": "x", "GIT_COMMITTER_EMAIL": "x@y.z"}
    subprocess.run(["git", *args], cwd=root, env={**env}, check=True,
                   capture_output=True, text=True)


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "sample"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "pyproject.toml").write_text('[project]\nname = "sample"\nversion = "0.1.0"\n')
    (repo / "requirements.txt").write_text("# empty\n")
    (repo / "README.md").write_text("# sample\n")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("def add(a, b):\n    return a - b  # bug\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    return repo


@pytest.mark.timeout(60)
def test_phase2_integration(sample_repo: Path, settings: Settings):
    # 1. Create/open workspace = the sample repo.
    from environment import Workspace

    ws = Workspace.open(sample_repo)
    assert ws.root == sample_repo.resolve()

    # 2. Detect project type.
    manifest = scan_project(ws.root)
    assert "python" in manifest.languages
    assert manifest.git_repository is True
    assert any(mf.kind == "python:pyproject" for mf in manifest.manifests)

    # 3. Inspect Git status (clean after initial commit).
    audit = AuditLog(settings.audit_log_path)
    policy = PermissionPolicy(writable_roots=[ws.root], allow_git_write=True, allow_dependency_install=True)
    git = GitTools(workspace=ws.root, policy=policy, audit=audit, default_timeout=15)
    import anyio

    status_out = anyio.run(git.status, {})
    assert "clean" in status_out["content"][0]["text"] or "##" in status_out["content"][0]["text"]

    # 4. Detect dependencies.
    deps = detect_dependencies(ws.root)
    assert deps and deps[0].ecosystem == "python"

    # 5. Modify a file (fix the bug).
    app = ws.root / "src" / "app.py"
    app.write_text("def add(a, b):\n    return a + b  # fixed\n")

    # 6. Show Git diff.
    diff_out = anyio.run(git.diff, {})
    assert "a + b" in diff_out["content"][0]["text"]
    assert "a - b" in diff_out["content"][0]["text"]

    # 7. Commit the change through the Git tool.
    commit_out = anyio.run(git.commit, {"message": "fix add function"})
    assert "fix add function" in commit_out["content"][0]["text"] or "main" in commit_out["content"][0]["text"] or "master" in commit_out["content"][0]["text"]

    # 8. Verify final repository state.
    log = subprocess.run(["git", "log", "--oneline"], cwd=ws.root, capture_output=True, text=True, check=True).stdout
    assert "fix add function" in log
    status2 = subprocess.run(["git", "status", "--porcelain"], cwd=ws.root, capture_output=True, text=True, check=True).stdout
    assert status2.strip() == ""

    # 9. Audit records exist for git ops.
    entries = audit.read()
    assert any(e["action"] == "git" and e["tool"] == "git_commit" for e in entries)


@pytest.mark.timeout(30)
async def test_phase2_permission_denial_blocks_commit(sample_repo: Path, settings: Settings):
    ws_path = sample_repo
    audit = AuditLog(settings.audit_log_path)
    policy = PermissionPolicy(writable_roots=[ws_path], allow_git_write=False)
    git = GitTools(workspace=ws_path, policy=policy, audit=audit, default_timeout=10)
    (ws_path / "x.txt").write_text("x")
    with pytest.raises(PermissionDeniedError):
        await git.commit({"message": "should fail"})
    # Verify no commit was actually created.
    log = subprocess.run(["git", "log", "--oneline"], cwd=ws_path, capture_output=True, text=True, check=True).stdout
    assert "should fail" not in log


@pytest.mark.timeout(30)
def test_phase2_session_emits_project_scanned_event(sample_repo: Path, settings: Settings):
    cfg = SessionConfig(workspace=sample_repo, prompt="scan", allow_git_write=True)
    session = AgentSession(settings, cfg)
    # The scan_project tool is registered.
    assert "scan_project" in session.registry.names()
    # Invoke it directly.
    import anyio

    tool = session.registry.get("scan_project")
    result = anyio.run(lambda: tool.func({}))
    payload = json.loads(result["content"][0]["text"])
    assert "python" in payload["languages"]
    assert payload["git_repository"] is True
    # Event emitted.
    assert any(e.type is EventType.PROJECT_SCANNED for e in session.events.history)
