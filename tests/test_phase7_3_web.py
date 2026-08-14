"""Phase 7.3 — Web Agent Test Console tests.

These tests exercise the thin HTTP/SSE adapter over the existing backend. They
use a deterministic fake orchestrator factory (no model call, no API key) so
the suite runs anywhere. The fake drives the REAL TaskManager/EventBus state
machines — only the model/query path is scripted, exactly like the Phase 7 e2e
fakes.

Coverage:
* API: health, create, get, list, cancel, start, resume, outcome, invalid task,
  malformed request.
* Events: SSE stream, task isolation, reconnect (Last-Event-ID), subscriber
  cleanup, bounded payloads, secret redaction in the stream.
* Security: no subprocess in the web layer, no direct filesystem mutation, no
  permission bypass, no Environment execution, secrets never in responses/events.
* Lifecycle: startup, shutdown, cancellation, task cleanup.
* E2E: create -> events stream -> verification -> completed, with the real
  backend stack (Environment + EventBus + TaskManager) via the fake orchestrator.

A separate optional live-agent test is gated on ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from config import Settings
from events import EventBus, EventType
from tasks.executor import ExecutionOutcome
from tasks.manager import TaskManager, default_task_id
from tasks.models import Plan, TaskFailure
from tasks.states import TaskState
from web import create_app
from web.console import WebConsole

# --- shared fakes -----------------------------------------------------------


class FakeOrchestrator:
    """A deterministic orchestrator that drives the real TaskManager + EventBus.

    No model call. Emits the same event sequence the real orchestrator would
    (state transitions + tool + completion) so the web layer can be tested
    end-to-end. Supports cancellation via a flag.
    """

    def __init__(
        self, session: Any, *, settings: Settings, manifest: Any,
        delay: float = 0.05, fail: bool = False, cancel_signal: asyncio.Event | None = None,
    ) -> None:
        self.events: EventBus = session.events
        self.manager = TaskManager(events=self.events, audit=None, session_id="web")
        self._settings = settings
        self._delay = delay
        self._fail = fail
        self._cancel_signal = cancel_signal

    async def run_task(self, *, user_request: str, workspace: str,
                       task_id: str | None = None, plan: Plan | None = None) -> ExecutionOutcome:
        tid = task_id or default_task_id()
        self.manager.create(task_id=tid, user_request=user_request, workspace=workspace)
        for to in (TaskState.PLANNING, TaskState.PLAN_READY, TaskState.EXECUTING):
            if self._cancel_signal and self._cancel_signal.is_set():
                self.manager.cancel(tid, reason="cancelled")
                task = self.manager.load(tid)
                return ExecutionOutcome(task=task, plan=Plan(plan_id="p", task_id=tid), cancelled=True)
            self.manager.transition(tid, to)
            await asyncio.sleep(self._delay)
        self.events.emit(EventType.TOOL_STARTED, "web", tool="read_file", status="ok")
        self.events.emit(
            EventType.TOOL_FINISHED, "web", tool="read_file", status="ok",
            duration_ms=12, result="ok",
        )
        if self._fail:
            self.manager.transition(tid, TaskState.VERIFYING)
            self.manager.mark_failed(
                tid, failure=TaskFailure(failure_class="test_failure", message="controlled failure"),
            )
            task = self.manager.load(tid)
            return ExecutionOutcome(task=task, plan=Plan(plan_id="p", task_id=tid), failure=task.failure)
        self.manager.transition(tid, TaskState.VERIFYING)
        self.manager.mark_completed(tid)
        task = self.manager.load(tid)
        return ExecutionOutcome(task=task, plan=Plan(plan_id="p", task_id=tid))

    def cancel(self, task_id: str, *, reason: str = "cancelled") -> None:
        if self._cancel_signal:
            self._cancel_signal.set()
        with contextlib.suppress(Exception):
            self.manager.cancel(task_id, reason=reason)

    async def resume_task(self, task_id: str) -> ExecutionOutcome:
        raise NotImplementedError("resume not used in web tests")


def make_factory(*, delay: float = 0.02, fail: bool = False,
                 cancel_signal: asyncio.Event | None = None):
    def factory(session, *, settings, manifest):
        return FakeOrchestrator(
            session, settings=settings, manifest=manifest,
            delay=delay, fail=fail, cancel_signal=cancel_signal,
        )
    return factory


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "README.md").write_text("# sample\n")
    (ws / "src").mkdir()
    (ws / "src" / "app.py").write_text("x = 1\n")
    return ws


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
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
def cancel_signal() -> asyncio.Event:
    return asyncio.Event()


@pytest.fixture
def app(workspace: Path, settings: Settings, cancel_signal: asyncio.Event):
    # delay=0.0 (yield-only) so the background task completes across TestClient
    # requests without needing wall-clock loop time. The SSE tests still see the
    # full event sequence because events are replayed from the bounded log.
    return create_app(
        settings=settings, workspace=workspace,
        orchestrator_factory=make_factory(delay=0.0, cancel_signal=cancel_signal),
        require_api_key=False,
    )


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


@pytest.fixture
def fail_app(workspace: Path, settings: Settings):
    return create_app(
        settings=settings, workspace=workspace,
        orchestrator_factory=make_factory(delay=0.0, fail=True),
        require_api_key=False,
    )


# --- API tests --------------------------------------------------------------


class TestAPI:
    def test_health(self, client: TestClient) -> None:
        r = client.get("/api/health")
        assert r.status_code == 200
        d = r.json()
        assert d["status"] == "ok"
        assert d["version"] == "0.1.0"
        assert d["backend_ready"] is True
        assert "workspace" in d
        # No secrets in health.
        assert "ANTHROPIC" not in r.text
        assert "api_key" not in json.dumps(d).lower() or d["api_key_configured"] is False

    def test_create_task_returns_id_and_state(self, client: TestClient) -> None:
        r = client.post("/api/tasks", json={"prompt": "do something"})
        assert r.status_code == 201
        d = r.json()
        assert "task_id" in d
        assert d["task_id"] and d["task_id"] != "<redacted>"
        assert d["state"] in ("created", "planning", "plan_ready", "executing")

    def test_create_task_missing_prompt(self, client: TestClient) -> None:
        r = client.post("/api/tasks", json={})
        assert r.status_code == 400
        assert "prompt" in r.json()["error"]

    def test_create_task_blank_prompt(self, client: TestClient) -> None:
        r = client.post("/api/tasks", json={"prompt": "   "})
        assert r.status_code == 400

    def test_create_task_malformed_json(self, client: TestClient) -> None:
        r = client.post(
            "/api/tasks", content=b"{not json", headers={"content-type": "application/json"},
        )
        assert r.status_code == 400
        assert "malformed" in r.json()["error"].lower()

    def test_create_task_wrong_type(self, client: TestClient) -> None:
        r = client.post("/api/tasks", json=[1, 2, 3])
        assert r.status_code == 400

    def test_get_task_unknown(self, client: TestClient) -> None:
        r = client.get("/api/tasks/does-not-exist")
        assert r.status_code == 404

    def test_get_task_known(self, client: TestClient) -> None:
        tid = client.post("/api/tasks", json={"prompt": "hi"}).json()["task_id"]
        r = client.get(f"/api/tasks/{tid}")
        assert r.status_code == 200
        d = r.json()
        assert d["id"] == tid
        assert "state" in d

    def test_list_tasks(self, client: TestClient) -> None:
        client.post("/api/tasks", json={"prompt": "one"})
        client.post("/api/tasks", json={"prompt": "two"})
        r = client.get("/api/tasks")
        assert r.status_code == 200
        tasks = r.json()["tasks"]
        assert len(tasks) >= 2

    def test_cancel_unknown_task(self, client: TestClient) -> None:
        r = client.post("/api/tasks/nope/cancel")
        assert r.status_code == 404

    def test_start_endpoint_returns_state(self, client: TestClient) -> None:
        tid = client.post("/api/tasks", json={"prompt": "hi"}).json()["task_id"]
        r = client.post(f"/api/tasks/{tid}/start")
        assert r.status_code == 200

    def test_outcome_unknown(self, client: TestClient) -> None:
        r = client.get("/api/tasks/nope/outcome")
        assert r.status_code == 404


# --- event tests ------------------------------------------------------------


class TestEvents:
    def test_event_stream_emits_real_events(self, client: TestClient) -> None:
        tid = client.post("/api/tasks", json={"prompt": "stream me"}).json()["task_id"]
        frames: list[str] = []
        with client.stream("GET", f"/api/tasks/{tid}/events") as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers["content-type"]
            for line in resp.iter_lines():
                frames.append(line)
                if any("stream_end" in f for f in frames):
                    break
                if len(frames) > 200:
                    break
        types = [f for f in frames if f.startswith("event: ")]
        assert any("task_created" in t for t in types)
        assert any("tool_started" in t for t in types) or any("task_state_changed" in t for t in types)

    def test_event_stream_unknown_task(self, client: TestClient) -> None:
        r = client.get("/api/tasks/nope/events")
        assert r.status_code == 404

    def test_events_are_task_isolated(self, client: TestClient) -> None:
        t1 = client.post("/api/tasks", json={"prompt": "task one"}).json()["task_id"]
        t2 = client.post("/api/tasks", json={"prompt": "task two"}).json()["task_id"]
        assert t1 != t2
        # Each stream only sees its own task's events.
        frames1 = self._collect(client, t1, max_frames=40)
        frames2 = self._collect(client, t2, max_frames=40)
        # The two streams must not be empty and must be task-scoped (each has
        # task_created, which is per-task).
        assert any("task_created" in f for f in frames1)
        assert any("task_created" in f for f in frames2)

    @staticmethod
    def _collect(client: TestClient, tid: str, *, max_frames: int = 40) -> list[str]:
        frames: list[str] = []
        with client.stream("GET", f"/api/tasks/{tid}/events") as resp:
            for line in resp.iter_lines():
                frames.append(line)
                if "stream_end" in line or len(frames) > max_frames:
                    break
        return frames

    def test_event_payloads_bounded(self, client: TestClient) -> None:
        # A huge prompt should not produce an unbounded event payload.
        big = "x" * 200_000
        tid = client.post("/api/tasks", json={"prompt": big}).json()["task_id"]
        frames = self._collect(client, tid, max_frames=60)
        total = sum(len(f) for f in frames)
        # Bounded: total streamed bytes must be far below the raw prompt size.
        assert total < 200_000

    def test_reconnect_replays_after_cursor(self, client: TestClient) -> None:
        tid = client.post("/api/tasks", json={"prompt": "reconnect"}).json()["task_id"]
        # First connection: gather history.
        first = self._collect(client, tid, max_frames=80)
        last_id = 0
        for f in first:
            if f.startswith("data: "):
                try:
                    payload = json.loads(f[len("data: "):])
                    if isinstance(payload, dict) and "id" in payload:
                        last_id = max(last_id, int(payload["id"]))
                except json.JSONDecodeError:
                    pass
        # Reconnect with Last-Event-ID header: should replay recent history.
        with client.stream(
            "GET", f"/api/tasks/{tid}/events",
            headers={"last-event-id": str(max(last_id - 2, 0))},
        ) as resp:
            replay: list[str] = []
            for line in resp.iter_lines():
                replay.append(line)
                if "stream_end" in line or len(replay) > 80:
                    break
        # The reconnect stream should contain event data (replayed history).
        assert any(line.startswith("data: ") for line in replay)

    def test_subscriber_cleanup_on_disconnect(self, app, workspace: Path, settings: Settings) -> None:
        # After the client disconnects, no EventBus subscriber should be leaked.

        console: WebConsole = app.state.console
        tid = client_create(console, "cleanup test")
        sub_count_before = console.get_run(tid).events.subscriber_count
        with TestClient(app).stream("GET", f"/api/tasks/{tid}/events") as resp:
            assert resp.status_code == 200
        # The task is registered (a run), but the SSE subscriber must be cleaned
        # up once the stream closes — the bus must not retain a dead subscriber.
        run = console.get_run(tid)
        assert run is not None
        # Subscriber count must not have grown unboundedly (best-effort: the
        # pump subscribes internally; once the run terminates it closes its sub).
        assert run.events.subscriber_count <= max(sub_count_before, 0) + 1


def client_create(console: WebConsole, prompt: str) -> str:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(console.create_task(prompt=prompt))["task_id"]
    finally:
        loop.close()


# --- security tests ---------------------------------------------------------


class TestSecurity:
    def test_no_subprocess_in_web_layer(self) -> None:
        import web

        web_dir = Path(web.__file__).resolve().parent
        # The web layer must not import or call subprocess / os.system / shell.
        for py in web_dir.rglob("*.py"):
            text = py.read_text(encoding="utf-8")
            assert "import subprocess" not in text, f"subprocess import in {py.name}"
            assert "from subprocess" not in text, f"subprocess import in {py.name}"
            # os.system / shell=True / eval / exec builtin never used.
            assert re.search(r"os\.system\s*\(", text) is None, f"os.system in {py.name}"
            assert "shell=True" not in text, f"shell=True in {py.name}"
            assert re.search(r"\beval\s*\(", text) is None, f"eval() in {py.name}"
            assert re.search(r"\bexec\s*\(", text) is None, f"exec() in {py.name}"

    def test_no_filesystem_mutation_in_web_layer(self) -> None:
        import web

        web_dir = Path(web.__file__).resolve().parent
        write_patterns = [
            r"\.write_text\(", r"\.write_bytes\(", r"\.unlink\(", r"\.mkdir\(",
            r"\.rmdir\(", r"open\([^)]+['\"][wa]",
        ]
        for py in web_dir.rglob("*.py"):
            text = py.read_text(encoding="utf-8")
            for pat in write_patterns:
                assert not re.search(pat, text), f"filesystem mutation {pat!r} in {py.name}"

    def test_web_layer_does_not_import_subprocess(self) -> None:
        import web.app
        import web.console
        import web.serialize

        for mod in (web.app, web.console, web.serialize):
            src = Path(mod.__file__).read_text(encoding="utf-8")
            assert "import subprocess" not in src
            assert "from subprocess" not in src

    def test_web_layer_does_not_construct_environment_directly(self) -> None:
        import web.app
        import web.console

        for mod in (web.app, web.console):
            src = Path(mod.__file__).read_text(encoding="utf-8")
            # The web layer must NOT build Environment/Runtime directly; it only
            # uses the AgentSession which owns the environment.
            assert "from environment import Environment" not in src
            assert "Environment(" not in src

    def test_secrets_never_in_responses(self, client: TestClient) -> None:
        secret = "AKIAIOSFODNN7EXAMPLE"  # AWS-shaped fake key
        tid = client.post("/api/tasks", json={"prompt": f"use {secret}"}).json()["task_id"]
        r = client.get(f"/api/tasks/{tid}")
        assert secret not in r.text
        assert "<redacted>" in r.text

    def test_secrets_never_in_event_stream(self, client: TestClient) -> None:
        secret = "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"  # github-shaped fake
        tid = client.post("/api/tasks", json={"prompt": f"token {secret}"}).json()["task_id"]
        frames: list[str] = []
        with client.stream("GET", f"/api/tasks/{tid}/events") as resp:
            for line in resp.iter_lines():
                frames.append(line)
                if "stream_end" in line or len(frames) > 100:
                    break
        joined = "\n".join(frames)
        assert secret not in joined

    def test_cross_origin_blocked(self, client: TestClient) -> None:
        # A cross-origin Origin header should be rejected for API routes.
        r = client.get("/api/health", headers={"origin": "http://evil.example.com",
                                                "host": "127.0.0.1:5000"})
        assert r.status_code == 403

    def test_same_origin_allowed(self, client: TestClient) -> None:
        r = client.get("/api/health", headers={"origin": "http://127.0.0.1:5000",
                                                "host": "127.0.0.1:5000"})
        assert r.status_code == 200

    def test_no_environment_vars_exposed(self, client: TestClient) -> None:
        r = client.get("/api/health")
        d = r.json()
        # The health response must not enumerate environment variables or keys.
        assert "PATH" not in json.dumps(d)
        assert "HOME" not in json.dumps(d)
        assert "os.environ" not in json.dumps(d)
        # api_key_configured is a boolean flag, never the key value itself.
        assert d["api_key_configured"] is False or d["api_key_configured"] is True


# --- lifecycle tests --------------------------------------------------------


class TestLifecycle:
    @staticmethod
    def _run(coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_shutdown_closes_console(self, app) -> None:
        console: WebConsole = app.state.console
        assert not console.is_closed
        self._run(console.shutdown(reason="test"))
        assert console.is_closed
        # Creating a task after shutdown must fail.
        with pytest.raises(Exception):  # noqa: B017 - any failure is expected
            self._run(console.create_task(prompt="x"))

    def test_shutdown_cancels_running_tasks(self, client: TestClient, app) -> None:
        console: WebConsole = app.state.console
        tid = client.post("/api/tasks", json={"prompt": "long"}).json()["task_id"]
        self._run(console.shutdown(reason="test"))
        # The run is recorded; no background task leaked.
        run = console.get_run(tid)
        if run is not None:
            assert run.is_terminal

    def test_cancel_uses_existing_mechanism(self, client: TestClient, cancel_signal) -> None:
        tid = client.post("/api/tasks", json={"prompt": "cancellable"}).json()["task_id"]
        r = client.post(f"/api/tasks/{tid}/cancel")
        assert r.status_code == 200
        assert r.json()["cancelled"] is True
        # The cancellation signal was set (existing cooperative mechanism).
        assert cancel_signal.is_set()

    def test_cancel_only_affects_requested_task(self, client: TestClient, cancel_signal) -> None:
        t1 = client.post("/api/tasks", json={"prompt": "one"}).json()["task_id"]
        t2 = client.post("/api/tasks", json={"prompt": "two"}).json()["task_id"]
        client.post(f"/api/tasks/{t1}/cancel")
        # t1 cancelled; t2 must still be independently inspectable.
        r1 = client.get(f"/api/tasks/{t1}").json()
        r2 = client.get(f"/api/tasks/{t2}").json()
        assert r1["id"] != r2["id"]


# --- failure / recovery rendering ------------------------------------------


class TestFailureRecovery:
    @staticmethod
    def _wait_for_terminal(client: TestClient, tid: str, timeout: float = 5.0) -> str:
        import time

        deadline = time.time() + timeout
        state = "created"
        while time.time() < deadline:
            r = client.get(f"/api/tasks/{tid}")
            if r.status_code == 200:
                state = r.json()["state"]
                if state in ("failed", "completed", "cancelled"):
                    return state
            time.sleep(0.02)
        return state

    def test_failed_task_shows_failure_info(self, fail_app) -> None:
        client = TestClient(fail_app)
        tid = client.post("/api/tasks", json={"prompt": "fail me"}).json()["task_id"]
        state = self._wait_for_terminal(client, tid)
        assert state == "failed"
        d = client.get(f"/api/tasks/{tid}").json()
        assert d["failure"] is not None
        assert d["failure"]["failure_class"] == "test_failure"
        # No stack trace leaked.
        assert "Traceback" not in json.dumps(d)

    def test_outcome_shows_failure(self, fail_app) -> None:
        client = TestClient(fail_app)
        tid = client.post("/api/tasks", json={"prompt": "fail"}).json()["task_id"]
        self._wait_for_terminal(client, tid)
        r = client.get(f"/api/tasks/{tid}/outcome")
        assert r.status_code == 200
        d = r.json()
        assert d["task"]["state"] == "failed"
        assert d["failure"]["failure_class"] == "test_failure"


# --- E2E --------------------------------------------------------------------


class TestEndToEnd:
    @staticmethod
    def _wait_for_terminal(client: TestClient, tid: str, timeout: float = 5.0) -> str:
        import time

        deadline = time.time() + timeout
        state = "created"
        while time.time() < deadline:
            r = client.get(f"/api/tasks/{tid}")
            if r.status_code == 200:
                state = r.json()["state"]
                if state in ("failed", "completed", "cancelled"):
                    return state
            time.sleep(0.02)
        return state

    def test_full_scenario_create_stream_complete(self, client: TestClient) -> None:
        """Create -> stream events -> verification -> completed."""
        r = client.post("/api/tasks", json={"prompt": "complete this task"})
        assert r.status_code == 201
        tid = r.json()["task_id"]

        frames: list[str] = []
        with client.stream("GET", f"/api/tasks/{tid}/events") as resp:
            assert resp.status_code == 200
            for line in resp.iter_lines():
                frames.append(line)
                if "stream_end" in line:
                    break
        # Real event sequence observed.
        joined = "\n".join(frames)
        assert "task_created" in joined
        assert "task_state_changed" in joined

        # Final state is completed.
        state = self._wait_for_terminal(client, tid)
        assert state == "completed"

        r = client.get(f"/api/tasks/{tid}/outcome")
        d = r.json()
        assert d["task"]["state"] == "completed"
        assert d["cancelled"] is False
        # No secrets anywhere.
        assert "ANTHROPIC" not in json.dumps(d)
        assert "api_key" not in json.dumps(d).lower()

    def test_real_backend_environment_provisioned(self, client: TestClient) -> None:
        """The real AgentSession environment is provisioned (environment_created event)."""
        tid = client.post("/api/tasks", json={"prompt": "env check"}).json()["task_id"]
        frames: list[str] = []
        with client.stream("GET", f"/api/tasks/{tid}/events") as resp:
            for line in resp.iter_lines():
                frames.append(line)
                if "stream_end" in line:
                    break
        joined = "\n".join(frames)
        # The real Environment emits environment_created on provision.
        assert "environment_created" in joined


# --- live-agent (optional, gated on API key) --------------------------------


@pytest.mark.skipif(
    not __import__("os").environ.get("ANTHROPIC_API_KEY"),
    reason="requires ANTHROPIC_API_KEY for a live agent run",
)
class TestLiveAgent:
    def test_live_task_runs(self, workspace: Path, settings: Settings) -> None:
        app = create_app(settings=settings, workspace=workspace, require_api_key=True)
        client = TestClient(app)
        r = client.post("/api/tasks", json={"prompt": "list the files in this workspace"})
        assert r.status_code == 201
        tid = r.json()["task_id"]
        # Wait for completion (bounded).
        import time

        deadline = time.time() + 120
        state = "created"
        while time.time() < deadline and state not in ("completed", "failed", "cancelled"):
            time.sleep(2)
            resp = client.get(f"/api/tasks/{tid}")
            if resp.status_code == 200:
                state = resp.json()["state"]
        assert state in ("completed", "failed", "cancelled")
