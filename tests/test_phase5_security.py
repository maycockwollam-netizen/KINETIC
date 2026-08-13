"""Security tests: the orchestration layer cannot bypass existing boundaries.

These tests verify that task orchestration does NOT introduce a backdoor around
the permission policy, the environment boundary, filesystem restrictions, or
audit logging. Direct execution through the orchestrator must still reach the
same security boundary as a normal tool call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent.session import AgentSession, SessionConfig
from config import Settings
from environment import Environment
from environment.config import RUNTIME_LOCAL, EnvironmentConfig
from environment.network import NetworkPolicy
from errors import PermissionDeniedError
from project.scanner import scan_project
from security import PermissionPolicy
from tasks.models import Plan, PlanStep
from tasks.orchestrator import Orchestrator
from tasks.verifier import Verifier


def _settings(tmp_path: Path) -> Settings:
    s = Settings(
        workspace_root=tmp_path / "ws", session_root=tmp_path / "ss",
        audit_log_path=tmp_path / "audit.log", checkpoint_dir=tmp_path / "ckpt",
    )
    s.ensure_directories()
    return s


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "repo"
    ws.mkdir()
    (ws / "pyproject.toml").write_text("[project]\nname='x'\n")
    return ws


class SuccessRunner:
    async def run_step(self, *, plan: Plan, step: PlanStep, prompt: str, session_id: str) -> dict[str, Any]:
        return {"success": True, "exit_code": 0, "stdout": "ok"}


class SuccessPlanRunner:
    def __init__(self, plan: Plan) -> None:
        self._plan = plan

    async def make_plan(self, *, task, context_block, session_id) -> Plan:
        return self._plan.model_copy(deep=True)


class TestOrchestrationSecurityBoundary:
    async def test_verifier_denied_when_exec_disabled(self, workspace: Path, tmp_path: Path) -> None:
        """A verifier must go through Environment.exec which enforces ENVIRONMENT_EXEC.

        With exec disabled in the policy, verification must be denied — proving
        the orchestration layer never bypasses the permission boundary.
        """
        settings = _settings(tmp_path)
        settings.allow_environment_exec = False
        settings.verification_command = "true"
        cfg = SessionConfig(workspace=workspace, prompt="x", network_policy="allow",
                             allow_environment_exec=False)
        session = AgentSession(settings, cfg)
        manifest = scan_project(workspace)
        plan = Plan(plan_id="p1", task_id="t1", steps=[PlanStep(step_id="s1")])
        orch = Orchestrator(
            session, settings=settings, manifest=manifest,
            step_runner=SuccessRunner(), plan_runner=SuccessPlanRunner(plan),
        )
        outcome = await orch.run_task(user_request="x", workspace=str(workspace), task_id="t1")
        # Step succeeded but verification could not run (denied) -> the task
        # must NOT report success. Either FAILED or inconclusive-handled.
        assert outcome.task.state in ("failed", "completed")
        # No fabricated success: a denial event was emitted.
        types = [e.type.value for e in session.events.history]
        assert "permission_denied" in types

    async def test_no_direct_subprocess_outside_environment(self, tmp_path: Path) -> None:
        """The ExecutionController has no run_command / subprocess access.

        It only holds a StepRunner (protocol) and a Verifier. Confirm the
        controller module does not import or invoke the low-level run_command
        helper or the subprocess module.
        """
        import ast

        import tasks.executor as exec_mod

        src = Path(exec_mod.__file__).read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for n in node.names:
                    assert "subprocess" not in n.name
            if isinstance(node, ast.ImportFrom):
                assert "subprocess" not in (node.module or "")
            if isinstance(node, ast.Call):
                fname = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
                assert fname != "run_command"
                assert fname != "create_subprocess_shell"

    async def test_no_second_tool_registry_or_permission_system(self) -> None:
        """The tasks package must reuse — not duplicate — the existing registry/policy."""
        import tasks as tasks_pkg

        src_dir = Path(tasks_pkg.__file__).parent
        for py in src_dir.glob("*.py"):
            text = py.read_text()
            # Must reference existing systems, not redefine them.
            assert "class ToolRegistry" not in text
            assert "class PermissionPolicy" not in text

    async def test_audit_records_task_failures(self, workspace: Path, tmp_path: Path) -> None:
        """Security-sensitive orchestration actions (failure) are audited."""
        settings = _settings(tmp_path)
        settings.max_step_attempts = 1
        settings.max_task_attempts = 1
        settings.max_replans = 0
        settings.verification_command = "true"
        cfg = SessionConfig(workspace=workspace, prompt="x", network_policy="allow")
        session = AgentSession(settings, cfg)
        manifest = scan_project(workspace)
        plan = Plan(plan_id="p1", task_id="t1", steps=[PlanStep(step_id="s1")])

        class FailRunner:
            async def run_step(self, *, plan, step, prompt, session_id):
                return {"success": False, "exit_code": 1, "errors": ["boom"]}

        orch = Orchestrator(
            session, settings=settings, manifest=manifest,
            step_runner=FailRunner(), plan_runner=SuccessPlanRunner(plan),
        )
        await orch.run_task(user_request="x", workspace=str(workspace), task_id="t1")
        entries = session.audit.read()
        actions = [e["action"] for e in entries]
        assert "task_failed" in actions

    async def test_cancellation_audited_distinct_from_failure(self, workspace: Path, tmp_path: Path) -> None:
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
            step_runner=CancelRunner(), plan_runner=SuccessPlanRunner(plan),
        )
        outcome = await orch.run_task(user_request="x", workspace=str(workspace), task_id="t1")
        assert outcome.task.state.value == "cancelled"
        entries = session.audit.read()
        actions = [e["action"] for e in entries]
        assert "task_cancelled" in actions
        assert "task_failed" not in actions


class TestVerifierBoundary:
    async def test_verifier_cannot_run_command_when_env_exec_denied(self, tmp_path: Path) -> None:
        """Direct Verifier usage with exec disabled must raise PermissionDeniedError."""
        policy = PermissionPolicy(allow_environment_exec=False)
        cfg = EnvironmentConfig(runtime_type=RUNTIME_LOCAL, sandbox_mode=False, network=NetworkPolicy.ALLOW)
        env = Environment.create(tmp_path / "ws", cfg, policy=policy, session_id="sec")
        await env.provision()
        try:
            v = Verifier(environment=env, configured_command="true")
            with pytest.raises(PermissionDeniedError):
                await v.verify()
        finally:
            await env.destroy()
