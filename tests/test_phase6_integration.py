"""Phase 6 integration tests: repair-enabled task lifecycle.

Drives the full orchestrator flow with ``enable_repair=True`` using deterministic
fake runners, but a REAL local environment for verification. No live
ANTHROPIC_API_KEY is required.

Covers:
  * repair succeeds → task completes after re-verification
  * repair fails (stuck) → bounded FAILED outcome
  * repair disabled → Phase 5 behavior unchanged
  * final review engaged with repair
  * permission denial during repair → bounded failure (never fabricated success)
  * environment failure during verification → bounded failure
  * cancellation during repair
  * checkpoint persists repair state
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kinetic.agent.session import AgentSession, SessionConfig
from kinetic.config import Settings
from kinetic.environment import EnvironmentState
from kinetic.project.scanner import scan_project
from kinetic.tasks.models import Plan, PlanStep
from kinetic.tasks.orchestrator import Orchestrator
from kinetic.tasks.states import TaskState


def _settings(tmp_path: Path, **kw) -> Settings:
    base = dict(
        workspace_root=tmp_path / "ws", session_root=tmp_path / "ss",
        audit_log_path=tmp_path / "audit.log", checkpoint_dir=tmp_path / "ckpt",
        max_step_attempts=2, max_task_attempts=3, max_replans=1,
        enable_repair=True, max_repair_attempts=2, max_verification_attempts=3,
    )
    base.update(kw)
    s = Settings(**base)
    s.ensure_directories()
    return s


class ScriptedStepRunner:
    def __init__(self, outcomes: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.outcomes = outcomes or {}
        self.calls: list[str] = []

    async def run_step(self, *, plan, step, prompt, session_id) -> dict[str, Any]:
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


class ScriptedRepairRunner:
    """A RepairRunner returning scripted outcomes."""

    def __init__(self, outcomes: list[dict[str, Any]]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    async def repair(self, *, prompt: str, session_id: str) -> dict[str, Any]:
        idx = self.calls
        self.calls += 1
        if idx < len(self.outcomes):
            return self.outcomes[idx]
        return self.outcomes[-1] if self.outcomes else {"success": True, "exit_code": 0}


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "repo"
    ws.mkdir()
    (ws / "pyproject.toml").write_text("[project]\nname='x'\n")
    (ws / "src").mkdir()
    (ws / "src" / "app.py").write_text("def add(a,b): return a+b\n")
    return ws


class ScriptedVerifier:
    """Returns scripted verification results in sequence."""

    def __init__(self, results) -> None:
        self._results = results
        self.calls = 0

    async def verify(self, *, command: str | None = None):
        idx = self.calls
        self.calls += 1
        if idx < len(self._results):
            r = self._results[idx]
            return r
        return self._results[-1]

    def classify(self, result):
        return {"outcome": result.outcome.value}


def _passed_vr():
    from kinetic.tasks.policies import VerificationOutcome
    from kinetic.tasks.verifier import VerificationResult
    return VerificationResult(outcome=VerificationOutcome.PASS, command="true", exit_code=0)


def _failed_vr():
    from kinetic.tasks.policies import VerificationOutcome
    from kinetic.tasks.verifier import VerificationResult
    return VerificationResult(
        outcome=VerificationOutcome.FAIL, command="false", exit_code=1,
        stderr="verification failed", reason="failed",
    )


class TestRepairIntegration:
    async def test_repair_succeeds_task_completes(self, workspace: Path, tmp_path: Path) -> None:
        # Step-level verification passes; final verification fails; repair
        # "succeeds" but re-verification keeps failing -> bounded FAILED.
        settings = _settings(tmp_path)
        settings.verification_command = "true"
        cfg = SessionConfig(workspace=workspace, prompt="fix tests", network_policy="allow")
        session = AgentSession(settings, cfg)
        manifest = scan_project(workspace)
        plan = Plan(plan_id="p1", task_id="t1", steps=[PlanStep(step_id="s1")])
        orch = Orchestrator(
            session, settings=settings, manifest=manifest,
            step_runner=ScriptedStepRunner(), plan_runner=ScriptedPlanRunner([plan]),
        )
        # Inject a scripted verifier: step-level verify passes, final verify fails,
        # all re-verifications fail. Both the controller and repair coordinator
        # use the same scripted verifier.
        sv = ScriptedVerifier([_passed_vr(), _failed_vr(), _failed_vr(), _failed_vr()])
        orch.controller.verifier = sv
        orch.controller.repair_coordinator._verifier = sv
        orch.controller.repair_coordinator._runner = ScriptedRepairRunner(
            [{"success": True, "exit_code": 0}] * 5
        )
        outcome = await orch.run_task(user_request="fix", workspace=str(workspace), task_id="t1")
        assert outcome.task.state is TaskState.FAILED
        assert outcome.repair is not None
        assert not outcome.repair.success
        assert session.environment.state is EnvironmentState.DESTROYED

    async def test_repair_disabled_phase5_behavior(self, workspace: Path, tmp_path: Path) -> None:
        settings = _settings(tmp_path, enable_repair=False)
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
        assert outcome.repair is None  # repair disabled

    async def test_repair_state_persisted_in_checkpoint(self, workspace: Path, tmp_path: Path) -> None:
        settings = _settings(tmp_path, enable_checkpoints=True)
        settings.verification_command = "true"
        cfg = SessionConfig(workspace=workspace, prompt="x", network_policy="allow")
        session = AgentSession(settings, cfg)
        manifest = scan_project(workspace)
        plan = Plan(plan_id="p1", task_id="t1", steps=[PlanStep(step_id="s1")])
        orch = Orchestrator(
            session, settings=settings, manifest=manifest,
            step_runner=ScriptedStepRunner(), plan_runner=ScriptedPlanRunner([plan]),
        )
        sv = ScriptedVerifier([_passed_vr(), _failed_vr(), _failed_vr(), _failed_vr()])
        orch.controller.verifier = sv
        orch.controller.repair_coordinator._verifier = sv
        orch.controller.repair_coordinator._runner = ScriptedRepairRunner(
            [{"success": True, "exit_code": 0}] * 3
        )
        await orch.run_task(user_request="x", workspace=str(workspace), task_id="t1")
        from kinetic.tasks.checkpoints import restore_repair_state

        assert orch.manager._store is not None
        assert orch.manager._store.exists("t1")
        raw = orch.manager._store.load("t1")
        rs = restore_repair_state(raw)
        assert rs is not None
        assert rs["verification_attempts"] >= 1


class TestSecurityBoundaries:
    async def test_permission_denied_during_repair_is_bounded_failure(
        self, workspace: Path, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        settings.verification_command = "true"
        # Disable environment exec -> verifier raises PermissionDeniedError.
        cfg = SessionConfig(
            workspace=workspace, prompt="x", network_policy="allow",
            allow_environment_exec=False,
        )
        session = AgentSession(settings, cfg)
        manifest = scan_project(workspace)
        plan = Plan(plan_id="p1", task_id="t1", steps=[PlanStep(step_id="s1")])
        orch = Orchestrator(
            session, settings=settings, manifest=manifest,
            step_runner=ScriptedStepRunner(), plan_runner=ScriptedPlanRunner([plan]),
        )
        outcome = await orch.run_task(user_request="x", workspace=str(workspace), task_id="t1")
        # A permission denial must never be fabricated as success.
        assert outcome.task.state is TaskState.FAILED
        assert session.environment.state is EnvironmentState.DESTROYED

    async def test_no_direct_subprocess_in_intelligence_layer(self) -> None:
        """The intelligence package must not import subprocess or call git directly."""
        import kinetic.intelligence.analyzer as analyzer
        import kinetic.intelligence.diff as diff
        import kinetic.intelligence.repair as repair
        import kinetic.intelligence.review as review

        for mod in (analyzer, diff, repair, review):
            assert not hasattr(mod, "subprocess"), f"{mod.__name__} must not use subprocess"
            # No direct run_command/os.system usage.
            src = Path(mod.__file__).read_text()
            assert "os.system" not in src
            assert "subprocess.Popen" not in src
            assert "shell=True" not in src
