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


class StorageError(KINETICError):
    """Persistent storage failed."""
