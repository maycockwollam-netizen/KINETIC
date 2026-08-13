"""Environment runtime abstraction.

The agent depends on this interface, never on a concrete runtime (Docker or
otherwise). Only the concrete implementations (``local.py``, ``docker.py``)
know how isolation is actually realized.

A runtime maps the abstract :class:`~kinetic.environment.config.EnvironmentConfig`
onto its native primitives:

    create  -> provision an isolated execution context
    start   -> make it runnable (no-op for some runtimes)
    exec    -> run one :class:`~kinetic.environment.process.ProcessSpec` inside it
    stop    -> stop the context (processes terminated)
    destroy -> release all resources
    inspect -> report runtime-specific status

Each method may raise :class:`~kinetic.errors.SandboxError` /
:class:`~kinetic.errors.RuntimeUnavailableError`. Critically, a runtime must
**fail closed**: if it cannot enforce a requested policy or limit it raises
rather than silently downgrading.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kinetic.environment.config import EnvironmentConfig
from kinetic.environment.process import ProcessResult, ProcessSpec
from kinetic.tools.terminal import CancellationToken


@dataclass
class RuntimeStatus:
    """Runtime-specific status reported by ``inspect``."""

    runtime_type: str
    ready: bool
    detail: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "runtime_type": self.runtime_type,
            "ready": self.ready,
            "detail": self.detail,
        }


class EnvironmentRuntime(ABC):
    """Abstract execution runtime behind the environment boundary."""

    #: Subclasses set this to their config ``runtime_type`` value.
    runtime_type: str = "abstract"

    def __init__(self, workspace: Path, config: EnvironmentConfig) -> None:
        self._workspace = workspace.resolve()
        self._config = config
        #: Optional correlation id set by the Environment so concrete runtimes
        #: can tag the resources they create (e.g. container labels) for
        #: ownership tracking and leak detection. ``None`` until assigned.
        self.session_id: str | None = None

    @property
    def workspace(self) -> Path:
        return self._workspace

    @property
    def config(self) -> EnvironmentConfig:
        return self._config

    @abstractmethod
    async def create(self) -> None:
        """Provision the isolated context. Raise on any failure (fail closed)."""

    @abstractmethod
    async def start(self) -> None:
        """Transition to runnable."""

    @abstractmethod
    async def exec(self, spec: ProcessSpec, *, cancellation: CancellationToken | None = None) -> ProcessResult:
        """Execute one process spec inside the context."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the context and terminate its processes."""

    @abstractmethod
    async def destroy(self) -> None:
        """Release all resources. Idempotent."""

    @abstractmethod
    async def inspect(self) -> RuntimeStatus:
        """Report runtime-specific status."""

    @staticmethod
    async def probe() -> bool:
        """Return True if this runtime is available in the current environment.

        Used by integration tests and fail-closed fallback decisions. Cheap,
        non-mutating, and never raises.
        """
        return False
