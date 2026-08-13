"""Structured data models for the coding-intelligence layer.

All models are small, serializable, and bounded. Large outputs (stdout/stderr)
are always carried as bounded snippets — never raw unbounded model output. The
models carry no logic; they are pure data exchanged between the analyzer,
repair coordinator, regression checker, reviewer and the checkpoint store.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from kinetic.tasks.policies import FailureClass


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class TestFailureInfo:
    """One structured test failure extracted from runner output."""

    name: str
    file: str = ""
    line: int | None = None
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "file": self.file,
            "line": self.line,
            "message": self.message,
        }


@dataclass
class FailureAnalysis:
    """Structured, bounded failure information produced by the analyzer.

    Captured output is always bounded and secret-masked before this object is
    persisted, audited, or exposed to the model.
    """

    failure_class: FailureClass
    command: str = ""
    tool: str = ""
    exit_code: int | None = None
    stdout_bounded: str = ""
    stderr_bounded: str = ""
    workspace: str = ""
    project_id: str = ""
    transient: bool = False
    retryable: bool = True
    test_failures: list[TestFailureInfo] = field(default_factory=list)
    failure_count: int | None = None
    diagnostic: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=_now_iso)

    @property
    def is_security_denial(self) -> bool:
        return self.failure_class is FailureClass.PERMISSION_DENIED

    @property
    def is_terminal(self) -> bool:
        """Failures that should never be retried/repaired."""
        return self.failure_class in (
            FailureClass.PERMISSION_DENIED,
            FailureClass.CANCELLATION,
            FailureClass.INVALID_PLAN,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "failure_class": self.failure_class.value,
            "command": self.command,
            "tool": self.tool,
            "exit_code": self.exit_code,
            "stdout_bounded": self.stdout_bounded,
            "stderr_bounded": self.stderr_bounded,
            "workspace": self.workspace,
            "project_id": self.project_id,
            "transient": self.transient,
            "retryable": self.retryable,
            "test_failures": [t.to_dict() for t in self.test_failures],
            "failure_count": self.failure_count,
            "diagnostic": dict(self.diagnostic),
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True)
class ChangeRecord:
    """One changed file parsed from ``git status --porcelain``."""

    path: str
    status: str  # the porcelain XY code
    staged: bool = False

    @property
    def is_added(self) -> bool:
        return self.status[0] in ("A", "?")

    @property
    def is_deleted(self) -> bool:
        return "D" in self.status

    @property
    def is_modified(self) -> bool:
        return "M" in self.status

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "status": self.status, "staged": self.staged}


@dataclass
class ChangeAnalysis:
    """Bounded description of the working-tree changes."""

    changed: list[ChangeRecord] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    outside_workspace: list[str] = field(default_factory=list)
    generated: list[str] = field(default_factory=list)
    broad: bool = False
    empty: bool = True
    diff_bounded: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "changed": [c.to_dict() for c in self.changed],
            "added": list(self.added),
            "deleted": list(self.deleted),
            "modified": list(self.modified),
            "outside_workspace": list(self.outside_workspace),
            "generated": list(self.generated),
            "broad": self.broad,
            "empty": self.empty,
            "diff_bounded": self.diff_bounded,
        }


@dataclass
class RepairAttempt:
    """One bounded repair attempt's record."""

    attempt: int
    analysis: FailureAnalysis | None = None
    repair_prompt_bounded: str = ""
    success: bool = False
    error: str = ""
    timestamp: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "analysis": self.analysis.to_dict() if self.analysis else None,
            "repair_prompt_bounded": self.repair_prompt_bounded,
            "success": self.success,
            "error": self.error,
            "timestamp": self.timestamp,
        }


@dataclass
class StuckSignal:
    """Result of stuck detection."""

    stuck: bool
    reason: str = ""
    signature: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"stuck": self.stuck, "reason": self.reason, "signature": self.signature}


@dataclass
class RepairState:
    """Mutable, serializable Phase 6 repair state for one task.

    Persisted in checkpoints so a task can resume its repair loop safely.
    """

    attempts: list[RepairAttempt] = field(default_factory=list)
    verification_attempts: int = 0
    total_recovery_attempts: int = 0
    last_failure_signature: str = ""
    stuck: StuckSignal | None = None
    regression_detected: bool = False

    @property
    def repair_count(self) -> int:
        return len(self.attempts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempts": [a.to_dict() for a in self.attempts],
            "verification_attempts": self.verification_attempts,
            "total_recovery_attempts": self.total_recovery_attempts,
            "last_failure_signature": self.last_failure_signature,
            "stuck": self.stuck.to_dict() if self.stuck else None,
            "regression_detected": self.regression_detected,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> RepairState:
        if not data or not isinstance(data, dict):
            return cls()
        stuck_raw = data.get("stuck")
        stuck = StuckSignal(**stuck_raw) if isinstance(stuck_raw, dict) else None
        return cls(
            attempts=[],  # attempts are rebuilt on demand; not restored deeply
            verification_attempts=int(data.get("verification_attempts", 0)),
            total_recovery_attempts=int(data.get("total_recovery_attempts", 0)),
            last_failure_signature=str(data.get("last_failure_signature", "")),
            stuck=stuck,
            regression_detected=bool(data.get("regression_detected", False)),
        )


@dataclass
class RepairOutcome:
    """The result of the bounded repair loop."""

    success: bool
    state: RepairState
    final_analysis: FailureAnalysis | None = None
    regression: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "state": self.state.to_dict(),
            "final_analysis": self.final_analysis.to_dict() if self.final_analysis else None,
            "regression": self.regression,
            "reason": self.reason,
        }


@dataclass
class RegressionResult:
    """Outcome of a post-repair regression check."""

    regressed: bool
    before_passed: bool
    after_passed: bool
    command: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "regressed": self.regressed,
            "before_passed": self.before_passed,
            "after_passed": self.after_passed,
            "command": self.command,
            "reason": self.reason,
        }


@dataclass
class ReviewCheck:
    """One deterministic final-review check."""

    name: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass
class ReviewResult:
    """Aggregate result of the deterministic final review."""

    passed: bool
    checks: list[ReviewCheck] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checks": [c.to_dict() for c in self.checks],
            "reason": self.reason,
        }
