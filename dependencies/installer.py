"""Dependency installation.

Runs the detected package manager's install command *inside the workspace*,
enforcing:

  * workspace boundary (never outside the managed root)
  * PermissionPolicy (DEPENDENCY_INSTALL capability)
  * timeout + cancellation
  * structured event emission
  * audit logging

It never installs globally, never modifies global package-manager config, and
never bypasses the permission system.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dependencies.detector import detect_primary
from dependencies.models import DependencyInfo
from errors import DependencyError, PermissionDeniedError
from events import EventBus, EventType
from security import AuditLog, PermissionPolicy
from security.policy import DEPENDENCY_INSTALL
from tools.terminal import CancellationToken, run_command


class DependencyInstaller:
    """Installs dependencies for a project inside its workspace."""

    def __init__(
        self,
        *,
        workspace: Path,
        policy: PermissionPolicy,
        audit: AuditLog,
        events: EventBus | None = None,
        session_id: str = "deps",
        default_timeout: float = 300.0,
        max_timeout: float = 1800.0,
    ) -> None:
        self._workspace = workspace.resolve()
        self._policy = policy
        self._audit = audit
        self._events = events
        self._session_id = session_id
        self._default_timeout = default_timeout
        self._max_timeout = max_timeout

    async def install(
        self,
        info: DependencyInfo | None = None,
        *,
        timeout: float | None = None,
        cancellation: CancellationToken | None = None,
    ) -> dict[str, Any]:
        """Install dependencies for the workspace's project.

        If ``info`` is None, detection runs first. Returns a structured result.
        """
        if info is None:
            info = detect_primary(self._workspace)
        if info is None:
            raise DependencyError("no dependencies detected in workspace")

        # Enforce the workspace boundary: install dir must be the workspace.
        if Path(info.project_dir).resolve() != self._workspace:
            raise DependencyError(
                "detected project directory is outside the managed workspace",
                ecosystem=info.ecosystem,
                command=info.install_command,
            )

        # Runtime permission gate (before any subprocess).
        try:
            self._policy.require("install_dependencies", DEPENDENCY_INSTALL, {"command": info.install_command})
        except PermissionDeniedError as exc:
            self._audit.record(
                session_id=self._session_id,
                action="dependency_install",
                tool="install_dependencies",
                allowed=False,
                reason=exc.reason,
                detail={"ecosystem": info.ecosystem, "package_manager": info.package_manager},
            )
            raise

        timeout = min(timeout or self._default_timeout, self._max_timeout)
        self._emit(
            EventType.DEPENDENCY_INSTALL_STARTED,
            ecosystem=info.ecosystem,
            package_manager=info.package_manager,
            command=info.install_command,
        )
        self._audit.record(
            session_id=self._session_id,
            action="dependency_install",
            tool="install_dependencies",
            allowed=True,
            detail={"ecosystem": info.ecosystem, "command": info.install_command},
        )

        result = await run_command(
            info.install_command,
            cwd=str(self._workspace),
            timeout=timeout,
            cancellation=cancellation,
        )
        self._emit(
            EventType.DEPENDENCY_INSTALL_FINISHED,
            ecosystem=info.ecosystem,
            package_manager=info.package_manager,
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            duration_ms=result.duration_ms,
        )
        if result.exit_code != 0:
            raise DependencyError(
                "dependency installation failed",
                ecosystem=info.ecosystem,
                command=info.install_command,
                exit_code=result.exit_code,
            )
        return {
            "ecosystem": info.ecosystem,
            "package_manager": info.package_manager,
            "command": info.install_command,
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "duration_ms": result.duration_ms,
            "timed_out": result.timed_out,
        }

    def _emit(self, event_type: EventType, **data: object) -> None:
        if self._events is not None:
            self._events.emit(event_type, self._session_id, **data)
