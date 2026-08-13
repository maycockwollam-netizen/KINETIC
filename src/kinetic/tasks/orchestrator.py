"""Glue between the task orchestration layer and the agent session.

This module provides StepRunner/PlanRunner implementations backed by a real
:class:`~kinetic.agent.session.AgentSession`, plus an
:class:`Orchestrator` facade that wires the whole Phase 5 flow together for a
single task (one task at a time per session).

The runners route every tool call through the existing safe path:
``AgentSession.query`` -> ``AgentAdapter`` -> SDK ``can_use_tool`` ->
``ToolRegistry`` -> ``PermissionPolicy`` -> ``Environment``. Nothing here
introduces a second tool path or bypasses the security boundary.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from kinetic.errors import OrchestrationError, PlanError
from kinetic.events import EventBus, EventType
from kinetic.security import AuditLog
from kinetic.tasks.executor import ExecutionController, ExecutionOutcome, PlanRunner, StepRunner
from kinetic.tasks.manager import TaskManager
from kinetic.tasks.models import Plan, PlanStep, Task
from kinetic.tasks.observer import Observer
from kinetic.tasks.policies import RecoveryPolicy, RetryLimits
from kinetic.tasks.recovery import RecoveryCoordinator
from kinetic.tasks.verifier import Verifier

if TYPE_CHECKING:
    from kinetic.agent.session import AgentSession
    from kinetic.config import Settings
    from kinetic.project.models import ProjectManifest


class AgentStepRunner:
    """A :class:`StepRunner` backed by an :class:`AgentSession`.

    Each step is one ``AgentSession.query`` call: the model reasons and selects
    tools; the SDK owns the agent loop. We only capture the bounded result.
    """

    def __init__(self, session: AgentSession) -> None:
        self._session = session

    async def run_step(
        self,
        *,
        plan: Plan,
        step: PlanStep,
        prompt: str,
        session_id: str,
    ) -> dict[str, Any]:
        try:
            result = await self._session.query(prompt)
        except Exception as exc:  # noqa: BLE001
            return {
                "result_text": None,
                "exit_code": 1,
                "errors": [str(exc)],
                "success": False,
                "tool_calls": [],
                "duration_ms": 0,
            }
        is_error = bool(getattr(result, "is_error", False))
        result_text = getattr(result, "result", None)
        # Tool calls are recorded in the event bus; gather from history.
        tool_calls: list[str] = []
        for ev in self._session.events.history:
            if ev.type is EventType.TOOL_STARTED and ev.data.get("tool"):
                tool_calls.append(str(ev.data["tool"]))
        return {
            "result_text": result_text,
            "exit_code": 0 if not is_error else 1,
            "errors": [] if not is_error else [result_text or "agent error"],
            "success": not is_error,
            "tool_calls": tool_calls,
            "duration_ms": int(getattr(result, "duration_ms", 0) or 0),
        }


class AgentPlanRunner:
    """A :class:`PlanRunner` that asks the model to produce a JSON plan.

    The model's raw output is parsed + validated by
    :func:`~kinetic.tasks.planner.parse_model_plan` — never executed blindly.
    """

    def __init__(self, session: AgentSession, *, max_steps: int, max_deps: int) -> None:
        self._session = session
        self._max_steps = max_steps
        self._max_deps = max_deps

    async def make_plan(
        self,
        *,
        task: Task,
        context_block: str | None,
        session_id: str,
    ) -> Plan:
        from kinetic.tasks.planner import parse_model_plan

        prompt = self._plan_prompt(task, context_block)
        result = await self._session.query(prompt)
        text = getattr(result, "result", "") or ""
        raw = self._extract_json(text)
        if raw is None:
            raise PlanError(
                "model did not return a parseable JSON plan",
                reason="malformed_plan",
            )
        return parse_model_plan(raw, task_id=task.id, max_steps=self._max_steps, max_deps=self._max_deps)

    def _plan_prompt(self, task: Task, context_block: str | None) -> str:
        lines = [
            "You are planning a coding task. Produce a JSON execution plan and NOTHING else.",
            f"Task: {task.user_request}",
        ]
        if context_block:
            lines.append(f"\nRecovery context:\n{context_block}")
        lines.append(
            "\nReturn strict JSON with this shape:\n"
            '{"plan_id":"plan-1","goal":"...","steps":['
            '{"step_id":"s1","description":"...","depends_on":[],'
            '"expected_outcome":"...","verification":"..."}],'
            '"verification_criteria":"...","estimated_risk":"low|medium|high|unknown"}'
        )
        return "\n".join(lines)

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any] | None:
        if not text:
            return None
        # Try direct parse first.
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # Fallback: first {...} block.
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
        return None


class Orchestrator:
    """Wires the Phase 5 task execution flow for one AgentSession.

    One task at a time (per session). Builds the TaskManager, verifier, observer,
    recovery, and execution controller from a configured AgentSession + settings,
    then runs a task end-to-end. Reuses the existing environment, permission
    policy, audit, and event bus — never a second copy.
    """

    def __init__(
        self,
        session: AgentSession,
        *,
        settings: Settings,
        manifest: ProjectManifest | None = None,
        plan_runner: PlanRunner | None = None,
        step_runner: StepRunner | None = None,
    ) -> None:
        self._session = session
        self._settings = settings
        self.events: EventBus = session.events
        self.audit: AuditLog = session.audit
        self.manager = TaskManager(
            events=self.events, audit=self.audit, session_id=session.session_id,
            store=self._build_store(),
        )
        self.observer = Observer.from_settings(settings)
        self.verifier = Verifier.from_settings(settings, environment=session.environment, manifest=manifest)
        self.recovery = RecoveryCoordinator(
            policy=RecoveryPolicy(RetryLimits.from_settings(settings)),
            events=self.events, audit=self.audit, session_id=session.session_id,
        )
        self._step_runner = step_runner or AgentStepRunner(session)
        self._plan_runner = plan_runner or AgentPlanRunner(
            session, max_steps=settings.max_plan_steps, max_deps=settings.max_plan_dependencies,
        )
        self.controller = ExecutionController(
            manager=self.manager,
            runner=self._step_runner,
            plan_runner=self._plan_runner,
            verifier=self.verifier,
            observer=self.observer,
            recovery=self.recovery,
            retry_limits=RetryLimits.from_settings(settings),
            events=self.events,
            audit=self.audit,
            store=self.manager._store,  # noqa: SLF001 - shared store
            enable_checkpoints=settings.enable_checkpoints,
            max_plan_steps=settings.max_plan_steps,
            max_plan_deps=settings.max_plan_dependencies,
            session_id=session.session_id,
        )

    def _build_store(self):
        from kinetic.tasks.checkpoints import CheckpointStore

        if not self._settings.enable_checkpoints:
            return None
        return CheckpointStore(self._settings.checkpoint_dir)

    async def run_task(
        self,
        *,
        user_request: str,
        workspace: str,
        project_id: str | None = None,
        task_id: str | None = None,
        plan: Plan | None = None,
    ) -> ExecutionOutcome:
        """Create + plan + execute a task end-to-end.

        Provisions the agent session once, runs the whole plan against it, then
        tears the session down (always). The environment is provisioned by the
        underlying :class:`AgentSession`; this method only orchestrates.
        """
        from kinetic.tasks.manager import default_task_id, workspace_project_id

        tid = task_id or default_task_id()
        pid = project_id or workspace_project_id(workspace)
        self.manager.create(
            task_id=tid, user_request=user_request, workspace=workspace, project_id=pid,
        )
        from kinetic.errors import PermissionDeniedError
        from kinetic.tasks.models import TaskFailure
        from kinetic.tasks.states import TaskState

        try:
            await self._session.prepare()
        except Exception as exc:  # noqa: BLE001
            await self._session.finish()
            self.manager.mark_failed(
                tid,
                failure=TaskFailure(
                    failure_class="environment_failure",
                    message=f"session provisioning failed: {exc}",
                ),
            )
            raise OrchestrationError(f"session provisioning failed: {exc}") from exc
        try:
            return await self.controller.execute(tid, plan=plan)
        except (OrchestrationError, PermissionDeniedError):
            # The controller already recorded the failure + state transition.
            # A PermissionDeniedError means a security boundary held — record it
            # as a failed task and return a bounded outcome (never a fabricated
            # success).
            task = self.manager.load(tid)
            if task.state not in (TaskState.FAILED, TaskState.CANCELLED):
                self.manager.mark_failed(
                    tid,
                    failure=TaskFailure(
                        failure_class="permission_denied",
                        message="a security boundary denied execution during the task",
                    ),
                )
                task = self.manager.load(tid)
            return ExecutionOutcome(
                task=task, plan=plan or Plan(plan_id="unknown", task_id=tid),
                failure=task.failure, cancelled=task.cancelled,
            )
        finally:
            await self._session.finish()

    async def resume_task(self, task_id: str) -> ExecutionOutcome:
        """Resume a task from a checkpoint (fail-closed on corruption).

        Provisions the agent session, restores the task/plan from its
        checkpoint, and continues execution. The environment is provisioned
        here and torn down in ``finally``.
        """
        from kinetic.errors import OrchestrationError, PermissionDeniedError
        from kinetic.tasks.checkpoints import restore_checkpoint
        from kinetic.tasks.states import TaskState

        if self.manager._store is None:  # noqa: SLF001
            raise OrchestrationError("checkpoints disabled; cannot resume")
        data = self.manager._store.load(task_id)  # noqa: SLF001
        task, plan, observations = restore_checkpoint(data)
        # Register the restored task with the manager.
        self.manager._tasks[task.id] = task  # noqa: SLF001
        from kinetic.tasks.models import TaskFailure

        try:
            await self._session.prepare()
        except Exception as exc:  # noqa: BLE001
            await self._session.finish()
            self.manager.mark_failed(
                task.id,
                failure=TaskFailure(
                    failure_class="environment_failure",
                    message=f"resume provisioning failed: {exc}",
                ),
            )
            raise OrchestrationError(f"resume provisioning failed: {exc}") from exc
        try:
            return await self.controller.execute(task.id, plan=plan)
        except (OrchestrationError, PermissionDeniedError):
            t = self.manager.load(task.id)
            if t.state not in (TaskState.FAILED, TaskState.CANCELLED):
                self.manager.mark_failed(
                    task.id,
                    failure=TaskFailure(
                        failure_class="permission_denied",
                        message="a security boundary denied execution during resume",
                    ),
                )
                t = self.manager.load(task.id)
            return ExecutionOutcome(
                task=t, plan=plan or Plan(plan_id="unknown", task_id=task.id),
                failure=t.failure, cancelled=t.cancelled,
            )
        finally:
            await self._session.finish()

    def cancel(self, task_id: str, *, reason: str = "cancelled") -> None:
        self.manager.cancel(task_id, reason=reason)


__all__ = [
    "AgentPlanRunner",
    "AgentStepRunner",
    "Orchestrator",
]
