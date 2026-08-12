"""Agent session: composes the agent, tools, security, and events for one task.

A session is the top-level object the CLI (and later the API/worker) drives.
It owns the lifecycle of one agent run against a workspace.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kinetic.agent.adapter import AgentAdapter
from kinetic.config import Settings
from kinetic.events import EventBus
from kinetic.security import AuditLog, PermissionPolicy
from kinetic.tools.base import ToolDefinition, ToolRegistry
from kinetic.tools.filesystem import filesystem_tools
from kinetic.tools.terminal import CancellationToken, terminal_tool


@dataclass
class SessionConfig:
    """Inputs for creating a session."""

    workspace: Path
    prompt: str
    model: str | None = None
    max_turns: int | None = 40
    system_prompt: str | None = None
    allow_network: bool = False


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
        self.registry = self._build_registry(cfg.workspace)
        self.policy = PermissionPolicy(
            writable_roots=settings.writable_roots() + [cfg.workspace.resolve()],
            allow_network=cfg.allow_network,
            allow_execute=True,
        )
        self._adapter: AgentAdapter | None = None

    def _build_registry(self, workspace: Path) -> ToolRegistry:
        registry = ToolRegistry()
        for t in filesystem_tools(workspace):
            registry.register(t)
        registry.register(
            terminal_tool(
                cwd=str(workspace),
                default_timeout=self.settings.default_command_timeout,
                max_timeout=self.settings.max_command_timeout,
            )
        )
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
        """Connect, run the prompt, disconnect, and return the outcome."""
        adapter = self._adapter or self.build_adapter()
        events_snapshot: list[dict[str, Any]] = []
        async with self.events.subscribe() as sub:
            try:
                async with adapter:
                    result = await adapter.query(self.cfg.prompt, session_id=self.session_id)
            except Exception as exc:  # noqa: BLE001
                return SessionResult(
                    session_id=self.session_id,
                    success=False,
                    error=str(exc),
                    events=events_snapshot,
                )
            # Drain remaining events.
            async for ev in sub:
                events_snapshot.append(ev.to_dict())
                break  # only peek one post-run; full history below
        events_snapshot = [e.to_dict() for e in self.events.history]
        return SessionResult(
            session_id=self.session_id,
            success=bool(result and not getattr(result, "is_error", False)),
            result_text=getattr(result, "result", None) if result else None,
            events=events_snapshot,
        )


def build_session(settings: Settings, cfg: SessionConfig) -> AgentSession:
    return AgentSession(settings, cfg)


def default_tools_for(workspace: Path, settings: Settings) -> list[ToolDefinition]:
    """Return the Phase 1 tool set for a workspace (used by tests)."""
    tools = filesystem_tools(workspace)
    tools.append(
        terminal_tool(
            cwd=str(workspace),
            default_timeout=settings.default_command_timeout,
            max_timeout=settings.max_command_timeout,
        )
    )
    return tools
