"""Claude Agent SDK adapter.

A thin, stable interface around the SDK so the rest of KINETIC never imports
SDK specifics directly. Responsibilities:

  * build ``ClaudeAgentOptions`` from our own settings + tools
  * create an in-process MCP server exposing our tool registry
  * wire the runtime permission gate (``can_use_tool``) to our policy + audit
  * own session lifecycle (connect / query / receive / interrupt / disconnect)
  * translate SDK messages into KINETIC events on the event bus

We deliberately do NOT re-implement the agent loop — the SDK owns that. We only
adapt its primitives to our domain.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from errors import AgentError, PermissionDeniedError
from events import EventBus, EventType
from security import AuditLog, PermissionPolicy
from tools.base import ToolDefinition, ToolRegistry

logger = logging.getLogger(__name__)

# Import SDK lazily so the rest of the package can be imported (and unit-tested
# with a fake transport) even if the SDK is not installed at import time.
try:
    from claude_agent_sdk import (
        ClaudeAgentOptions,
        ClaudeSDKClient,
        PermissionResultAllow,
        PermissionResultDeny,
        create_sdk_mcp_server,
    )
    from claude_agent_sdk import (
        tool as sdk_tool,
    )
except ImportError as exc:  # pragma: no cover - import guard
    ClaudeAgentOptions = None  # type: ignore[assignment]
    ClaudeSDKClient = None  # type: ignore[assignment]
    PermissionResultAllow = None  # type: ignore[assignment]
    PermissionResultDeny = None  # type: ignore[assignment]
    create_sdk_mcp_server = None  # type: ignore[assignment]
    sdk_tool = None  # type: ignore[assignment]
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


MCP_SERVER_NAME = "kinetic"


def _to_sdk_tool(defn: ToolDefinition):
    """Wrap a KINETIC ToolDefinition into an SDK ``@tool``-decorated function."""
    return sdk_tool(
        defn.name,
        defn.description,
        defn.input_schema,
    )(defn.func)


class AgentAdapter:
    """Stable internal interface to the Claude Agent SDK."""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        policy: PermissionPolicy,
        audit: AuditLog,
        events: EventBus,
        cwd: Path,
        model: str = "claude-sonnet-4-5-20250929",
        permission_mode: str = "default",
        max_turns: int | None = 40,
        fallback_model: str | None = None,
        max_budget_usd: float | None = None,
        system_prompt: str | None = None,
        extra_allowed_tools: list[str] | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        interactive_approval: bool = False,
        approval_registry: Any = None,
        approval_timeout: float = 300.0,
    ) -> None:
        if _IMPORT_ERROR is not None:
            raise AgentError(
                "claude-agent-sdk is not installed; cannot create an agent adapter"
            ) from _IMPORT_ERROR
        self._registry = registry
        self._policy = policy
        self._audit = audit
        self._events = events
        self._cwd = Path(cwd).resolve()
        self._model = model
        self._base_url = base_url
        self._api_key = api_key
        self._interactive_approval = interactive_approval
        self._approval_registry = approval_registry
        self._approval_timeout = approval_timeout
        self._client: Any = None
        self._options = self._build_options(
            model=model,
            permission_mode=permission_mode,
            max_turns=max_turns,
            fallback_model=fallback_model,
            max_budget_usd=max_budget_usd,
            system_prompt=system_prompt,
            extra_allowed_tools=extra_allowed_tools,
            base_url=base_url,
            api_key=api_key,
        )

    # --- public API ---------------------------------------------------------

    @property
    def options(self) -> Any:
        return self._options

    async def __aenter__(self) -> AgentAdapter:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.disconnect()

    async def connect(self) -> None:
        try:
            self._client = ClaudeSDKClient(options=self._options)
            await self._client.connect()
        except Exception as exc:  # noqa: BLE001 - surface as AgentError
            raise AgentError(f"failed to connect agent: {exc}") from exc

    async def disconnect(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            finally:
                self._client = None

    async def interrupt(self) -> None:
        if self._client is not None:
            self._client.interrupt()

    async def query(self, prompt: str, *, session_id: str = "default") -> Any:
        """Send a prompt and stream messages, emitting events as they arrive.

        Returns the final ``ResultMessage`` (or ``None`` if the stream ended
        without one).
        """
        if self._client is None:
            raise AgentError("agent is not connected")
        self._events.emit(EventType.AGENT_STARTED, session_id, prompt=prompt)
        await self._client.query(prompt, session_id=session_id)
        result = None
        try:
            async for message in self._client.receive_response():
                result = self._handle_message(message, session_id) or result
        except Exception as exc:  # noqa: BLE001
            self._events.emit(EventType.AGENT_ERROR, session_id, error=str(exc))
            raise AgentError(f"agent stream failed: {exc}") from exc
        self._events.emit(EventType.TASK_COMPLETED, session_id, success=bool(result and not getattr(result, "is_error", False)))
        return result

    # --- internals ----------------------------------------------------------

    def _build_options(
        self,
        *,
        model: str,
        permission_mode: str,
        max_turns: int | None,
        fallback_model: str | None,
        max_budget_usd: float | None,
        system_prompt: str | None,
        extra_allowed_tools: list[str] | None,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> Any:
        sdk_tools = [_to_sdk_tool(t) for t in self._registry.all()]
        server = create_sdk_mcp_server(name=MCP_SERVER_NAME, version="1.0.0", tools=sdk_tools)
        allowed = [t.allowed_tool_name(MCP_SERVER_NAME) for t in self._registry.all()]
        if extra_allowed_tools:
            allowed.extend(extra_allowed_tools)

        kwargs: dict[str, Any] = {
            "model": model,
            "cwd": str(self._cwd),
            "permission_mode": permission_mode,
            "mcp_servers": {MCP_SERVER_NAME: server},
            "allowed_tools": allowed,
            "can_use_tool": self._can_use_tool,
        }
        # Forward LLM provider overrides to the Claude Code subprocess via env
        # vars. The API key is only ever placed in the subprocess env (never
        # logged, never persisted). base_url enables proxies/gateways.
        env_overrides: dict[str, str] = {}
        if base_url:
            env_overrides["ANTHROPIC_BASE_URL"] = base_url
        if api_key:
            env_overrides["ANTHROPIC_API_KEY"] = api_key
        if env_overrides:
            kwargs["env"] = env_overrides
        if max_turns is not None:
            kwargs["max_turns"] = max_turns
        if fallback_model is not None:
            kwargs["fallback_model"] = fallback_model
        if max_budget_usd is not None:
            kwargs["max_budget_usd"] = max_budget_usd
        if system_prompt:
            kwargs["system_prompt"] = system_prompt
        return ClaudeAgentOptions(**kwargs)

    async def _can_use_tool(
        self, tool_name: str, tool_input: dict[str, Any], context: Any
    ) -> Any:
        """Runtime permission gate invoked by the SDK before each tool call.

        This is the real security boundary: it runs *before* the tool, regardless
        of prompt instructions. Decisions are audited. When interactive approval
        is enabled, an allowed tool still awaits a human decision — interactive
        approval never relaxes the static policy, it only adds a checkpoint.
        """
        # SDK exposes MCP tools as mcp__server__name; strip to our registry name.
        local = tool_name.split("__")[-1] if "__" in tool_name else tool_name
        defn = self._registry.get(local)
        session_id = getattr(context, "session_id", "unknown") if context is not None else "unknown"
        if defn is None:
            self._audit.record(session_id=session_id, action="permission", tool=tool_name, allowed=False, reason="unknown tool")
            return PermissionResultDeny(message="unknown tool")
        try:
            self._policy.require(tool_name, defn.permission, tool_input)
        except PermissionDeniedError as exc:
            self._audit.record(session_id=session_id, action="permission", tool=tool_name, allowed=False, reason=exc.reason)
            self._events.emit(EventType.PERMISSION_DENIED, session_id, tool=tool_name, reason=exc.reason)
            return PermissionResultDeny(message=exc.reason)
        # Static policy allowed the tool. If interactive approval is on, ask the
        # human before proceeding (bounded by a timeout; timeout => deny).
        if self._interactive_approval and self._approval_registry is not None:
            req = self._approval_registry.request(
                task_id=session_id, tool=tool_name, tool_input=tool_input,
                reason=getattr(defn, "description", "") or "",
            )
            allowed = await self._approval_registry.await_decision(req, timeout=self._approval_timeout)
            self._audit.record(
                session_id=session_id, action="permission", tool=tool_name,
                allowed=allowed, reason="interactive:" + ("allow" if allowed else "deny"),
            )
            if not allowed:
                self._events.emit(EventType.PERMISSION_DENIED, session_id, tool=tool_name, reason="denied by user")
                return PermissionResultDeny(message="denied by user")
            return PermissionResultAllow(updated_input=tool_input)
        self._audit.record(session_id=session_id, action="permission", tool=tool_name, allowed=True)
        return PermissionResultAllow(updated_input=tool_input)

    def _handle_message(self, message: Any, session_id: str) -> Any:
        """Translate one SDK message into KINETIC events. Returns ResultMessage if any."""
        mtype = type(message).__name__
        if mtype == "AssistantMessage":
            self._handle_assistant(message, session_id)
        elif mtype == "UserMessage":
            self._handle_user(message, session_id)
        elif mtype == "SystemMessage":
            self._events.emit(EventType.AGENT_MESSAGE, session_id, kind="system", subtype=getattr(message, "subtype", ""))
        elif mtype == "ResultMessage":
            self._events.emit(
                EventType.TASK_COMPLETED,
                session_id,
                is_error=getattr(message, "is_error", False),
                result=getattr(message, "result", None),
                num_turns=getattr(message, "num_turns", 0),
                duration_ms=getattr(message, "duration_ms", 0),
            )
            return message
        elif mtype == "StreamEvent":
            self._events.emit(EventType.AGENT_MESSAGE, session_id, kind="stream", event=getattr(message, "event", {}))
        return None

    def _handle_assistant(self, message: Any, session_id: str) -> None:
        for block in getattr(message, "content", []) or []:
            btype = getattr(block, "type", None) or type(block).__name__
            if btype == "text":
                self._events.emit(EventType.AGENT_MESSAGE, session_id, kind="text", text=getattr(block, "text", ""))
            elif btype == "ToolUseBlock" or btype == "tool_use":
                self._events.emit(
                    EventType.TOOL_STARTED,
                    session_id,
                    tool=getattr(block, "name", ""),
                    tool_input=getattr(block, "input", {}),
                )

    def _handle_user(self, message: Any, session_id: str) -> None:
        for block in getattr(message, "content", []) or []:
            btype = getattr(block, "type", None) or type(block).__name__
            if btype == "ToolResultBlock" or btype == "tool_result":
                self._events.emit(
                    EventType.TOOL_FINISHED,
                    session_id,
                    content=str(getattr(block, "content", "")),
                )
