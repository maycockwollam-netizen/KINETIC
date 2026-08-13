"""Unit tests for Git tools."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from errors import GitError, PermissionDeniedError
from events import EventBus, EventType
from security import AuditLog, PermissionPolicy
from tools.git import GitTools, git_tools


def _git(root: Path, *args: str) -> str:
    env = {"GIT_AUTHOR_NAME": "x", "GIT_AUTHOR_EMAIL": "x@y.z",
           "GIT_COMMITTER_NAME": "x", "GIT_COMMITTER_EMAIL": "x@y.z"}
    r = subprocess.run(["git", *args], cwd=root, env={**env}, capture_output=True, text=True, check=True)
    return r.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("hello\n")
    _git(tmp_path, "add", "a.txt")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    return tmp_path


def _tools(repo: Path, *, allow_write: bool = False, events: EventBus | None = None) -> GitTools:
    return GitTools(
        workspace=repo,
        policy=PermissionPolicy(writable_roots=[repo], allow_git_write=allow_write),
        audit=AuditLog(repo / "audit.log"),
        events=events,
        default_timeout=15,
        max_timeout=30,
    )


# --- read-only ---------------------------------------------------------------


@pytest.mark.timeout(15)
async def test_status(repo: Path):
    g = _tools(repo)
    r = await g.status({})
    assert "##" in r["content"][0]["text"] or "branch" in r["content"][0]["text"]


@pytest.mark.timeout(15)
async def test_diff(repo: Path):
    (repo / "a.txt").write_text("changed\n")
    g = _tools(repo)
    r = await g.diff({})
    assert "changed" in r["content"][0]["text"]


@pytest.mark.timeout(15)
async def test_diff_staged(repo: Path):
    (repo / "b.txt").write_text("new\n")
    _git(repo, "add", "b.txt")
    g = _tools(repo)
    r = await g.diff({"staged": True})
    assert "b.txt" in r["content"][0]["text"]


@pytest.mark.timeout(15)
async def test_log(repo: Path):
    g = _tools(repo)
    r = await g.log({"limit": 5})
    assert "initial" in r["content"][0]["text"]


@pytest.mark.timeout(15)
async def test_branch(repo: Path):
    g = _tools(repo)
    r = await g.branch({})
    assert "main" in r["content"][0]["text"] or "master" in r["content"][0]["text"]


@pytest.mark.timeout(15)
async def test_show(repo: Path):
    g = _tools(repo)
    r = await g.show({"ref": "HEAD"})
    assert "a.txt" in r["content"][0]["text"]


@pytest.mark.timeout(15)
async def test_show_missing_ref_raises(repo: Path):
    g = _tools(repo)
    with pytest.raises(GitError, match="git failed"):
        await g.show({"ref": "deadbeef"})


# --- mutating (require git_write) --------------------------------------------


@pytest.mark.timeout(15)
async def test_commit_denied_without_write(repo: Path):
    (repo / "c.txt").write_text("c\n")
    g = _tools(repo, allow_write=False)
    with pytest.raises(PermissionDeniedError, match="git write"):
        await g.commit({"message": "t"})


@pytest.mark.timeout(15)
async def test_commit_allowed_with_write(repo: Path):
    (repo / "c.txt").write_text("c\n")
    g = _tools(repo, allow_write=True)
    r = await g.commit({"message": "add c"})
    assert "add c" in r["content"][0]["text"] or "master" in r["content"][0]["text"] or "main" in r["content"][0]["text"]
    # Verify the commit landed.
    log = _git(repo, "log", "--oneline")
    assert "add c" in log


@pytest.mark.timeout(15)
async def test_checkout_denied_without_write(repo: Path):
    _git(repo, "branch", "feature")
    g = _tools(repo, allow_write=False)
    with pytest.raises(PermissionDeniedError):
        await g.checkout({"ref": "feature"})


@pytest.mark.timeout(15)
async def test_checkout_allowed_with_write(repo: Path):
    _git(repo, "branch", "feature")
    g = _tools(repo, allow_write=True)
    await g.checkout({"ref": "feature"})
    assert b"* feature" in subprocess.run(
        ["git", "branch"], cwd=repo, capture_output=True, check=True
    ).stdout


@pytest.mark.timeout(15)
async def test_commit_empty_message_raises(repo: Path):
    g = _tools(repo, allow_write=True)
    with pytest.raises(GitError, match="requires non-empty"):
        await g.commit({"message": ""})


# --- audit + events ----------------------------------------------------------


@pytest.mark.timeout(15)
async def test_git_operations_audited(repo: Path):
    g = _tools(repo, allow_write=True)
    await g.status({})
    (repo / "d.txt").write_text("d\n")
    await g.commit({"message": "d"})
    entries = g._audit.read()
    actions = [e["action"] for e in entries]
    assert actions.count("git") >= 2
    allowed = [e for e in entries if e["action"] == "git" and e["allowed"]]
    assert any(e["tool"] == "git_status" for e in allowed)
    assert any(e["tool"] == "git_commit" for e in allowed)


@pytest.mark.timeout(15)
async def test_git_denial_audited(repo: Path):
    g = _tools(repo, allow_write=False)
    with pytest.raises(PermissionDeniedError):
        await g.commit({"message": "x"})
    entries = g._audit.read()
    assert any(e["action"] == "git" and e["allowed"] is False for e in entries)


@pytest.mark.timeout(15)
async def test_git_emits_events(repo: Path):
    bus = EventBus()
    g = _tools(repo, events=bus)
    await g.status({})
    types = [e.type for e in bus.history]
    assert EventType.GIT_COMMAND_STARTED in types
    assert EventType.GIT_COMMAND_FINISHED in types


# --- registry integration ----------------------------------------------------


def test_git_tools_registry(repo: Path):
    policy = PermissionPolicy(writable_roots=[repo])
    audit = AuditLog(repo / "audit.log")
    tools = git_tools(workspace=repo, policy=policy, audit=audit)
    assert {t.name for t in tools} == {
        "git_status", "git_diff", "git_log", "git_branch",
        "git_show", "git_checkout", "git_commit",
    }


@pytest.mark.timeout(15)
async def test_git_cancel_sets_cancellation(repo: Path):
    # The cancel flag is cooperative; verify it is wired and toggleable.
    g = GitTools(
        workspace=repo,
        policy=PermissionPolicy(writable_roots=[repo], allow_git_write=True),
        audit=AuditLog(repo / "audit.log"),
    )
    assert not g._cancel.cancelled
    g.cancel()
    assert g._cancel.cancelled


@pytest.mark.timeout(15)
async def test_git_command_timeout(repo: Path):
    # `git ls-remote` against a fifo that never responds blocks long enough
    # to trigger the timeout. Use a named pipe as a fake remote.
    import os

    from security.policy import GIT_READ

    fifo = repo / "fake.git"
    os.mkfifo(fifo)
    g = GitTools(
        workspace=repo,
        policy=PermissionPolicy(writable_roots=[repo]),
        audit=AuditLog(repo / "audit.log"),
        default_timeout=0.3,
        max_timeout=0.3,
    )
    with pytest.raises(GitError):
        await g._run("git_timeout", ["ls-remote", str(fifo)], GIT_READ, timeout=0.3)
    os.unlink(fifo)
