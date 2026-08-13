"""Controlled process execution model.

Every process is associated with an :class:`~environment.environment.Environment`
and carries its full execution context (command, cwd, env, timeout) plus the
captured outcome (stdout/stderr/exit code) and lifecycle state. No arbitrary
host-level execution bypasses the environment abstraction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class ProcessState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    FAILED = "failed"


@dataclass(frozen=True)
class ProcessSpec:
    """The request to execute a command inside an environment."""

    command: str
    cwd: str = "."
    env: dict[str, str] = field(default_factory=dict)
    timeout: float = 120.0

    def to_dict(self) -> dict[str, Any]:
        # Never echo env values (may carry secrets); list keys only.
        return {
            "command": self.command,
            "cwd": self.cwd,
            "env_keys": sorted(self.env.keys()),
            "timeout": self.timeout,
        }


@dataclass
class ProcessResult:
    """The outcome of executing a :class:`ProcessSpec`."""

    spec: ProcessSpec
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    state: ProcessState = ProcessState.COMPLETED

    @property
    def timed_out(self) -> bool:
        return self.state is ProcessState.TIMED_OUT

    @property
    def cancelled(self) -> bool:
        return self.state is ProcessState.CANCELLED

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and self.state is ProcessState.COMPLETED

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.spec.command,
            "cwd": self.spec.cwd,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "state": self.state.value,
            "stdout_len": len(self.stdout),
            "stderr_len": len(self.stderr),
        }


def now_iso() -> str:
    return datetime.now(UTC).isoformat()
