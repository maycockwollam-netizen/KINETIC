"""Memory persistence abstraction and local SQLite backend.

The agent never depends directly on a specific backend. :class:`MemoryStore` is
the interface; :class:`SQLiteStore` is the Phase 4 local implementation
(metadata + content + embeddings in a single SQLite file, deterministic and
testable, no external service required).

Vector search is implemented in-process: embeddings are stored as JSON blobs and
cosine similarity is computed against the query embedding over the candidate
set (bounded by the filter + a candidate limit). Lexical search uses a
tokenized LIKE scan over a normalized content column, also bounded. This keeps
Phase 4 free of paid infra while remaining correct.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from errors import StorageError
from memory.embeddings import cosine_similarity
from memory.metadata import MemoryFilter
from memory.models import MemoryRecord, MemoryScope, compute_content_hash


class MemoryStore(ABC):
    """Persistence abstraction for memory records."""

    @abstractmethod
    def put(self, record: MemoryRecord) -> MemoryRecord: ...

    @abstractmethod
    def get(self, memory_id: str) -> MemoryRecord | None: ...

    @abstractmethod
    def update(self, memory_id: str, fields: dict[str, Any]) -> MemoryRecord | None: ...

    @abstractmethod
    def delete(self, memory_id: str) -> bool: ...

    @abstractmethod
    def list(self, filter: MemoryFilter | None = None, *, limit: int | None = None) -> list[MemoryRecord]: ...

    @abstractmethod
    def search_lexical(
        self, query: str, filter: MemoryFilter | None = None, *, limit: int = 50
    ) -> list[tuple[MemoryRecord, float]]: ...

    @abstractmethod
    def search_vector(
        self, query_embedding: list[float], filter: MemoryFilter | None = None, *, limit: int = 50
    ) -> list[tuple[MemoryRecord, float]]: ...

    @abstractmethod
    def invalidate(self, memory_id: str) -> MemoryRecord | None: ...

    @abstractmethod
    def find_duplicates(
        self, content_hash: str, *, project_id: str | None = None
    ) -> list[MemoryRecord]: ...

    @abstractmethod
    def touch(self, memory_id: str) -> None: ...

    @abstractmethod
    def close(self) -> None: ...


_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    memory_type TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT '',
    project_id TEXT,
    workspace_id TEXT,
    source TEXT NOT NULL DEFAULT 'agent',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_accessed_at TEXT NOT NULL,
    importance REAL NOT NULL DEFAULT 0.5,
    confidence REAL NOT NULL DEFAULT 0.5,
    tags TEXT NOT NULL DEFAULT '[]',
    content_hash TEXT NOT NULL DEFAULT '',
    embedding TEXT,
    invalidated INTEGER NOT NULL DEFAULT 0,
    invalidated_at TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    superseded_by TEXT
)
"""

_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_mem_project ON memories(project_id)"
)
_HASH_INDEX_SQL = "CREATE INDEX IF NOT EXISTS idx_mem_hash ON memories(content_hash)"
_TYPE_INDEX_SQL = "CREATE INDEX IF NOT EXISTS idx_mem_type ON memories(memory_type)"
_INVALIDATED_INDEX_SQL = "CREATE INDEX IF NOT EXISTS idx_mem_invalidated ON memories(invalidated)"


def _row_to_record(row: sqlite3.Row) -> MemoryRecord:
    tags_raw = row["tags"]
    tags: list[str] = json.loads(tags_raw) if tags_raw else []
    emb_raw = row["embedding"]
    embedding: list[float] | None = json.loads(emb_raw) if emb_raw else None
    return MemoryRecord(
        id=row["id"],
        content=row["content"],
        memory_type=MemoryScope(row["memory_type"]),
        scope=row["scope"],
        project_id=row["project_id"],
        workspace_id=row["workspace_id"],
        source=row["source"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_accessed_at=row["last_accessed_at"],
        importance=row["importance"],
        confidence=row["confidence"],
        tags=tags,
        content_hash=row["content_hash"],
        embedding=embedding,
        invalidated=bool(row["invalidated"]),
        invalidated_at=row["invalidated_at"],
        version=row["version"],
        superseded_by=row["superseded_by"],
    )


class SQLiteStore(MemoryStore):
    """Local persistent memory store backed by a single SQLite file.

    Thread-safe via a per-instance lock (connections are not shareable across
    threads by default). All operations are synchronous; the MemoryManager wraps
    them for the async layer.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_CREATE_SQL)
        self._conn.execute(_INDEX_SQL)
        self._conn.execute(_HASH_INDEX_SQL)
        self._conn.execute(_TYPE_INDEX_SQL)
        self._conn.execute(_INVALIDATED_INDEX_SQL)
        self._conn.commit()

    # --- core CRUD ---------------------------------------------------------

    def put(self, record: MemoryRecord) -> MemoryRecord:
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO memories "
                    "(id, content, memory_type, scope, project_id, workspace_id, source, "
                    " created_at, updated_at, last_accessed_at, importance, confidence, "
                    " tags, content_hash, embedding, invalidated, invalidated_at, "
                    " version, superseded_by) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        record.id,
                        record.content,
                        record.memory_type.value,
                        record.scope,
                        record.project_id,
                        record.workspace_id,
                        record.source,
                        record.created_at,
                        record.updated_at,
                        record.last_accessed_at,
                        record.importance,
                        record.confidence,
                        json.dumps(record.tags),
                        record.content_hash,
                        json.dumps(record.embedding) if record.embedding is not None else None,
                        int(record.invalidated),
                        record.invalidated_at,
                        record.version,
                        record.superseded_by,
                    ),
                )
                self._conn.commit()
            except sqlite3.Error as exc:
                raise StorageError(f"failed to persist memory {record.id}: {exc}") from exc
            return record

    def get(self, memory_id: str) -> MemoryRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
            return _row_to_record(row) if row else None

    def update(self, memory_id: str, fields: dict[str, Any]) -> MemoryRecord | None:
        if not fields:
            return self.get(memory_id)
        # Whitelist updatable columns to avoid SQL injection / schema drift.
        allowed = {
            "content", "memory_type", "scope", "project_id", "workspace_id",
            "source", "updated_at", "last_accessed_at", "importance", "confidence",
            "tags", "content_hash", "embedding", "invalidated", "invalidated_at",
            "version", "superseded_by",
        }
        sets: list[str] = []
        vals: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "memory_type":
                value = value.value if isinstance(value, MemoryScope) else value
            elif key == "tags":
                value = json.dumps(value)
            elif key == "embedding":
                value = json.dumps(value) if value is not None else None
            elif key == "invalidated":
                value = int(bool(value))
            sets.append(f"{key} = ?")
            vals.append(value)
        if not sets:
            return self.get(memory_id)
        vals.append(memory_id)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE memories SET {', '.join(sets)} WHERE id = ?", vals
            )
            self._conn.commit()
            if cur.rowcount == 0:
                return None
            return self.get(memory_id)

    def delete(self, memory_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def list(self, filter: MemoryFilter | None = None, *, limit: int | None = None) -> list[MemoryRecord]:
        where, params = _build_where(filter)
        sql = "SELECT * FROM memories"
        if where:
            sql += f" WHERE {where}"
        sql += " ORDER BY created_at DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
            return [_row_to_record(r) for r in rows]

    def search_lexical(
        self, query: str, filter: MemoryFilter | None = None, *, limit: int = 50
    ) -> list[tuple[MemoryRecord, float]]:
        import re

        tokens = re.findall(r"[a-z0-9]+", query.lower())
        if not tokens:
            return []
        where, params = _build_where(filter)
        sql = "SELECT * FROM memories"
        if where:
            sql += f" WHERE {where}"
        # Bound the scan: fetch candidates filtered by scope/type, then score in Python.
        sql += f" LIMIT {max(limit, 50) * 4}"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        results: list[tuple[MemoryRecord, float]] = []
        for row in rows:
            rec = _row_to_record(row)
            content_lower = rec.content.lower()
            score = sum(1.0 for tok in tokens if tok in content_lower)
            if score > 0:
                # Normalize by token count to keep a long doc from dominating.
                results.append((rec, score / len(tokens)))
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:limit]

    def search_vector(
        self, query_embedding: list[float], filter: MemoryFilter | None = None, *, limit: int = 50
    ) -> list[tuple[MemoryRecord, float]]:
        where, params = _build_where(filter)
        sql = "SELECT * FROM memories WHERE embedding IS NOT NULL"
        if where:
            sql += f" AND {where}"
        sql += f" LIMIT {max(limit, 50) * 4}"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        results: list[tuple[MemoryRecord, float]] = []
        for row in rows:
            rec = _row_to_record(row)
            if rec.embedding is None:
                continue
            try:
                sim = cosine_similarity(query_embedding, rec.embedding)
            except ValueError:
                continue
            if sim > 0.0:
                results.append((rec, sim))
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:limit]

    def invalidate(self, memory_id: str) -> MemoryRecord | None:
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat()
        rec = self.update(
            memory_id, {"invalidated": True, "invalidated_at": now}
        )
        if rec is None:
            return None
        return self._bump_version(memory_id)

    def _bump_version(self, memory_id: str) -> MemoryRecord | None:
        with self._lock:
            self._conn.execute(
                "UPDATE memories SET version = version + 1 WHERE id = ?", (memory_id,)
            )
            self._conn.commit()
            return self.get(memory_id)

    def find_duplicates(
        self, content_hash: str, *, project_id: str | None = None
    ) -> list[MemoryRecord]:
        if project_id is not None:
            sql = (
                "SELECT * FROM memories WHERE content_hash = ? AND project_id IS ? "
                "AND invalidated = 0 ORDER BY created_at DESC"
            )
            params: tuple[Any, ...] = (content_hash, project_id)
        else:
            sql = (
                "SELECT * FROM memories WHERE content_hash = ? AND project_id IS NULL "
                "AND invalidated = 0 ORDER BY created_at DESC"
            )
            params = (content_hash,)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
            return [_row_to_record(r) for r in rows]

    def touch(self, memory_id: str) -> None:
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat()
        with self._lock:
            self._conn.execute(
                "UPDATE memories SET last_accessed_at = ? WHERE id = ?", (now, memory_id)
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> SQLiteStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _build_where(filter: MemoryFilter | None) -> tuple[str, list[Any]]:
    """Build a SQL WHERE clause + params enforcing project isolation + filters.

    Project isolation: when a filter specifies project_id, results are locked to
    that project (NULL project memories are global and included only when no
    specific project is requested, i.e. project_id is None in the filter).

    Invalidated memories are excluded unless explicitly requested, even when no
    other filter is given (so a bare list() never returns invalidated rows).
    """
    clauses: list[str] = []
    params: list[Any] = []
    if filter is None:
        filter = MemoryFilter()
    if filter.memory_types:
        placeholders = ",".join("?" for _ in filter.memory_types)
        clauses.append(f"memory_type IN ({placeholders})")
        params.extend(t.value for t in filter.memory_types)
    if filter.project_id is not None:
        clauses.append("project_id = ?")
        params.append(filter.project_id)
    if filter.workspace_id is not None:
        clauses.append("workspace_id = ?")
        params.append(filter.workspace_id)
    if filter.source is not None:
        clauses.append("source = ?")
        params.append(filter.source)
    if not filter.include_invalidated:
        clauses.append("invalidated = 0")
    if filter.created_after is not None:
        clauses.append("created_at >= ?")
        params.append(filter.created_after.isoformat())
    if filter.created_before is not None:
        clauses.append("created_at <= ?")
        params.append(filter.created_before.isoformat())
    return " AND ".join(clauses), params


__all__ = [
    "MemoryStore",
    "SQLiteStore",
    "compute_content_hash",
]
