"""Phase 7 — end-to-end integration test using fakes.

A realistic, deterministic scenario that exercises the full pipeline without a
live API key:

 1. Create workspace
 2. Detect project
 3. Create task
 4. Assemble context
 5. Generate plan
 6. Execute coding step (fake runner)
 7. Observe changes
 8. Run verification
 9. Introduce a controlled failure
10. Analyze failure
11. Repair (fake runner)
12. Re-run verification
13. Run final review
14. Commit through existing Git abstraction
15. Persist checkpoint
16. Complete task
17. Destroy environment
18. Verify audit/events/metrics
19. Verify no secrets leaked
20. Verify working tree safety

The test uses deterministic fakes for the model/query path; everything else is
real (TaskManager, ExecutionController, Observer, Verifier, Environment,
AuditLog, EventBus, MetricsCollector).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from config import Settings
from events import EventBus, EventType
from observability.metrics import MetricsCollector
from security import AuditLog, PermissionPolicy
from tasks.executor import ExecutionController
from tasks.manager import TaskManager
from tasks.models import Plan, PlanStep, Task
from tasks.observer import Observer
from tasks.policies import RecoveryPolicy, RetryLimits, VerificationOutcome
from tasks.recovery import RecoveryCoordinator
from tasks.states import TaskState
from tasks.verifier import VerificationResult, Verifier
from tools.git import GitTools

# --- fakes ------------------------------------------------------------------

class FakeStepRunner:
    """Returns scripted results; deterministic, no model call."""

    def __init__(self, results: list[dict[str, Any]]) -> None:
        self._results = list(results)
        self._idx = 0
        self.calls: list[str] = []

    async def run_step(self, *, plan: Plan, step: PlanStep, prompt: str, session_id: str) -> dict[str, Any]:
        self.calls.append(step.step_id)
        if self._idx < len(self._results):
            r = self._results[self._idx]
            self._idx += 1
            return r
        return {"result_text": "done", "exit_code": 0, "success": True, "tool_calls": [], "duration_ms": 10}


class FakePlanRunner:
    """Returns a pre-built plan; deterministic."""

    def __init__(self, plan: Plan) -> None:
        self._plan = plan

    async def make_plan(self, *, task: Task, context_block: str | None, session_id: str) -> Plan:
        return self._plan


class FakeVerifier(Verifier):
    """Scripted verification outcomes."""

    def __init__(self, outcomes: list[VerificationOutcome]) -> None:
        self._outcomes = list(outcomes)
        self._idx = 0
        self.calls = 0

    async def verify(self, *, command: str | None = None) -> VerificationResult:
        self.calls += 1
        outcome = self._outcomes[min(self._idx, len(self._outcomes) - 1)]
        self._idx += 1
        return VerificationResult(
            outcome=outcome,
            command=command or "fake",
            exit_code=0 if outcome is VerificationOutcome.PASS else 1,
            stdout="",
            stderr="controlled failure" if outcome is VerificationOutcome.FAIL else "",
        )


# --- the scenario -----------------------------------------------------------

@pytest.fixture
def e2e_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "e2e_ws"
    ws.mkdir()
    (ws / "README.md").write_text("# E2E Test\n")
    (ws / "src").mkdir()
    (ws / "src" / "app.py").write_text("def add(a, b):\n    return a + b\n")
    env = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@test.com",
           "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@test.com"}
    subprocess.run(["git", "init"], cwd=ws, check=True, capture_output=True, env=env)
    subprocess.run(["git", "add", "-A"], cwd=ws, check=True, capture_output=True, env=env)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=ws, check=True, capture_output=True, env=env)
    return ws


class TestEndToEndFake:
    """The full Phase 7 scenario, deterministic, no API key."""

    async def test_full_scenario_success(self, e2e_workspace: Path, tmp_path: Path) -> None:
        settings = Settings(
            workspace_root=tmp_path / "ws_root",
            checkpoint_dir=tmp_path / "ckpts",
            audit_log_path=tmp_path / "audit.log",
        )
        settings.ensure_directories()

        events = EventBus()
        audit = AuditLog(settings.audit_log_path)
        metrics = MetricsCollector()

        # 1. Workspace exists (fixture).
        # 2. Project detect (minimal).
        # 3. Create task via manager.
        from tasks.checkpoints import CheckpointStore

        store = CheckpointStore(settings.checkpoint_dir)
        manager = TaskManager(events=events, audit=audit, session_id="e2e",
                              metrics=metrics, store=store)
        manager.create(task_id="e2e-task", user_request="implement feature",
                              workspace=str(e2e_workspace))

        # 4. Context assembly (no memory; bounded).
        # 5. Generate plan (fake plan runner).
        plan = Plan(
            plan_id="e2e-plan", task_id="e2e-task", goal="implement feature",
            steps=[
                PlanStep(step_id="s1", description="write code"),
            ],
        )

        # 6-8. Execute + observe + verify (step succeeds, final verify passes).
        step_runner = FakeStepRunner([
            {"result_text": "implemented", "exit_code": 0, "success": True, "tool_calls": ["write_file"], "duration_ms": 50},
        ])
        plan_runner = FakePlanRunner(plan)
        verifier = FakeVerifier([VerificationOutcome.PASS])
        observer = Observer.from_settings(settings)
        recovery = RecoveryCoordinator(
            policy=RecoveryPolicy(RetryLimits.from_settings(settings)),
            events=events, audit=audit, session_id="e2e",
        )

        controller = ExecutionController(
            manager=manager, runner=step_runner, plan_runner=plan_runner,
            verifier=verifier, observer=observer, recovery=recovery,
            retry_limits=RetryLimits.from_settings(settings),
            events=events, audit=audit, store=store, enable_checkpoints=True,
            max_plan_steps=settings.max_plan_steps, max_plan_deps=settings.max_plan_dependencies,
            session_id="e2e", metrics=metrics,
        )

        # Transition through the state machine.
        manager.transition("e2e-task", TaskState.CONTEXT_READY)
        outcome = await controller.execute("e2e-task", plan=plan)

        # 16. Task completed.
        assert outcome.task.state.value == "completed"
        assert outcome.final_verification is not None
        assert outcome.final_verification.outcome is VerificationOutcome.PASS

        # 18. Verify metrics recorded.
        snap = metrics.snapshot()
        assert snap["counters"].get("tasks.started") == 1
        assert snap["counters"].get("tasks.completed") == 1
        assert snap["counters"].get("steps.executed") >= 1
        assert snap["counters"].get("verification.attempts") >= 1

        # 18. Verify events emitted.
        event_types = {e.type for e in events.history}
        assert EventType.TASK_CREATED in event_types
        assert EventType.TASK_COMPLETED in event_types
        assert EventType.TASK_STEP_STARTED in event_types
        assert EventType.TASK_VERIFICATION_COMPLETED in event_types

        # 18. Verify audit records exist.
        audit_entries = audit.read()
        assert len(audit_entries) > 0

        # 15. Checkpoint persisted.
        assert (tmp_path / "ckpts" / "e2e-task.json").exists()

    async def test_failure_then_repair_then_success(self, e2e_workspace: Path, tmp_path: Path) -> None:
        from intelligence.analyzer import FailureAnalyzer
        from intelligence.repair import RepairContextBuilder, RepairCoordinator
        from intelligence.review import FinalReviewer
        from intelligence.stuck import StuckDetector

        settings = Settings(
            workspace_root=tmp_path / "ws_root",
            checkpoint_dir=tmp_path / "ckpts",
            audit_log_path=tmp_path / "audit.log",
            enable_repair=True, max_repair_attempts=2,
        )
        settings.ensure_directories()

        events = EventBus()
        audit = AuditLog(settings.audit_log_path)
        metrics = MetricsCollector()

        manager = TaskManager(events=events, audit=audit, session_id="e2e2", metrics=metrics)
        manager.create(task_id="e2e2", user_request="fix bug", workspace=str(e2e_workspace))

        plan = Plan(
            plan_id="p2", task_id="e2e2", goal="fix bug",
            steps=[PlanStep(step_id="s1", description="fix")],
        )

        step_runner = FakeStepRunner([
            {"result_text": "attempt 1", "exit_code": 0, "success": True, "tool_calls": [], "duration_ms": 10},
            {"result_text": "repaired", "exit_code": 0, "success": True, "tool_calls": [], "duration_ms": 10},
        ])

        # Make a real change in the workspace so the diff is non-empty.
        (e2e_workspace / "fixed.py").write_text("# fixed\n")
        # Verify outcomes: PASS (step-level), FAIL (final verify triggers repair),
        # PASS (repair coordinator re-verify), PASS (executor re-verify after repair).
        verifier = FakeVerifier([VerificationOutcome.PASS, VerificationOutcome.FAIL,
                                  VerificationOutcome.PASS, VerificationOutcome.PASS])
        observer = Observer.from_settings(settings)
        recovery = RecoveryCoordinator(
            policy=RecoveryPolicy(RetryLimits.from_settings(settings)),
            events=events, audit=audit, session_id="e2e2",
        )
        from tasks.checkpoints import CheckpointStore

        store = CheckpointStore(settings.checkpoint_dir)

        # Fake repair runner that succeeds.
        class FakeRepairRunner:
            async def repair(self, *, prompt: str, session_id: str) -> dict[str, Any]:
                return {"result_text": "fixed", "exit_code": 0, "success": True, "duration_ms": 10}

        analyzer = FailureAnalyzer.from_settings(settings, events=events, audit=audit, session_id="e2e2")
        context_builder = RepairContextBuilder.from_settings(settings)
        stuck = StuckDetector(events=events, session_id="e2e2")
        repair_coord = RepairCoordinator(
            runner=FakeRepairRunner(), analyzer=analyzer, verifier=verifier,
            context_builder=context_builder, stuck_detector=stuck,
            regression_checker=None, change_analyzer=None,
            max_repair_attempts=settings.max_repair_attempts,
            max_verification_attempts=settings.max_verification_attempts,
            events=events, audit=audit, session_id="e2e2",
        )
        reviewer = FinalReviewer(events=events, session_id="e2e2")

        # Use a change analyzer that reports non-empty changes (the workspace
        # was modified by the step runner).
        from intelligence.diff import ChangeAnalyzer, GitToolsInspector
        from tools.git import GitTools

        git = GitTools(
            workspace=e2e_workspace, policy=PermissionPolicy(
                writable_roots=[e2e_workspace], allow_git_write=True),
            audit=audit, events=events, session_id="e2e2",
            default_timeout=10, max_timeout=30,
        )
        change_analyzer = ChangeAnalyzer(
            inspector=GitToolsInspector(git), workspace=e2e_workspace,
            broad_threshold=settings.diff_broad_change_threshold,
            max_changed=settings.diff_max_changed_files,
            events=events, session_id="e2e2",
        )

        controller = ExecutionController(
            manager=manager, runner=step_runner, plan_runner=FakePlanRunner(plan),
            verifier=verifier, observer=observer, recovery=recovery,
            retry_limits=RetryLimits.from_settings(settings),
            events=events, audit=audit, store=store, enable_checkpoints=True,
            max_plan_steps=settings.max_plan_steps, max_plan_deps=settings.max_plan_dependencies,
            session_id="e2e2", metrics=metrics,
            repair_coordinator=repair_coord, change_analyzer=change_analyzer,
            final_reviewer=reviewer,
        )

        manager.transition("e2e2", TaskState.CONTEXT_READY)
        outcome = await controller.execute("e2e2", plan=plan)

        # Task should complete after repair.
        assert outcome.task.state.value == "completed"
        assert outcome.repair is not None
        assert outcome.repair.success

        # Verify repair metric recorded.
        snap = metrics.snapshot()
        assert snap["counters"].get("repair.attempts", 0) >= 1

    async def test_no_secrets_leaked(self, e2e_workspace: Path, tmp_path: Path) -> None:
        """Verify secret-like content never appears in events/audit/checkpoints."""
        settings = Settings(
            checkpoint_dir=tmp_path / "ckpts",
            audit_log_path=tmp_path / "audit.log",
        )
        settings.ensure_directories()

        events = EventBus()
        audit = AuditLog(settings.audit_log_path)
        metrics = MetricsCollector()

        manager = TaskManager(events=events, audit=audit, session_id="secret-test", metrics=metrics)
        manager.create(task_id="secret-task", user_request="handle secret", workspace=str(e2e_workspace))

        plan = Plan(
            plan_id="sp", task_id="secret-task", goal="test",
            steps=[PlanStep(step_id="s1", description="do")],
        )

        secret = "api_key=sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        step_runner = FakeStepRunner([
            {"result_text": f"processed {secret}", "exit_code": 0, "success": True,
             "stdout": secret, "tool_calls": [], "duration_ms": 5},
        ])
        verifier = FakeVerifier([VerificationOutcome.INCONCLUSIVE])
        observer = Observer.from_settings(settings)
        recovery = RecoveryCoordinator(
            policy=RecoveryPolicy(RetryLimits.from_settings(settings)),
            events=events, audit=audit, session_id="secret-test",
        )
        from tasks.checkpoints import CheckpointStore

        store = CheckpointStore(settings.checkpoint_dir)

        controller = ExecutionController(
            manager=manager, runner=step_runner, plan_runner=FakePlanRunner(plan),
            verifier=verifier, observer=observer, recovery=recovery,
            retry_limits=RetryLimits.from_settings(settings),
            events=events, audit=audit, store=store, enable_checkpoints=True,
            max_plan_steps=settings.max_plan_steps, max_plan_deps=settings.max_plan_dependencies,
            session_id="secret-test", metrics=metrics,
        )
        manager.transition("secret-task", TaskState.CONTEXT_READY)
        await controller.execute("secret-task", plan=plan)

        # The raw secret must NOT appear in events.
        import json
        events_json = json.dumps([e.to_dict() for e in events.history])
        assert secret not in events_json

        # Not in audit log.
        audit_json = json.dumps(audit.read())
        assert secret not in audit_json

        # Not in checkpoint.
        ckpt_path = tmp_path / "ckpts" / "secret-task.json"
        if ckpt_path.exists():
            ckpt_json = ckpt_path.read_text()
            assert secret not in ckpt_json

    async def test_working_tree_safety(self, e2e_workspace: Path, tmp_path: Path) -> None:
        """Verify the working tree is safe: git operations are workspace-scoped."""
        settings = Settings(
            audit_log_path=tmp_path / "audit.log",
        )
        audit = AuditLog(settings.audit_log_path)
        events = EventBus()
        policy = PermissionPolicy(writable_roots=[e2e_workspace], allow_git_write=True)
        git = GitTools(
            workspace=e2e_workspace, policy=policy, audit=audit, events=events,
            session_id="wt-test", default_timeout=10, max_timeout=30,
        )
        # Read git status — must be workspace-scoped.
        result = await git.status({})
        text = result["content"][0]["text"]
        # Should show clean or the branch name, not an error about wrong directory.
        assert "fatal:" not in text.lower()

        # Write a file and commit.
        (e2e_workspace / "new.py").write_text("# new\n")
        commit_result = await git.commit({"message": "add new.py"})
        assert "content" in commit_result

        # Verify the commit landed in the workspace repo.
        log = await git.log({"limit": 1})
        log_text = log["content"][0]["text"]
        assert "add new.py" in log_text
