"""Structured memory models and scopes.

A memory record is a single curated fact (NOT a raw conversation turn). Each
record carries rich metadata so the retrieval layer can rank by importance,
confidence, recency, source trust, and project scope without re-reading content.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class MemoryScope(StrEnum):
    """The breadth and lifetime of a memory.

    EPHEMERAL: current execution context; may vanish after the task completes.
    TASK:      knowledge specific to one task; survives only if persisted.
    PROJECT:   repository/project knowledge (architecture, conventions, facts).
    AGENT:     stable agent-level knowledge, carefully curated.
    """

    EPHEMERAL = "ephemeral"
    TASK = "task"
    PROJECT = "project"
    AGENT = "agent"

    @property
    def persistent(self) -> bool:
        """Whether this scope is meant to outlive a single task by default."""
        return self in (MemoryScope.PROJECT, MemoryScope.AGENT)


def compute_content_hash(content: str, *, scope: MemoryScope, project_id: str | None) -> str:
    """A stable dedup key over (content, scope, project).

    Repeatedly storing the same fact under the same scope+project must not
    create unlimited duplicates; the hash lets the store detect an existing
    non-invalidated record and return it instead of inserting a copy.
    """
    h = hashlib.sha256()
    h.update(scope.value.encode())
    h.update(b"\x1f")
    h.update((project_id or "").encode())
    h.update(b"\x1f")
    h.update(content.strip().encode())
    return h.hexdigest()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class MemoryRecord(BaseModel):
    """A single persistent memory record with structured metadata."""

    id: str
    content: str
    memory_type: MemoryScope
    #: Logical grouping string (free-form, e.g. "architecture", "conventions").
    scope: str = ""
    project_id: str | None = None
    workspace_id: str | None = None
    source: str = "agent"
    created_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)
    last_accessed_at: str = Field(default_factory=_now_iso)
    importance: float = 0.5
    confidence: float = 0.5
    tags: list[str] = Field(default_factory=list)
    content_hash: str = ""
    #: Embedding vector reference (stored inline for the local backend).
    embedding: list[float] | None = None
    invalidated: bool = False
    invalidated_at: str | None = None
    version: int = 1
    superseded_by: str | None = None

    @field_validator("importance", "confidence")
    @classmethod
    def _bounded_zero_one(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("importance/confidence must be in [0.0, 1.0]")
        return v

    def to_storage_dict(self) -> dict[str, Any]:
        """Row representation for the SQLite store (JSON-encodable)."""
        import json

        return {
            "id": self.id,
            "content": self.content,
            "memory_type": self.memory_type.value,
            "scope": self.scope,
            "project_id": self.project_id,
            "workspace_id": self.workspace_id,
            "source": self.source,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_accessed_at": self.last_accessed_at,
            "importance": self.importance,
            "confidence": self.confidence,
            "tags": json.dumps(self.tags),
            "content_hash": self.content_hash,
            "embedding": json.dumps(self.embedding) if self.embedding is not None else None,
            "invalidated": self.invalidated,
            "invalidated_at": self.invalidated_at,
            "version": self.version,
            "superseded_by": self.superseded_by,
        }


class RankedMemory(BaseModel):
    """A memory plus the retrieval signals that selected it."""

    record: MemoryRecord
    final_score: float
    components: dict[str, float] = Field(default_factory=dict)
    source_matches: list[str] = Field(default_factory=list)

    model_config = {"arbitrary_types_allowed": True}
