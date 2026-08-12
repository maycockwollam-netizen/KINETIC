"""Audit logging for security-sensitive operations.

Every permission decision (allow or deny) and every tool invocation is recorded
so that security-sensitive actions are auditable after the fact. The log is
append-only and human-readable (JSON-lines), suitable for Phase 7 auditing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


class AuditLog:
    """Append-only JSONL audit log."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        *,
        session_id: str,
        action: str,
        tool: str | None = None,
        allowed: bool | None = None,
        reason: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        entry = {
            "id": uuid4().hex,
            "ts": datetime.now(UTC).isoformat(),
            "session_id": session_id,
            "action": action,
            "tool": tool,
            "allowed": allowed,
            "reason": reason,
            "detail": detail or {},
        }
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    def read(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        entries: list[dict[str, Any]] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entries.append(json.loads(line))
        return entries
