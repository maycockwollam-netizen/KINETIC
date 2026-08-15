"""Phase 7.3+ — real web surfaces: LLM config, agents, automations, files,
interactive approvals.

These tests cover the new backend endpoints added to replace the demo/mock
front-end behavior. They use the same deterministic FakeOrchestrator factory as
the Phase 7.3 web tests (no model call, no API key). The home directory for
the persistent JSON stores is redirected to the temp dir so tests don't touch
the real user data dir.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from config import Settings
from events import EventBus
from tasks.executor import ExecutionOutcome
from tasks.manager import TaskManager, default_task_id
from tasks.models import Plan
from tasks.states import TaskState
from web import create_app


class FakeOrchestrator:
    def __init__(self, session: Any, *, settings: Settings, manifest: Any,
                 delay: float = 0.0) -> None:
        self.events: EventBus = session.events
        self.manager = TaskManager(events=self.events, audit=None, session_id="web")
        self._delay = delay

    async def run_task(self, *, user_request: str, workspace: str,
                       task_id: str | None = None, plan: Plan | None = None) -> ExecutionOutcome:
        tid = task_id or default_task_id()
        self.manager.create(task_id=tid, user_request=user_request, workspace=workspace)
        for to in (TaskState.PLANNING, TaskState.PLAN_READY, TaskState.EXECUTING, TaskState.VERIFYING):
            self.manager.transition(tid, to)
            await asyncio.sleep(self._delay)
        self.manager.mark_completed(tid)
        return ExecutionOutcome(task=self.manager.load(tid), plan=Plan(plan_id="p", task_id=tid))

    def cancel(self, task_id: str, *, reason: str = "cancelled") -> None:
        with contextlib.suppress(Exception):
            self.manager.cancel(task_id, reason=reason)

    async def resume_task(self, task_id: str) -> ExecutionOutcome:
        raise NotImplementedError


def _factory(delay: float = 0.0):
    def factory(session, *, settings, manifest):
        return FakeOrchestrator(session, settings=settings, manifest=manifest, delay=delay)
    return factory


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "README.md").write_text("# sample\n")
    return ws


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    # Redirect the home data dir so the JSON stores land in the temp dir.
    monkeypatch.setenv("HOME", str(tmp_path))
    s = Settings(
        workspace_root=tmp_path / "workspaces",
        session_root=tmp_path / "sessions",
        audit_log_path=tmp_path / "audit.log",
        memory_db_path=tmp_path / "memory.db",
        checkpoint_dir=tmp_path / "checkpoints",
        web_event_poll_timeout=0.02,
    )
    s.ensure_directories()
    return s


@pytest.fixture
def app(workspace: Path, settings: Settings):
    return create_app(
        settings=settings, workspace=workspace,
        orchestrator_factory=_factory(),
        require_api_key=False,
    )


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


class TestLLMConfig:
    def test_get_llm_config_never_returns_key(self, client: TestClient) -> None:
        r = client.get("/api/llm")
        assert r.status_code == 200
        d = r.json()
        assert "api_key_set" in d and isinstance(d["api_key_set"], bool)
        assert "api_key" not in d or d.get("api_key") is None

    def test_set_llm_config_base_url_and_key(self, client: TestClient) -> None:
        r = client.put("/api/llm", json={
            "base_url": "https://proxy.example.com", "api_key": "sk-test-123",
            "model": "claude-sonnet-4-5-20250929",
        })
        assert r.status_code == 200
        d = r.json()
        assert d["base_url"] == "https://proxy.example.com"
        assert d["api_key_set"] is True
        # The key is never echoed back.
        assert "sk-test-123" not in r.text

    def test_invalid_base_url_rejected(self, client: TestClient) -> None:
        r = client.put("/api/llm", json={"base_url": "not-a-url"})
        assert r.status_code == 400

    def test_interactive_approval_toggle(self, client: TestClient) -> None:
        r = client.put("/api/llm", json={"interactive_approval": True})
        assert r.status_code == 200
        assert r.json()["interactive_approval"] is True


class TestAgents:
    def test_create_list_get_delete(self, client: TestClient) -> None:
        r = client.post("/api/agents", json={"name": "R", "description": "res", "model": "Sonnet 5", "system_prompt": "x"})
        assert r.status_code == 201
        agent = r.json()
        aid = agent["id"]
        assert agent["name"] == "R"
        # list
        assert client.get("/api/agents").json()["agents"][0]["id"] == aid
        # get
        assert client.get(f"/api/agents/{aid}").json()["name"] == "R"
        # update
        r = client.put(f"/api/agents/{aid}", json={"name": "R2"})
        assert r.json()["name"] == "R2"
        # delete
        assert client.delete(f"/api/agents/{aid}").status_code == 200
        assert client.get(f"/api/agents/{aid}").status_code == 404

    def test_unknown_agent_404(self, client: TestClient) -> None:
        assert client.get("/api/agents/nope").status_code == 404
        assert client.delete("/api/agents/nope").status_code == 404


class TestAutomations:
    def test_create_run_delete(self, client: TestClient) -> None:
        r = client.post("/api/automations", json={"name": "Daily", "cron": "0 8 * * *", "prompt": "hello"})
        assert r.status_code == 201
        a = r.json()
        aid = a["id"]
        # run now -> creates a real task
        r2 = client.post(f"/api/automations/{aid}/run")
        assert r2.status_code == 201
        run_body = r2.json()
        assert "task_id" in run_body
        # last_run populated honestly (the GET response is scrubbed by the
        # SecretDetector — a 32-hex task_id looks like a token — so compare via
        # the un-scrubbed automation_id instead, and assert last_run_at is set).
        a2 = client.get(f"/api/automations/{aid}").json()
        assert a2["last_run_at"] is not None
        assert a2["id"] == aid
        assert client.delete(f"/api/automations/{aid}").status_code == 200

    def test_run_unknown_404(self, client: TestClient) -> None:
        assert client.post("/api/automations/nope/run").status_code == 404


class TestFiles:
    def test_upload_list_delete(self, client: TestClient, workspace: Path) -> None:
        r = client.post("/api/files", files={"file": ("note.txt", b"hello world", "text/plain")})
        assert r.status_code == 201
        f = r.json()
        assert f["name"] == "note.txt"
        assert f["size"] == 11
        # blob actually written into the workspace upload dir
        assert (workspace / ".kinetic_uploads" / f"{f['id']}_note.txt").exists()
        # list
        assert client.get("/api/files").json()["files"][0]["id"] == f["id"]
        # delete
        assert client.delete(f"/api/files/{f['id']}").status_code == 200
        assert not (workspace / ".kinetic_uploads" / f"{f['id']}_note.txt").exists()

    def test_invalid_name_rejected(self, client: TestClient) -> None:
        r = client.post("/api/files", files={"file": ("../evil.txt", b"x", "text/plain")})
        assert r.status_code == 400

    def test_no_file_400(self, client: TestClient) -> None:
        assert client.post("/api/files").status_code == 400


class TestApprovals:
    def test_pending_list_and_resolve_unknown(self, client: TestClient) -> None:
        # create a task to get a real task_id
        tid = client.post("/api/tasks", json={"prompt": "do x"}).json()["task_id"]
        # No pending approvals initially.
        assert client.get(f"/api/tasks/{tid}/approvals").json()["approvals"] == []
        # Resolving a non-existent request returns 404.
        assert client.post(f"/api/tasks/{tid}/approvals/abc/resolve", json={"allow": True}).status_code == 404


class TestCreateTaskOverrides:
    def test_create_task_with_agent_id(self, client: TestClient) -> None:
        aid = client.post("/api/agents", json={"name": "A", "model": "Sonnet 5"}).json()["id"]
        r = client.post("/api/tasks", json={"prompt": "hi", "agent_id": aid})
        assert r.status_code == 201
        assert "task_id" in r.json()

    def test_create_task_unknown_agent(self, client: TestClient) -> None:
        r = client.post("/api/tasks", json={"prompt": "hi", "agent_id": "ghost"})
        assert r.status_code == 400

    def test_create_task_with_repair_flag(self, client: TestClient) -> None:
        r = client.post("/api/tasks", json={"prompt": "hi", "enable_repair": True})
        assert r.status_code == 201
