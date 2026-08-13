"""Agent session: composes the agent, tools, security, and events for one task.

A session is the top-level object the CLI (and later the API/worker) drives.
It owns the lifecycle of one agent run against a workspace.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kinetic.agent.adapter import AgentAdapter
from kinetic.config import Settings
from kinetic.events import EventBus, EventType
from kinetic.security import AuditLog, PermissionPolicy
from kinetic.tools.base import ToolDefinition, ToolRegistry
from kinetic.tools.filesystem import filesystem_tools
from kinetic.tools.git import git_tools
from kinetic.tools.project import project_tools
from kinetic.tools.terminal import CancellationToken, terminal_tool

if TYPE_CHECKING:
    from kinetic.environment import Environment


@dataclass
class SessionConfig:
    """Inputs for creating a session."""

    workspace: Path
    prompt: str
    model: str | None = None
    max_turns: int | None = 40
    system_prompt: str | None = None
    allow_network: bool = False
    allow_git_write: bool = False
    allow_dependency_install: bool = False
    # Phase 3 — environment / sandbox
    runtime_type: str | None = None  # None -> use settings default
    sandbox_mode: bool | None = None
    network_policy: str | None = None
    allow_environment_exec: bool = True
    allow_environment_network: bool = False
    allow_environment_admin: bool = False


@dataclass
class SessionResult:
    session_id: str
    success: bool
    result_text: str | None = None
    error: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)


class AgentSession:
    """One agent run: tools + security + events + the SDK adapter."""

    def __init__(
        self,
        settings: Settings,
        cfg: SessionConfig,
        *,
        session_id: str | None = None,
        cancellation: CancellationToken | None = None,
    ) -> None:
        self.settings = settings
        self.cfg = cfg
        self.session_id = session_id or uuid.uuid4().hex
        self.cancellation = cancellation or CancellationToken()
        self.events = EventBus()
        self.audit = AuditLog(settings.audit_log_path)
        self.policy = PermissionPolicy(
            writable_roots=settings.writable_roots() + [cfg.workspace.resolve()],
            allow_network=cfg.allow_network,
            allow_execute=True,
            allow_git_write=cfg.allow_git_write,
            allow_dependency_install=cfg.allow_dependency_install,
            allow_environment_exec=cfg.allow_environment_exec,
            allow_environment_network=cfg.allow_environment_network,
            allow_environment_admin=cfg.allow_environment_admin,
        )
        self.environment = self._build_environment()
        self.registry = self._build_registry(cfg.workspace)
        self._adapter: AgentAdapter | None = None

    def _build_environment(self) -> Environment:
        """Build the sandboxed execution environment for this session."""
        from kinetic.environment import Environment

        # Merge session overrides onto settings.
        runtime_type = self.cfg.runtime_type or self.settings.runtime_type
        sandbox_mode = self.cfg.sandbox_mode
        if sandbox_mode is None:
            sandbox_mode = self.settings.sandbox_mode or runtime_type == "docker"
        network_policy = self.cfg.network_policy or self.settings.network_policy

        from kinetic.environment.config import EnvironmentConfig
        from kinetic.environment.envvars import EnvironmentVariablePolicy
        from kinetic.environment.network import NetworkPolicy
        from kinetic.environment.resources import ResourceLimits

        net = NetworkPolicy(network_policy)
        env_vars = EnvironmentVariablePolicy(allowed=set(self.settings.allowed_env_vars))
        resources = ResourceLimits(
            cpu=self.settings.cpu_limit,
            memory_bytes=(self.settings.memory_limit_mb * 1024 * 1024) if self.settings.memory_limit_mb else None,
            process_count=self.settings.process_limit,
            execution_timeout=self.settings.execution_timeout,
            disk_bytes=(self.settings.disk_limit_mb * 1024 * 1024) if self.settings.disk_limit_mb else None,
        )
        config = EnvironmentConfig(
            runtime_type=("docker" if runtime_type == "docker" else "local"),
            sandbox_mode=sandbox_mode,
            network=net,
            resources=resources,
            env_vars=env_vars,
            image=self.settings.docker_image,
        )
        return Environment(
            self.cfg.workspace,
            config,
            policy=self.policy,
            audit=self.audit,
            events=self.events,
            session_id=self.session_id,
        )

    def _build_registry(self, workspace: Path) -> ToolRegistry:
        registry = ToolRegistry()
        for t in filesystem_tools(workspace):
            registry.register(t)
        registry.register(
            terminal_tool(
                cwd=str(workspace),
                default_timeout=self.settings.default_command_timeout,
                max_timeout=self.settings.max_command_timeout,
                environment=self.environment,
            )
        )
        for t in git_tools(
            workspace=workspace,
            policy=self.policy,
            audit=self.audit,
            events=self.events,
            session_id=self.session_id,
            default_timeout=self.settings.default_command_timeout,
            max_timeout=self.settings.max_command_timeout,
        ):
            registry.register(t)
        for t in project_tools(
            workspace=workspace,
            policy=self.policy,
            audit=self.audit,
            events=self.events,
            session_id=self.session_id,
            default_timeout=self.settings.default_command_timeout,
            max_timeout=self.settings.max_command_timeout,
        ):
            registry.register(t)
        return registry

    def build_adapter(self) -> AgentAdapter:
        """Construct (but do not connect) the adapter. Mainly for testing."""
        self._adapter = AgentAdapter(
            registry=self.registry,
            policy=self.policy,
            audit=self.audit,
            events=self.events,
            cwd=self.cfg.workspace,
            model=self.cfg.model or self.settings.model,
            permission_mode=self.settings.permission_mode,
            max_turns=self.cfg.max_turns,
            fallback_model=self.settings.fallback_model,
            max_budget_usd=self.settings.max_budget_usd,
            system_prompt=self.cfg.system_prompt,
        )
        return self._adapter

    async def run(self) -> SessionResult:
        """Connect, run the prompt, disconnect, and return the outcome.

        The sandboxed environment is always torn down (stop + destroy) in a
        ``finally`` block, so a provisioning failure, a model error, or an
        interrupted session never leaves a container behind.
        """
        adapter = self._adapter or self.build_adapter()
        events_snapshot: list[dict[str, Any]] = []
        # Provision the sandboxed environment for this run; fail closed if it
        # cannot be brought up (e.g. docker unavailable with docker requested).
        try:
            await self.environment.provision()
        except Exception as exc:  # noqa: BLE001
            await self._teardown_environment()
            return SessionResult(
                session_id=self.session_id,
                success=False,
                error=f"environment provisioning failed: {exc}",
                events=[e.to_dict() for e in self.events.history],
            )
        result: Any = None
        try:
            sub = await self.events.subscribe()
            try:
                try:
                    async with adapter:
                        result = await adapter.query(self.cfg.prompt, session_id=self.session_id)
                except Exception as exc:  # noqa: BLE001
                    return SessionResult(
                        session_id=self.session_id,
                        success=False,
                        error=str(exc),
                        events=[e.to_dict() for e in self.events.history],
                    )
                # Drain remaining events.
                async for ev in sub:
                    events_snapshot.append(ev.to_dict())
                    break  # only peek one post-run; full history below
            finally:
                sub.close()
            events_snapshot = [e.to_dict() for e in self.events.history]
            success = bool(result and not getattr(result, "is_error", False))
            result_text = getattr(result, "result", None) if result else None
        finally:
            await self._teardown_environment()
        return SessionResult(
            session_id=self.session_id,
            success=success,
            result_text=result_text,
            events=events_snapshot,
        )

    async def _teardown_environment(self) -> None:
        """Stop + destroy the environment, surfacing — but not fatal — failures.

        Cleanup failures are recorded as events/audit by the Environment rather
        than silently swallowed, yet never mask the run's own outcome.
        """
        try:
            await self.environment.stop()
        except Exception as exc:  # noqa: BLE001 - record, continue to destroy
            self.events.emit(EventType.ENVIRONMENT_FAILED, self.session_id,
                             reason=f"stop failed: {exc}", runtime=self.environment.config.runtime_type)
        try:
            await self.environment.destroy()
        except Exception as exc:  # noqa: BLE001 - record, do not raise
            self.events.emit(EventType.ENVIRONMENT_FAILED, self.session_id,
                             reason=f"destroy failed: {exc}", runtime=self.environment.config.runtime_type)


def build_session(settings: Settings, cfg: SessionConfig) -> AgentSession:
    return AgentSession(settings, cfg)


def default_tools_for(workspace: Path, settings: Settings) -> list[ToolDefinition]:
    """Return the full registered tool set for a workspace (used by tests).

    Uses the low-level terminal path (no environment) to keep tests independent
    of sandbox availability.
    """
    from kinetic.security import AuditLog, PermissionPolicy

    policy = PermissionPolicy(writable_roots=[workspace.resolve()])
    audit = AuditLog(settings.audit_log_path)
    tools: list[ToolDefinition] = filesystem_tools(workspace)
    tools.append(
        terminal_tool(
            cwd=str(workspace),
            default_timeout=settings.default_command_timeout,
            max_timeout=settings.max_command_timeout,
        )
    )
    tools.extend(
        git_tools(
            workspace=workspace,
            policy=policy,
            audit=audit,
            default_timeout=settings.default_command_timeout,
            max_timeout=settings.max_command_timeout,
        )
    )
    tools.extend(
        project_tools(
            workspace=workspace,
            policy=policy,
            audit=audit,
            default_timeout=settings.default_command_timeout,
            max_timeout=settings.max_command_timeout,
        )
    )
    return tools
