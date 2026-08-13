"""Workspace metadata."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path


@dataclass
class WorkspaceMeta:
    """Structured metadata about a workspace."""

    root: Path
    status: str
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    parent: Path | None = None
    readonly: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "root": str(self.root),
            "status": self.status,
            "created_at": self.created_at,
            "parent": str(self.parent) if self.parent else None,
            "readonly": self.readonly,
        }
