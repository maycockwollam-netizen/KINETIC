"""Execution controller: the single safe tool-execution path.

The executor drives a validated plan to completion, one step at a time, in
topological (dependency) order. Each step is executed by the agent — through a
:class:`StepRunner` that wraps the existing AgentSession/adapter/registry/
permission/environment stack. There is exactly one safe execution path: the
runner goes through ``AgentSession.query`` -> ``AgentAdapter`` -> ``can_use_tool``
-> ``ToolRegistry`` -> ``PermissionPolicy`` -> ``Environment`` -> runtime.

The executor itself never calls subprocesses, never touches the filesystem
directly, and never bypasses the permission policy. It only orchestrates.

After each step it verifies (via the :class:`Verifier`), observes (via the
:class:`Observer`), and on failure hands off to the
:class:`RecoveryCoordinator` for a bounded retry / re-plan / fail decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from kinetic.errors import OrchestrationError
from kinetic.events import EventBus, EventType
from kinetic.security import AuditLog
from kinetic.tasks.checkpoints import build_checkpoint
from kinetic.tasks.models import Plan, PlanStep, StepStatus, Task, TaskFailure
from kinetic.tasks.observer import Observation, Observer
from kinetic.tasks.planner import (
    next_executable_step,
    reset_failed_steps,
    topological_order,
    validate_plan,
)
from kinetic.tasks.policies import FailureClass, RetryLimits, VerificationOutcome
from kinetic.tasks.recovery import RecoveryCoordinator, failure_context_prompt
from kinetic.tasks.states import REPLAN_SOURCE_STATES, TaskState
from kinetic.tasks.verifier import VerificationResult, Verifier

if TYPE_CHECKING:
    from kinetic.tasks.checkpoints import CheckpointStore
    from kinetic.tasks.manager import TaskManager


@runtime_checkable
class StepRunner(Protocol):
    """Executes one plan step through the agent (one safe path).

    Implementations wrap :class:`~kinetic.agent.session.AgentSession` (live
    model) or a deterministic fake (tests). The runner MUST route tool calls
    through the existing permission/environment stack; it never executes tools
    directly outside that boundary.
    """

    async def run_step(
        self,
        *,
        plan: Plan,
        step: PlanStep,
        prompt: str,
        session_id: str,
    ) -> dict[str, Any]:
        """Execute one step; return ``{result_text, exit_code, ...}``."""
        ...


@runtime_checkable
class PlanRunner(Protocol):
    """Produces a validated plan from a model (or directly, for tests)."""

    async def make_plan(
        self,
        *,
        task: Task,
        context_block: str | None,
        session_id: str,
    ) -> Plan:
        ...


@dataclass
class ExecutionOutcome:
    """The result of running a plan to completion (or failure)."""

    task: Task
    plan: Plan
    observations: list[Observation] = field(default_factory=list)
    final_verification: VerificationResult | None = None
    replans: int = 0
    failure: TaskFailure | None = None
    cancelled: bool = False


class ExecutionController:
    """Drives a plan through the agent, verifying + recovering as it goes.

    Dependencies:
      * ``manager`` — authoritative task state (only writer).
      * ``runner`` — the single safe execution path (AgentSession-backed).
      * ``verifier`` — verification abstraction.
      * ``observer`` — bounded, secret-filtered observations.
      * ``recovery`` — bounded recovery decisions + audit/events.
      * ``store`` — optional checkpoint persistence.
    """

    def __init__(
        self,
        *,
        manager: TaskManager,
        runner: StepRunner,
        plan_runner: PlanRunner | None = None,
        verifier: Verifier,
        observer: Observer,
        recovery: RecoveryCoordinator,
        retry_limits: RetryLimits,
        events: EventBus,
        audit: AuditLog | None = None,
        store: CheckpointStore | None = None,
        enable_checkpoints: bool = True,
        max_plan_steps: int = 12,
        max_plan_deps: int = 8,
        session_id: str = "tasks",
    ) -> None:
        self.manager = manager
        self.runner = runner
        self.plan_runner = plan_runner
        self.verifier = verifier
        self.observer = observer
        self.recovery = recovery
        self.limits = retry_limits
        self.events = events
        self.audit = audit
        self.store = store
        self.enable_checkpoints = enable_checkpoints and store is not None
        self.max_plan_steps = max_plan_steps
        self.max_plan_deps = max_plan_deps
        self.session_id = session_id

    # --- top-level orchestration ------------------------------------------

    async def execute(self, task_id: str, *, plan: Plan | None = None) -> ExecutionOutcome:
        """Plan (if needed) + execute a task to completion.

        ``plan`` may be provided directly (e.g. restored from a checkpoint); if
        absent, a :class:`PlanRunner` must be configured.
        """
        task = self.manager.load(task_id)
        if task.state in REPLAN_SOURCE_STATES or task.state is TaskState.PLAN_READY:
            pass  # resuming
        elif task.state is TaskState.CREATED or task.state is TaskState.CONTEXT_READY:
            self.manager.transition(task_id, TaskState.PLANNING)
        elif task.state is TaskState.PLANNING:
            pass
        else:
            raise OrchestrationError(f"cannot execute task in state {task.state.value}")

        # Obtain/validate a plan.
        if plan is None:
            plan = await self._make_plan(task)
        else:
            validate_plan(plan, max_steps=self.max_plan_steps, max_deps=self.max_plan_deps)
        self.manager.set_plan(task_id, plan.plan_id)
        self.manager.transition(task_id, TaskState.PLAN_READY)
        self.events.emit(EventType.TASK_PLAN_CREATED, self.session_id, task_id=task_id, plan_id=plan.plan_id, steps=len(plan.steps))
        self.manager.transition(task_id, TaskState.EXECUTING)

        observations: list[Observation] = []
        replans = 0
        try:
            while not plan.all_done():
                if self.manager.load(task_id).cancelled:
                    return self._cancelled(task, plan, observations, replans)
                step = next_executable_step(plan)
                if step is None:
                    if plan.all_done():
                        break
                    # No executable step and not all done → stuck; attempt recovery.
                    if not self._recover_blocked(task, plan, observations):
                        break
                    continue
                outcome = await self._run_step(task, plan, step)
                observations.extend(outcome)
                # Re-read plan (steps may have been reset/re-planned).
                if self.manager.load(task_id).cancelled:
                    return self._cancelled(task, plan, observations, replans)
            # Final verification of the whole task (EXECUTING -> VERIFYING first).
            self.manager.transition(task_id, TaskState.VERIFYING, reason="final verification")
            final = await self._verify_task(task, plan, observations)
            if final.outcome is VerificationOutcome.PASS:
                self.manager.mark_completed(task_id)
            elif final.outcome is VerificationOutcome.FAIL:
                failure = TaskFailure(
                    failure_class=FailureClass.TEST_FAILURE.value,
                    message=f"final verification failed: {final.reason}",
                    observation_summary=final.stderr or final.reason,
                )
                self.manager.mark_failed(task_id, failure=failure)
            else:
                # Inconclusive: do NOT pretend success. Mark completed only if
                # all steps succeeded and there was no explicit failure signal;
                # otherwise fail honestly.
                if all(o.success for o in observations) and plan.all_done():
                    self.manager.mark_completed(task_id)
                else:
                    failure = TaskFailure(
                        failure_class=FailureClass.UNKNOWN.value,
                        message="verification inconclusive and not all steps succeeded",
                    )
                    self.manager.mark_failed(task_id, failure=failure)
        except OrchestrationError:
            raise
        return ExecutionOutcome(
            task=self.manager.load(task_id),
            plan=plan,
            observations=observations,
            final_verification=final,
            replans=replans,
            failure=self.manager.load(task_id).failure,
        )

    # --- step execution ----------------------------------------------------

    async def _run_step(self, task: Task, plan: Plan, step: PlanStep) -> list[Observation]:
        """Run one step with bounded retry; emit events; return observations."""
        produced: list[Observation] = []
        attempt = 0
        while True:
            attempt += 1
            step.attempts = attempt
            step.status = StepStatus.RUNNING
            self.manager.set_current_step(task.id, step.step_id)
            self.events.emit(
                EventType.TASK_STEP_STARTED, self.session_id,
                task_id=task.id, step_id=step.step_id, attempt=attempt,
            )
            prompt = self._build_step_prompt(task, plan, step)
            result: dict[str, Any] = {}
            runner_error: str | None = None
            try:
                result = await self.runner.run_step(
                    plan=plan, step=step, prompt=prompt, session_id=self.session_id,
                )
            except Exception as exc:  # noqa: BLE001 - record, classify, recover
                runner_error = str(exc)

            result_text = result.get("result_text") if isinstance(result, dict) else None
            exit_code = result.get("exit_code") if isinstance(result, dict) else None
            stdout = result.get("stdout", "") if isinstance(result, dict) else ""
            stderr = result.get("stderr", "") if isinstance(result, dict) else ""
            duration_ms = result.get("duration_ms", 0) if isinstance(result, dict) else 0
            tool_calls = result.get("tool_calls", []) if isinstance(result, dict) else []
            errors = [runner_error] if runner_error else (
                result.get("errors", []) if isinstance(result, dict) else []
            )
            success = bool(result.get("success", exit_code == 0 and not runner_error)) if isinstance(result, dict) else False

            # Step-level verification (if the step declares one or it is the
            # final step). Keep it lightweight: run the project verifier.
            verification = "skipped"
            verify_result: VerificationResult | None = None
            if step.verification or self._is_final_step(plan, step):
                verify_result = await self.verifier.verify(command=step.verification or None)
                verification = verify_result.outcome.value
                self.events.emit(
                    EventType.TASK_VERIFICATION_STARTED, self.session_id,
                    task_id=task.id, step_id=step.step_id,
                )
                self.events.emit(
                    EventType.TASK_VERIFICATION_COMPLETED, self.session_id,
                    task_id=task.id, step_id=step.step_id, outcome=verification,
                )

            obs = self.observer.observe(
                step_id=step.step_id,
                result_text=result_text,
                exit_code=exit_code,
                stdout=stdout, stderr=stderr,
                changed_files=result.get("changed_files") if isinstance(result, dict) else None,
                test_results=verify_result.__dict__ if verify_result else None,
                errors=errors,
                duration_ms=duration_ms,
                verification=verification,
                tool_calls=tool_calls,
                success=success and (verification != "fail"),
            )
            produced.append(obs)
            self._checkpoint(task, plan, obs, produced)

            if obs.success:
                step.status = StepStatus.COMPLETED
                self.events.emit(
                    EventType.TASK_STEP_COMPLETED, self.session_id,
                    task_id=task.id, step_id=step.step_id, attempt=attempt,
                )
                return produced

            # Failure path.
            step.status = StepStatus.FAILED
            self.events.emit(
                EventType.TASK_STEP_FAILED, self.session_id,
                task_id=task.id, step_id=step.step_id, attempt=attempt,
            )
            task_attempts = self.manager.increment_attempt(task.id)
            fclass, decision = self.recovery.evaluate(
                observation=obs,
                step_attempts=attempt,
                task_attempts=task_attempts,
                replans=self.manager.load(task.id).replan_count,
                verification_failed=(verification == "fail"),
            )
            if decision.fail:
                self.manager.mark_failed(
                    task.id,
                    failure=TaskFailure(
                        failure_class=fclass.value,
                        message=decision.reason,
                        step_id=step.step_id,
                        attempt=attempt,
                        observation_summary=obs.stderr_summary or obs.stdout_summary,
                    ),
                )
                raise OrchestrationError(f"step {step.step_id} failed permanently: {decision.reason}")
            if decision.replan:
                await self._replan(task, plan, obs, produced)
                return produced
            # decision.retry is True: reset the step and loop to retry it.
            step.status = StepStatus.PENDING

    # --- planning / re-planning -------------------------------------------

    async def _make_plan(self, task: Task) -> Plan:
        if self.plan_runner is None:
            raise OrchestrationError("no plan runner configured to produce a plan")
        self.events.emit(EventType.TASK_PLANNING_STARTED, self.session_id, task_id=task.id)
        try:
            plan = await self.plan_runner.make_plan(
                task=task, context_block=None, session_id=self.session_id,
            )
        except Exception as exc:  # noqa: BLE001
            self.manager.mark_failed(
                task.id,
                failure=TaskFailure(
                    failure_class=FailureClass.INVALID_PLAN.value,
                    message=f"planning failed: {exc}",
                ),
            )
            raise OrchestrationError(f"planning failed: {exc}") from exc
        return validate_plan(plan, max_steps=self.max_plan_steps, max_deps=self.max_plan_deps)

    async def _replan(self, task: Task, plan: Plan, failed_obs: Observation, observations: list[Observation]) -> None:
        """Re-plan with updated observations (bounded)."""
        if self.plan_runner is None:
            self.manager.mark_failed(
                task.id,
                failure=TaskFailure(
                    failure_class=FailureClass.INVALID_PLAN.value,
                    message="re-planning requested but no plan runner configured",
                    step_id=failed_obs.step_id,
                ),
            )
            raise OrchestrationError("re-planning requested but no plan runner configured")
        replans = self.manager.increment_replan(task.id)
        if replans > self.limits.max_replans:
            self.manager.mark_failed(
                task.id,
                failure=TaskFailure(
                    failure_class=FailureClass.INVALID_PLAN.value,
                    message="re-plan budget exhausted",
                    step_id=failed_obs.step_id,
                ),
            )
            raise OrchestrationError("re-plan budget exhausted")
        # Move to RECOVERING then PLANNING (validated path).
        self.manager.transition(task.id, TaskState.RECOVERING, reason=f"re-plan after {failed_obs.step_id}")
        self.manager.transition(task.id, TaskState.PLANNING, reason="re-planning")
        context = failure_context_prompt(
            failure_class=FailureClass.INVALID_PLAN,
            observation=failed_obs,
            plan_goal=plan.goal,
            previous_observations=observations,
        )
        new_plan = await self.plan_runner.make_plan(
            task=task, context_block=context, session_id=self.session_id,
        )
        new_plan = validate_plan(new_plan, max_steps=self.max_plan_steps, max_deps=self.max_plan_deps)
        # Preserve completed steps' results by reusing their IDs if present.
        self._merge_plan(plan, new_plan)
        self.events.emit(
            EventType.TASK_REPLANNED, self.session_id,
            task_id=task.id, plan_id=new_plan.plan_id, replans=replans,
        )
        self.manager.transition(task.id, TaskState.PLAN_READY, reason="re-planned")
        self.manager.transition(task.id, TaskState.EXECUTING, reason="executing re-planned")

    def _merge_plan(self, old: Plan, new: Plan) -> None:
        """Apply the new plan's steps onto the old plan object, preserving
        completed steps where IDs match."""
        completed = {s.step_id: s for s in old.steps if s.status is StepStatus.COMPLETED}
        old.steps = new.steps
        for s in old.steps:
            if s.step_id in completed:
                s.status = StepStatus.COMPLETED
                s.attempts = completed[s.step_id].attempts
        old.plan_id = new.plan_id
        old.goal = new.goal or old.goal
        old.verification_criteria = new.verification_criteria or old.verification_criteria

    # --- helpers ----------------------------------------------------------

    def _build_step_prompt(self, task: Task, plan: Plan, step: PlanStep) -> str:
        """A bounded prompt for one step (no raw history dump)."""
        lines = [
            f"Task: {task.user_request}",
            f"Plan goal: {plan.goal}",
            f"Step {step.step_id}: {step.description or step.step_id}",
        ]
        if step.expected_outcome:
            lines.append(f"Expected outcome: {step.expected_outcome}")
        if step.verification:
            lines.append(f"Verification: {step.verification}")
        completed = [s.step_id for s in plan.steps if s.status is StepStatus.COMPLETED]
        if completed:
            lines.append("Completed steps: " + ", ".join(completed))
        lines.append("Use the available tools to complete this step. Stop when done.")
        return "\n".join(lines)

    def _is_final_step(self, plan: Plan, step: PlanStep) -> bool:
        order = topological_order(plan)
        return bool(order) and order[-1].step_id == step.step_id

    async def _verify_task(self, task: Task, plan: Plan, observations: list[Observation]) -> VerificationResult:
        self.events.emit(EventType.TASK_VERIFICATION_STARTED, self.session_id, task_id=task.id, step_id=None)
        result = await self.verifier.verify()
        self.events.emit(
            EventType.TASK_VERIFICATION_COMPLETED, self.session_id,
            task_id=task.id, outcome=result.outcome.value,
        )
        return result

    def _recover_blocked(self, task: Task, plan: Plan, observations: list[Observation]) -> bool:
        """No executable step but not all done — attempt a reset/re-plan once."""
        if not plan.steps:
            return False
        reset_failed_steps(plan)
        return next_executable_step(plan) is not None

    def _checkpoint(
        self, task: Task, plan: Plan, latest: Observation, observations: list[Observation]
    ) -> None:
        if not self.enable_checkpoints:
            return
        try:
            ckpt = build_checkpoint(
                task, plan,
                observations=[o.to_dict() for o in observations],
                completed_step_ids=plan.completed_step_ids(),
            )
            self.manager.snapshot(task.id, ckpt)
        except Exception as exc:  # noqa: BLE001 - checkpoint is best-effort
            self.events.emit(EventType.AGENT_ERROR, self.session_id, action="checkpoint", reason=str(exc))

    def _cancelled(
        self, task: Task, plan: Plan, observations: list[Observation], replans: int
    ) -> ExecutionOutcome:
        return ExecutionOutcome(
            task=task, plan=plan, observations=observations, replans=replans, cancelled=True,
        )


__all__ = [
    "ExecutionController",
    "ExecutionOutcome",
    "StepRunner",
    "PlanRunner",
]
