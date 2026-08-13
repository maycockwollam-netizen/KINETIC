"""Project-scan + dependency tools exposed to the agent.

These wrap the project scanner and dependency subsystems as registered tools so
the agent can invoke them through the normal tool registry. They reuse the
existing permission policy and audit log; project scan is read-only, while
dependency installation requires DEPENDENCY_INSTALL.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dependencies.detector import detect_dependencies
from errors import PermissionDeniedError
from events import EventBus, EventType
from project.scanner import scan_project
from security import AuditLog, PermissionPolicy
from security.policy import DEPENDENCY_READ, ToolPermission
from tools.base import ToolDefinition, tool_result
from tools.terminal import CancellationToken

_SCAN_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}
_DETECT_DEPS_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}
_INSTALL_DEPS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"timeout": {"type": "number", "default": 300}},
}


class ProjectTools:
    """Agent-callable tools for project scanning and dependency ops."""

    def __init__(
        self,
        *,
        workspace: Path,
        policy: PermissionPolicy,
        audit: AuditLog,
        events: EventBus | None = None,
        session_id: str = "project",
        default_timeout: float = 300.0,
        max_timeout: float = 1800.0,
    ) -> None:
        self._workspace = workspace.resolve()
        self._policy = policy
        self._audit = audit
        self._events = events
        self._session_id = session_id
        # Lazy import avoids a circular dependency through tools.__init__.
        from dependencies.installer import DependencyInstaller

        self._installer = DependencyInstaller(
            workspace=self._workspace,
            policy=policy,
            audit=audit,
            events=events,
            session_id=session_id,
            default_timeout=default_timeout,
            max_timeout=max_timeout,
        )

    async def scan(self, args: dict[str, Any]) -> dict[str, Any]:
        self._policy.require("scan_project", DEPENDENCY_READ, {})
        self._audit.record(session_id=self._session_id, action="scan_project", tool="scan_project", allowed=True)
        manifest = scan_project(self._workspace)
        if self._events is not None:
            self._events.emit(EventType.PROJECT_SCANNED, self._session_id, **manifest.to_dict())
        return tool_result(_to_json(manifest.to_dict()))

    async def detect_deps(self, args: dict[str, Any]) -> dict[str, Any]:
        self._policy.require("detect_dependencies", DEPENDENCY_READ, {})
        deps = detect_dependencies(self._workspace)
        if self._events is not None:
            self._events.emit(
                EventType.DEPENDENCY_DETECTED,
                self._session_id,
                ecosystems=[d.ecosystem for d in deps],
            )
        return tool_result(_to_json([d.to_dict() for d in deps]))

    async def install_deps(self, args: dict[str, Any]) -> dict[str, Any]:
        timeout = float(args.get("timeout", 300))
        try:
            result = await self._installer.install(timeout=timeout, cancellation=CancellationToken())
        except PermissionDeniedError as exc:
            # Installer already audited the denial; re-surface as tool error text.
            return tool_result(f"permission denied: {exc.reason}", is_error=True)
        return tool_result(_to_json(result))


def _to_json(obj: Any) -> str:
    import json

    return json.dumps(obj, default=str, indent=2)


def project_tools(
    *,
    workspace: Path,
    policy: PermissionPolicy,
    audit: AuditLog,
    events: EventBus | None = None,
    session_id: str = "project",
    default_timeout: float = 300.0,
    max_timeout: float = 1800.0,
) -> list[ToolDefinition]:
    pt = ProjectTools(
        workspace=workspace,
        policy=policy,
        audit=audit,
        events=events,
        session_id=session_id,
        default_timeout=default_timeout,
        max_timeout=max_timeout,
    )
    return [
        ToolDefinition("scan_project", "Scan the project and return structured metadata.", _SCAN_SCHEMA, DEPENDENCY_READ, pt.scan),
        ToolDefinition("detect_dependencies", "Detect dependency ecosystems and package managers.", _DETECT_DEPS_SCHEMA, DEPENDENCY_READ, pt.detect_deps),
        ToolDefinition(
            "install_dependencies",
            "Install project dependencies using the detected package manager (requires permission).",
            _INSTALL_DEPS_SCHEMA,
            _install_permission(),
            pt.install_deps,
        ),
    ]


def _install_permission() -> ToolPermission:
    from security.policy import DEPENDENCY_INSTALL

    return DEPENDENCY_INSTALL
