"""Agent-callable memory tools.

These integrate with the EXISTING ToolRegistry — no second registry. Dangerous
operations (create/update/delete) require explicit permission via the
permission policy (MEMORY_WRITE / MEMORY_DELETE), which are off by default.

Memory tools never log full sensitive content; they surface memory ids, types,
scores, and short previews only. Secret-like content is rejected before
persistence by the MemoryManager itself (defense in depth: the tool also
validates, but the manager is the authority).
"""

from __future__ import annotations

import json
from typing import Any

from kinetic.errors import MemoryError, MemorySecurityError, PermissionDeniedError
from kinetic.events import EventBus, EventType
from kinetic.memory.lifecycle import MemoryManager
from kinetic.memory.metadata import MemoryFilter
from kinetic.memory.models import MemoryScope
from kinetic.security import AuditLog, PermissionPolicy
from kinetic.security.policy import MEMORY_DELETE, MEMORY_READ, MEMORY_WRITE
from kinetic.tools.base import ToolDefinition, tool_result

# How many chars of a memory to preview in tool output (never the full secret-
# bearing content, even though persistence already rejects it).
_PREVIEW_CHARS = 200


class MemoryTools:
    """Builds the agent-facing memory tool set bound to a MemoryManager."""

    def __init__(
        self,
        *,
        manager: MemoryManager,
        policy: PermissionPolicy,
        audit: AuditLog,
        events: EventBus | None = None,
        session_id: str = "memory",
    ) -> None:
        self._mgr = manager
        self._policy = policy
        self._audit = audit
        self._events = events
        self._session_id = session_id

    def _preview(self, content: str) -> str:
        return content[:_PREVIEW_CHARS] + ("…" if len(content) > _PREVIEW_CHARS else "")

    async def search(self, args: dict[str, Any]) -> dict[str, Any]:
        op = "memory_search"
        self._require(op, MEMORY_READ, {"query": args.get("query", "")})
        query = args.get("query", "")
        limit = int(args.get("limit", 5))
        project_id = args.get("project_id")
        mem_filter = MemoryFilter.for_project(project_id=project_id) if project_id else None
        ranked = await self._mgr.search(query, filter=mem_filter, limit=limit)
        out = [
            {
                "id": r.record.id,
                "type": r.record.memory_type.value,
                "score": round(r.final_score, 4),
                "components": {k: round(v, 4) for k, v in r.components.items()},
                "content": self._preview(r.record.content),
                "tags": r.record.tags,
            }
            for r in ranked
        ]
        return tool_result(json.dumps(out, ensure_ascii=False))

    async def get(self, args: dict[str, Any]) -> dict[str, Any]:
        op = "memory_get"
        self._require(op, MEMORY_READ, {"memory_id": args.get("id", "")})
        mid = args.get("id")
        if not isinstance(mid, str) or not mid.strip():
            return tool_result("error: 'id' is required", is_error=True)
        record = await self._mgr.retrieve(mid)
        if record is None:
            return tool_result(f"memory not found: {mid}", is_error=True)
        return tool_result(json.dumps({
            "id": record.id,
            "type": record.memory_type.value,
            "content": record.content,
            "project_id": record.project_id,
            "tags": record.tags,
            "importance": record.importance,
            "confidence": record.confidence,
            "invalidated": record.invalidated,
        }, ensure_ascii=False))

    async def create(self, args: dict[str, Any]) -> dict[str, Any]:
        op = "memory_create"
        self._require(op, MEMORY_WRITE, {"content": args.get("content", "")})
        try:
            record = await self._mgr.create(
                content=args["content"],
                memory_type=MemoryScope(args.get("memory_type", "task")),
                scope=args.get("scope", ""),
                project_id=args.get("project_id"),
                workspace_id=args.get("workspace_id"),
                source=args.get("source", "agent"),
                importance=float(args.get("importance", 0.5)),
                confidence=float(args.get("confidence", 0.5)),
                tags=args.get("tags") or [],
            )
        except MemorySecurityError as exc:
            return tool_result(f"rejected: {exc}", is_error=True)
        except MemoryError as exc:
            return tool_result(f"error: {exc}", is_error=True)
        return tool_result(json.dumps({
            "id": record.id, "type": record.memory_type.value, "created": True,
        }, ensure_ascii=False))

    async def update(self, args: dict[str, Any]) -> dict[str, Any]:
        op = "memory_update"
        self._require(op, MEMORY_WRITE, {"memory_id": args.get("id", "")})
        try:
            record = await self._mgr.update(
                args["id"],
                content=args.get("content"),
                importance=float(args["importance"]) if "importance" in args else None,
                confidence=float(args["confidence"]) if "confidence" in args else None,
                tags=args.get("tags"),
            )
        except MemorySecurityError as exc:
            return tool_result(f"rejected: {exc}", is_error=True)
        except MemoryError as exc:
            return tool_result(f"error: {exc}", is_error=True)
        return tool_result(json.dumps({"id": record.id, "updated": True, "version": record.version}, ensure_ascii=False))

    async def delete(self, args: dict[str, Any]) -> dict[str, Any]:
        op = "memory_delete"
        self._require(op, MEMORY_DELETE, {"memory_id": args.get("id", "")})
        try:
            deleted = await self._mgr.delete(args["id"])
        except MemoryError as exc:
            return tool_result(f"error: {exc}", is_error=True)
        return tool_result(json.dumps({"id": args["id"], "deleted": deleted}, ensure_ascii=False))

    def _require(self, tool_name: str, permission: Any, tool_input: dict[str, Any]) -> None:
        try:
            self._policy.require(tool_name, permission, tool_input)
        except PermissionDeniedError as exc:
            self._audit.record(
                session_id=self._session_id, action="permission",
                tool=tool_name, allowed=False, reason=exc.reason,
            )
            if self._events is not None:
                self._events.emit(EventType.PERMISSION_DENIED, self._session_id, tool=tool_name, reason=exc.reason)
            raise


# --- schemas --------------------------------------------------------------

_SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Natural-language search query."},
        "limit": {"type": "integer", "default": 5},
        "project_id": {"type": "string"},
    },
    "required": ["query"],
}

_GET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"id": {"type": "string"}},
    "required": ["id"],
}

_CREATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "content": {"type": "string", "description": "The fact to remember."},
        "memory_type": {"type": "string", "enum": ["ephemeral", "task", "project", "agent"], "default": "task"},
        "scope": {"type": "string"},
        "project_id": {"type": "string"},
        "workspace_id": {"type": "string"},
        "importance": {"type": "number", "default": 0.5},
        "confidence": {"type": "number", "default": 0.5},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["content"],
}

_UPDATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "content": {"type": "string"},
        "importance": {"type": "number"},
        "confidence": {"type": "number"},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["id"],
}

_DELETE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"id": {"type": "string"}},
    "required": ["id"],
}


def memory_tools(
    *,
    manager: MemoryManager,
    policy: PermissionPolicy,
    audit: AuditLog,
    events: EventBus | None = None,
    session_id: str = "memory",
) -> list[ToolDefinition]:
    """Build the memory tool set bound to ``manager``."""
    mt = MemoryTools(manager=manager, policy=policy, audit=audit, events=events, session_id=session_id)
    return [
        ToolDefinition("memory_search", "Search memories (hybrid retrieval).", _SEARCH_SCHEMA, MEMORY_READ, mt.search),
        ToolDefinition("memory_get", "Fetch a single memory by id.", _GET_SCHEMA, MEMORY_READ, mt.get),
        ToolDefinition("memory_create", "Create a memory (secret-filtered). Requires memory_write.", _CREATE_SCHEMA, MEMORY_WRITE, mt.create),
        ToolDefinition("memory_update", "Update a memory. Requires memory_write.", _UPDATE_SCHEMA, MEMORY_WRITE, mt.update),
        ToolDefinition("memory_delete", "Delete a memory (restricted). Requires memory_delete.", _DELETE_SCHEMA, MEMORY_DELETE, mt.delete),
    ]
