"""Phase 5 — task planning & execution orchestration.

Turns KINETIC from an agent infrastructure layer into a complete autonomous
coding-task execution system. The Claude Agent SDK remains responsible for
model interaction and tool reasoning; KINETIC owns the task lifecycle, planning
state, execution state, observation, verification, bounded recovery, and
checkpoints.

Architecture::

    TaskManager (state machine)
        -> Planner (model-generated, validated)
        -> ExecutionController (single safe path)
            -> AgentSession / AgentAdapter (SDK)
                -> ToolRegistry -> PermissionPolicy -> Environment -> Runtime
        -> Observer (bounded, secret-filtered)
        -> Verifier (PASS/FAIL/INCONCLUSIVE)
        -> RecoveryCoordinator (bounded retry / re-plan / fail)
        -> Checkpoints (local, deterministic, fail-closed restore)

Security is enforced at the runtime/tool layer (unchanged from Phase 1–4).
The orchestration layer adds NO new tool path, NO new permission system, and
NO direct subprocess/filesystem access.
"""

from __future__ import annotations

from kinetic.tasks.checkpoints import (
    CheckpointStore,
    build_checkpoint,
    restore_checkpoint,
    restore_repair_state,
)
from kinetic.tasks.executor import ExecutionController, ExecutionOutcome, PlanRunner, StepRunner
from kinetic.tasks.manager import TaskManager, default_task_id, workspace_project_id
from kinetic.tasks.models import Plan, PlanStep, StepStatus, Task, TaskFailure
from kinetic.tasks.observer import Observation, Observer, summarize
from kinetic.tasks.planner import (
    invalidate_remaining_steps,
    next_executable_step,
    parse_model_plan,
    reset_failed_steps,
    topological_order,
    validate_plan,
)
from kinetic.tasks.policies import (
    FailureClass,
    RecoveryDecision,
    RecoveryPolicy,
    RetryLimits,
    VerificationOutcome,
    classify_failure,
)
from kinetic.tasks.recovery import RecoveryCoordinator, failure_context_prompt
from kinetic.tasks.states import REPLAN_SOURCE_STATES, TERMINAL_STATES, TaskState
from kinetic.tasks.verifier import VerificationResult, Verifier, command_for_manifest

__all__ = [
    "CheckpointStore",
    "ExecutionController",
    "ExecutionOutcome",
    "FailureClass",
    "Observation",
    "Observer",
    "Plan",
    "PlanRunner",
    "PlanStep",
    "REPLAN_SOURCE_STATES",
    "RecoveryCoordinator",
    "RecoveryDecision",
    "RecoveryPolicy",
    "RetryLimits",
    "StepRunner",
    "StepStatus",
    "TERMINAL_STATES",
    "Task",
    "TaskFailure",
    "TaskManager",
    "TaskState",
    "VerificationOutcome",
    "VerificationResult",
    "Verifier",
    "build_checkpoint",
    "classify_failure",
    "command_for_manifest",
    "default_task_id",
    "failure_context_prompt",
    "invalidate_remaining_steps",
    "next_executable_step",
    "parse_model_plan",
    "reset_failed_steps",
    "restore_checkpoint",
    "restore_repair_state",
    "summarize",
    "topological_order",
    "validate_plan",
    "workspace_project_id",
]
