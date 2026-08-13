"""Domain-level error hierarchy for KINETIC.

Errors are intentionally narrow so callers can react to specific failure modes
(permission denied, tool failure, environment/sandbox issues, SDK transport errors)
without parsing strings.
"""

from __future__ import annotations


class KINETICError(Exception):
    """Base class for all KINETIC errors."""


class ConfigError(KINETICError):
    """Raised when configuration is missing or invalid."""


class SecurityError(KINETICError):
    """Raised when a runtime security boundary is violated."""


class PermissionDeniedError(SecurityError):
    """A tool invocation was denied by the permission policy."""

    def __init__(self, tool: str, reason: str) -> None:
        self.tool = tool
        self.reason = reason
        super().__init__(f"Permission denied for tool {tool!r}: {reason}")


class ToolError(KINETICError):
    """A tool failed during execution."""

    def __init__(self, tool: str, message: str) -> None:
        self.tool = tool
        super().__init__(f"[{tool}] {message}")


class AgentError(KINETICError):
    """The agent layer failed (session, transport, or model)."""


class EnvironmentError_(KINETICError):  # noqa: N801 - avoid shadowing builtin in imports
    """The environment/sandbox subsystem failed."""


class SandboxError(EnvironmentError_):
    """A sandbox/runtime operation failed (creation, isolation, enforcement)."""


class EnvironmentStateError(EnvironmentError_):
    """An invalid environment lifecycle transition or state was requested."""


class RuntimeUnavailableError(EnvironmentError_):
    """The requested runtime (e.g. Docker) is unavailable; do not fall back silently."""


class WorkspaceError(EnvironmentError_):
    """A workspace operation failed (creation, cleanup, path validation)."""

    def __init__(self, workspace: str, message: str) -> None:
        self.workspace = workspace
        super().__init__(f"[workspace {workspace}] {message}")


class ProjectError(KINETICError):
    """Project scanning or metadata failed."""


class GitError(KINETICError):
    """A Git operation failed."""

    def __init__(self, operation: str, message: str, *, exit_code: int | None = None) -> None:
        self.operation = operation
        self.exit_code = exit_code
        suffix = f" (exit {exit_code})" if exit_code is not None else ""
        super().__init__(f"[git {operation}] {message}{suffix}")


class DependencyError(KINETICError):
    """Dependency detection or installation failed."""

    def __init__(self, message: str, *, ecosystem: str | None = None, command: str | None = None,
                 exit_code: int | None = None) -> None:
        self.ecosystem = ecosystem
        self.command = command
        self.exit_code = exit_code
        parts = [message]
        if ecosystem:
            parts.append(f"ecosystem={ecosystem}")
        if command:
            parts.append(f"command={command}")
        if exit_code is not None:
            parts.append(f"exit={exit_code}")
        super().__init__(": ".join(parts))


class StorageError(KINETICError):
    """Persistent storage failed."""


class MemoryError(KINETICError):
    """The memory subsystem failed (store, retrieval, consolidation)."""


class MemorySecurityError(MemoryError):
    """A memory operation violated a security boundary (secret, scope)."""

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        self.reason = reason
        super().__init__(message)


class ContextError(KINETICError):
    """The context engine failed to assemble a context package."""


# Phase 5 — task planning & execution orchestration
class TaskError(KINETICError):
    """The task orchestration subsystem failed."""


class TaskStateError(TaskError):
    """An invalid task state transition was requested."""

    def __init__(self, from_: str, to: str) -> None:
        self.from_state = from_
        self.to_state = to
        super().__init__(f"invalid task state transition: {from_} -> {to}")


class PlanError(TaskError):
    """A plan is missing, malformed, or failed validation."""

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        self.reason = reason
        super().__init__(message)


class VerificationError(TaskError):
    """The verification subsystem failed (not a test failure — an internal error)."""


class CheckpointError(TaskError):
    """A checkpoint is missing, corrupted, or cannot be safely restored."""


class OrchestrationError(TaskError):
    """The execution controller could not complete the task."""


# Phase 6 — coding intelligence, verification & recovery
class IntelligenceError(KINETICError):
    """The coding-intelligence subsystem failed (analysis, repair, review)."""


class RepairError(IntelligenceError):
    """A bounded repair operation failed."""
