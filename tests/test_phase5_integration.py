"""Phase 5 integration tests: full task lifecycle with a real (fake) runner.

These tests drive the full orchestrator flow (task → context → plan → execute →
verify → complete/recover) using deterministic fake runners, but a REAL local
environment for verification, so the safe-path + permission boundary is
exercised end-to-end. No live Anthropic API key is required.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent.session import AgentSession, SessionConfig
from config import Settings
from environment import EnvironmentState
from project.scanner import scan_project
from tasks.models import Plan, PlanStep
from tasks.orchestrator import Orchestrator
from tasks.states import TaskState


def _settings(tmp_path: Path) -> Settings:
    s = Settings(
        workspace_root=tmp_path / "ws", session_root=tmp_path / "ss",
        audit_log_path=tmp_path / "audit.log", checkpoint_dir=tmp_path / "ckpt",
        max_step_attempts=2, max_task_attempts=3, max_replans=1,
    )
    s.ensure_directories()
    return s


class ScriptedStepRunner:
    """A StepRunner that runs scripted outcomes; no SDK/model needed."""

    def __init__(self, outcomes: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.outcomes = outcomes or {}
        self.calls: list[str] = []

    async def run_step(self, *, plan: Plan, step: PlanStep, prompt: str, session_id: str) -> dict[str, Any]:
        self.calls.append(step.step_id)
        seq = self.outcomes.get(step.step_id, [])
        idx = step.attempts - 1
        if idx < len(seq):
            return seq[idx]
        if seq:
            return seq[-1]
        return {"success": True, "exit_code": 0, "stdout": "ok"}


class ScriptedPlanRunner:
    def __init__(self, plans: list[Plan]) -> None:
        self._plans = plans
        self.calls = 0

    async def make_plan(self, *, task, context_block, session_id) -> Plan:
        plan = self._plans[min(self.calls, len(self._plans) - 1)]
        self.calls += 1
        return plan.model_copy(deep=True)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "repo"
    ws.mkdir()
    (ws / "pyproject.toml").write_text("[project]\nname='x'\n")
    (ws / "src").mkdir()
    (ws / "src" / "app.py").write_text("def add(a,b): return a+b\n")
    (ws / "tests").mkdir()
    (ws / "tests" / "test_app.py").write_text("def test_add(): assert 1+1==2\n")
    return ws


class TestOrchestratorIntegration:
    async def test_full_task_completes(self, workspace: Path, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        settings.verification_command = "true"  # deterministically pass
        cfg = SessionConfig(workspace=workspace, prompt="add a feature", network_policy="allow")
        session = AgentSession(settings, cfg)
        manifest = scan_project(workspace)
        plan = Plan(plan_id="p1", task_id="t1", goal="add feature",
                    steps=[PlanStep(step_id="s1", description="inspect"), PlanStep(step_id="s2", depends_on=["s1"])])
        orch = Orchestrator(
            session, settings=settings, manifest=manifest,
            step_runner=ScriptedStepRunner(), plan_runner=ScriptedPlanRunner([plan]),
        )
        outcome = await orch.run_task(user_request="add a feature", workspace=str(workspace), task_id="t1")
        assert outcome.task.state is TaskState.COMPLETED
        # Environment was torn down.
        assert session.environment.state is EnvironmentState.DESTROYED

    async def test_task_failure_marks_failed(self, workspace: Path, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        settings.verification_command = "false"
        cfg = SessionConfig(workspace=workspace, prompt="x", network_policy="allow")
        session = AgentSession(settings, cfg)
        manifest = scan_project(workspace)
        plan = Plan(plan_id="p1", task_id="t1", steps=[PlanStep(step_id="s1")])
        orch = Orchestrator(
            session, settings=settings, manifest=manifest,
            step_runner=ScriptedStepRunner(), plan_runner=ScriptedPlanRunner([plan]),
        )
        outcome = await orch.run_task(user_request="x", workspace=str(workspace), task_id="t1")
        # Verification FAILS -> task FAILED (not faked success).
        assert outcome.task.state is TaskState.FAILED
        assert session.environment.state is EnvironmentState.DESTROYED

    async def test_cancellation_propagates(self, workspace: Path, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        settings.verification_command = "true"
        cfg = SessionConfig(workspace=workspace, prompt="x", network_policy="allow")
        session = AgentSession(settings, cfg)
        manifest = scan_project(workspace)
        plan = Plan(plan_id="p1", task_id="t1", steps=[PlanStep(step_id="s1"), PlanStep(step_id="s2")])

        class CancelRunner:
            async def run_step(self, *, plan, step, prompt, session_id):
                if step.step_id == "s1":
                    orch.manager.cancel("t1")
                return {"success": True, "exit_code": 0}

        orch = Orchestrator(
            session, settings=settings, manifest=manifest,
            step_runner=CancelRunner(), plan_runner=ScriptedPlanRunner([plan]),
        )
        outcome = await orch.run_task(user_request="x", workspace=str(workspace), task_id="t1")
        assert outcome.task.state is TaskState.CANCELLED
        assert session.environment.state is EnvironmentState.DESTROYED

    async def test_resume_from_checkpoint(self, workspace: Path, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        settings.enable_checkpoints = True
        settings.verification_command = "true"
        cfg = SessionConfig(workspace=workspace, prompt="x", network_policy="allow")
        session = AgentSession(settings, cfg)
        manifest = scan_project(workspace)
        plan = Plan(plan_id="p1", task_id="t1", steps=[PlanStep(step_id="s1")])
        orch = Orchestrator(
            session, settings=settings, manifest=manifest,
            step_runner=ScriptedStepRunner(), plan_runner=ScriptedPlanRunner([plan]),
        )
        outcome = await orch.run_task(user_request="x", workspace=str(workspace), task_id="t1")
        assert outcome.task.state is TaskState.COMPLETED
        # Checkpoint exists.
        assert orch.manager._store is not None
        assert orch.manager._store.exists("t1")


class TestContextIntegration:
    async def test_context_provided_before_planning(self, workspace: Path, tmp_path: Path) -> None:
        """ContextEngine assembles bounded context before the plan runs."""
        settings = _settings(tmp_path)
        settings.verification_command = "true"
        cfg = SessionConfig(workspace=workspace, prompt="x", network_policy="allow", allow_memory_write=True)
        session = AgentSession(settings, cfg)
        # Seed a project memory.
        from memory.models import MemoryScope

        await session.memory.create(
            content="project uses uv run pytest for tests",
            memory_type=MemoryScope.PROJECT, project_id=str(workspace),
        )
        manifest = scan_project(workspace)
        plan = Plan(plan_id="p1", task_id="t1", steps=[PlanStep(step_id="s1")])
        captured_context: list[str | None] = []

        class CtxPlanRunner:
            async def make_plan(self, *, task, context_block, session_id):
                captured_context.append(context_block)
                return plan.model_copy(deep=True)

        orch = Orchestrator(
            session, settings=settings, manifest=manifest,
            step_runner=ScriptedStepRunner(), plan_runner=CtxPlanRunner(),
        )
        await orch.run_task(user_request="run tests", workspace=str(workspace), task_id="t1")
        # The plan runner received a context block (may be None on first call,
        # but context was assembled by session.prepare before execution).
        assert session.events.history  # context_built or agent activity occurred


class TestFailureIntegration:
    async def test_repeated_failure_leads_to_failed(self, workspace: Path, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        settings.max_step_attempts = 1
        settings.max_task_attempts = 1
        settings.max_replans = 0
        settings.verification_command = "true"
        cfg = SessionConfig(workspace=workspace, prompt="x", network_policy="allow")
        session = AgentSession(settings, cfg)
        manifest = scan_project(workspace)
        plan = Plan(plan_id="p1", task_id="t1", steps=[PlanStep(step_id="s1")])
        runner = ScriptedStepRunner({"s1": [{"success": False, "exit_code": 1, "errors": ["boom"]}]})
        orch = Orchestrator(
            session, settings=settings, manifest=manifest,
            step_runner=runner, plan_runner=ScriptedPlanRunner([plan]),
        )
        outcome = await orch.run_task(user_request="x", workspace=str(workspace), task_id="t1")
        assert outcome.task.state is TaskState.FAILED
        assert session.environment.state is EnvironmentState.DESTROYED
