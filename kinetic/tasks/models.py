"""Task and plan data models.

Structured, machine-readable task and plan objects. Large data (stdout/stderr,
model reasoning) never lives inside these objects — observations and model
output are stored separately (observations are bounded; model output is not
persisted at all). This keeps the task object small and inspectable.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator

from kinetic.tasks.states import TaskState


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class StepStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"  # dependencies satisfied
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class PlanStep(BaseModel):
    """One executable step within a plan."""

    step_id: str
    description: str = ""
    depends_on: list[str] = Field(default_factory=list)
    expected_outcome: str = ""
    verification: str = ""
    status: StepStatus = StepStatus.PENDING
    attempts: int = 0

    @field_validator("step_id")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("step_id must be non-empty")
        return v

    @field_validator("depends_on")
    @classmethod
    def _dedupe_deps(cls, v: list[str]) -> list[str]:
        seen: list[str] = []
        for d in v:
            if d not in seen:
                seen.append(d)
        return seen


class Plan(BaseModel):
    """A structured, validated execution plan.

    Plans are machine-readable and validated before execution: dependencies
    must be acyclic, step IDs unique, and the plan bounded.
    """

    plan_id: str
    task_id: str
    goal: str = ""
    steps: list[PlanStep] = Field(default_factory=list)
    verification_criteria: str = ""
    estimated_risk: str = "unknown"

    @property
    def step_ids(self) -> list[str]:
        return [s.step_id for s in self.steps]

    def get_step(self, step_id: str) -> PlanStep | None:
        return next((s for s in self.steps if s.step_id == step_id), None)

    def completed_step_ids(self) -> list[str]:
        return [s.step_id for s in self.steps if s.status is StepStatus.COMPLETED]

    def all_done(self) -> bool:
        return bool(self.steps) and all(s.status is StepStatus.COMPLETED for s in self.steps)


class TaskFailure(BaseModel):
    """Structured failure information attached to a task."""

    failure_class: str
    message: str
    step_id: str | None = None
    attempt: int = 0
    observation_summary: str = ""
    timestamp: str = Field(default_factory=_now_iso)


class Task(BaseModel):
    """The authoritative task record.

    State is mutated only through the TaskManager. Large outputs (model
    reasoning, raw observations) are never stored here — only references and
    bounded summaries.
    """

    id: str
    user_request: str
    workspace: str
    project_id: str | None = None
    state: TaskState = TaskState.CREATED
    plan_id: str | None = None
    current_step: str | None = None
    attempt_count: int = 0
    replan_count: int = 0
    created_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)
    completed_at: str | None = None
    failure: TaskFailure | None = None
    cancelled: bool = False
    cancellation_reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("user_request")
    @classmethod
    def _nonempty_request(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("user_request must be non-empty")
        return v

    def touch(self) -> None:
        self.updated_at = _now_iso()

    def summary(self) -> dict[str, Any]:
        """A bounded, public status snapshot (no private reasoning)."""
        return {
            "id": self.id,
            "state": self.state.value,
            "current_step": self.current_step,
            "attempt_count": self.attempt_count,
            "replan_count": self.replan_count,
            "plan_id": self.plan_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "failure": self.failure.model_dump() if self.failure else None,
            "cancelled": self.cancelled,
        }
