"""Memory lifecycle manager: the orchestration layer.

MemoryManager composes the store, retriever, ranker, embeddings, secret
detector, event bus, and audit log. It is the only component that mutates
memory and emits memory events. Security is enforced HERE, not in prompts:

  * secret filtering — credential-like content is rejected before persistence
  * scope isolation — project-scoped queries cannot cross project boundaries
  * deduplication — repeated identical facts return the existing record
  * invalidation-preferred — important memories are invalidated, not deleted

Every operation is failure-safe: a store error is recorded + emitted and raised
as :class:`MemoryError`, never corrupting task execution or fabricating results.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from kinetic.errors import MemoryError, MemorySecurityError
from kinetic.events import EventBus, EventType
from kinetic.memory.embeddings import EmbeddingProvider
from kinetic.memory.metadata import MemoryFilter, SecretDetector
from kinetic.memory.models import MemoryRecord, MemoryScope, RankedMemory, compute_content_hash
from kinetic.memory.retrieval import Retriever
from kinetic.memory.store import MemoryStore
from kinetic.security import AuditLog


class MemoryManager:
    """Orchestrates memory CRUD, retrieval, and consolidation."""

    def __init__(
        self,
        *,
        store: MemoryStore,
        embeddings: EmbeddingProvider,
        retriever: Retriever | None = None,
        secret_detector: SecretDetector | None = None,
        events: EventBus | None = None,
        audit: AuditLog | None = None,
        session_id: str = "memory",
    ) -> None:
        self._store = store
        self._embeddings = embeddings
        self._retriever = retriever or Retriever(store, embeddings)
        self._secrets = secret_detector or SecretDetector()
        self._events = events
        self._audit = audit
        self._session_id = session_id

    # --- creation ----------------------------------------------------------

    async def create(
        self,
        *,
        content: str,
        memory_type: MemoryScope,
        scope: str = "",
        project_id: str | None = None,
        workspace_id: str | None = None,
        source: str = "agent",
        importance: float = 0.5,
        confidence: float = 0.5,
        tags: list[str] | None = None,
    ) -> MemoryRecord:
        """Create a validated memory; reject secrets; dedup on content hash."""
        content = (content or "").strip()
        if not content:
            raise MemoryError("memory content must not be empty")

        # Secret gate: prefer not persisting when uncertain.
        matches = self._secrets.detect(content)
        if matches:
            self._audit_record(
                action="memory_create_denied",
                allowed=False,
                reason=f"secret-like content detected: {matches[0].kind}",
                detail={"kinds": [m.kind for m in matches]},
            )
            self._emit(
                EventType.PERMISSION_DENIED,
                reason="secret-like content rejected",
            )
            raise MemorySecurityError(
                "memory content rejected: secret-like value detected",
                reason=matches[0].kind,
            )

        content_hash = compute_content_hash(content, scope=memory_type, project_id=project_id)
        # Dedup: if an identical, non-invalidated record exists, return it.
        try:
            dups = self._store.find_duplicates(content_hash, project_id=project_id)
        except Exception as exc:  # noqa: BLE001
            self._fail("memory_create", f"dedup check failed: {exc}")
            raise MemoryError(f"dedup check failed: {exc}") from exc
        if dups:
            existing = dups[0]
            self._store.touch(existing.id)
            return existing

        embedding = self._safe_embed(content)
        record = MemoryRecord(
            id=uuid4().hex,
            content=content,
            memory_type=memory_type,
            scope=scope,
            project_id=project_id,
            workspace_id=workspace_id,
            source=source,
            importance=importance,
            confidence=confidence,
            tags=list(tags or []),
            content_hash=content_hash,
            embedding=embedding,
        )
        try:
            self._store.put(record)
        except Exception as exc:  # noqa: BLE001
            self._fail("memory_create", f"persist failed: {exc}")
            raise MemoryError(f"persist failed: {exc}") from exc
        self._emit(
            EventType.MEMORY_CREATED,
            memory_id=record.id,
            memory_type=memory_type.value,
            project_id=project_id,
            content_hash=content_hash,
        )
        self._audit_record(
            action="memory_create",
            allowed=True,
            detail={"memory_id": record.id, "memory_type": memory_type.value, "project_id": project_id},
        )
        return record

    # --- update / invalidate / delete -------------------------------------

    async def update(
        self,
        memory_id: str,
        *,
        content: str | None = None,
        importance: float | None = None,
        confidence: float | None = None,
        tags: list[str] | None = None,
    ) -> MemoryRecord:
        """Update a memory. Content changes are secret-checked + re-embedded."""
        existing = self._get_or_fail(memory_id)
        fields: dict[str, Any] = {"updated_at": datetime.now(UTC).isoformat()}
        if content is not None:
            content = content.strip()
            if not content:
                raise MemoryError("memory content must not be empty")
            matches = self._secrets.detect(content)
            if matches:
                self._audit_record(
                    action="memory_update_denied",
                    allowed=False,
                    reason=f"secret-like content detected: {matches[0].kind}",
                    detail={"memory_id": memory_id},
                )
                raise MemorySecurityError(
                    "memory content rejected: secret-like value detected",
                    reason=matches[0].kind,
                )
            fields["content"] = content
            fields["content_hash"] = compute_content_hash(
                content, scope=existing.memory_type, project_id=existing.project_id
            )
            fields["embedding"] = self._safe_embed(content)
        if importance is not None:
            fields["importance"] = importance
        if confidence is not None:
            fields["confidence"] = confidence
        if tags is not None:
            fields["tags"] = list(tags)
        try:
            updated = self._store.update(memory_id, fields)
        except Exception as exc:  # noqa: BLE001
            self._fail("memory_update", f"update failed: {exc}")
            raise MemoryError(f"update failed: {exc}") from exc
        if updated is None:
            raise MemoryError(f"memory not found after update: {memory_id}")
        self._emit(EventType.MEMORY_UPDATED, memory_id=memory_id, version=updated.version)
        self._audit_record(
            action="memory_update",
            allowed=True,
            detail={"memory_id": memory_id, "version": updated.version},
        )
        return updated

    async def invalidate(self, memory_id: str) -> MemoryRecord:
        """Mark a memory invalid (non-destructive). Preferred over delete."""
        self._get_or_fail(memory_id)
        try:
            updated = self._store.invalidate(memory_id)
        except Exception as exc:  # noqa: BLE001
            self._fail("memory_invalidate", f"invalidate failed: {exc}")
            raise MemoryError(f"invalidate failed: {exc}") from exc
        if updated is None:
            raise MemoryError(f"memory not found after invalidate: {memory_id}")
        self._emit(EventType.MEMORY_INVALIDATED, memory_id=memory_id, version=updated.version)
        self._audit_record(
            action="memory_invalidate",
            allowed=True,
            detail={"memory_id": memory_id, "version": updated.version},
        )
        return updated

    async def delete(self, memory_id: str) -> bool:
        """Hard delete a memory (restricted). Prefer :meth:`invalidate`."""
        self._get_or_fail(memory_id)
        try:
            deleted = self._store.delete(memory_id)
        except Exception as exc:  # noqa: BLE001
            self._fail("memory_delete", f"delete failed: {exc}")
            raise MemoryError(f"delete failed: {exc}") from exc
        self._emit(EventType.MEMORY_DELETED, memory_id=memory_id, deleted=deleted)
        self._audit_record(
            action="memory_delete",
            allowed=deleted,
            detail={"memory_id": memory_id},
        )
        return deleted

    # --- retrieval ---------------------------------------------------------

    async def retrieve(self, memory_id: str) -> MemoryRecord | None:
        """Fetch a single memory by id (touches last_accessed_at)."""
        record = self._store.get(memory_id)
        if record is not None:
            self._store.touch(memory_id)
            self._emit(EventType.MEMORY_RETRIEVED, memory_id=memory_id, via="get")
        return record

    async def search(
        self,
        query: str,
        *,
        filter: MemoryFilter | None = None,
        limit: int = 10,
    ) -> list[RankedMemory]:
        """Hybrid search; returns ranked memories with retrieval explanations."""
        try:
            ranked = await self._retriever.retrieve(query, filter=filter, limit=limit)
        except Exception as exc:  # noqa: BLE001
            self._fail("memory_search", f"retrieval failed: {exc}")
            raise MemoryError(f"retrieval failed: {exc}") from exc
        for r in ranked:
            self._store.touch(r.record.id)
        self._emit(
            EventType.MEMORY_RETRIEVED,
            via="search",
            query=query,
            count=len(ranked),
            top_score=ranked[0].final_score if ranked else 0.0,
        )
        return ranked

    # --- consolidation -----------------------------------------------------

    async def consolidate(
        self,
        *,
        source_ids: list[str],
        content: str,
        memory_type: MemoryScope = MemoryScope.PROJECT,
        scope: str = "",
        project_id: str | None = None,
        confidence: float = 0.8,
        importance: float = 0.7,
        tags: list[str] | None = None,
    ) -> MemoryRecord:
        """Consolidate several memories into one explicit project-level fact.

        Explicit and deterministic: the new content is provided (not generated
        by the model), secret-checked, and the source memories are invalidated
        (not deleted) and linked via ``superseded_by``. Model-generated memory
        never silently overwrites trusted facts here.
        """
        if not source_ids:
            raise MemoryError("consolidation requires at least one source memory")
        sources: list[MemoryRecord] = []
        for sid in source_ids:
            rec = self._get_or_fail(sid)
            sources.append(rec)

        # Secret-check the consolidated content.
        matches = self._secrets.detect(content)
        if matches:
            self._audit_record(
                action="memory_consolidate_denied",
                allowed=False,
                reason=f"secret-like content detected: {matches[0].kind}",
                detail={"source_ids": source_ids},
            )
            raise MemorySecurityError(
                "consolidated content rejected: secret-like value detected",
                reason=matches[0].kind,
            )

        created = await self.create(
            content=content,
            memory_type=memory_type,
            scope=scope,
            project_id=project_id,
            source="consolidation",
            importance=importance,
            confidence=confidence,
            tags=list(tags or []),
        )
        # Link + invalidate sources.
        for src in sources:
            await self.invalidate(src.id)
            try:
                self._store.update(src.id, {"superseded_by": created.id})
            except Exception as exc:  # noqa: BLE001 - non-fatal link
                self._fail("memory_consolidate", f"link failed for {src.id}: {exc}")
        self._emit(
            EventType.MEMORY_CONSOLIDATED,
            memory_id=created.id,
            source_ids=source_ids,
        )
        self._audit_record(
            action="memory_consolidate",
            allowed=True,
            detail={"memory_id": created.id, "source_ids": source_ids},
        )
        return created

    # --- internals ---------------------------------------------------------

    def _get_or_fail(self, memory_id: str) -> MemoryRecord:
        record = self._store.get(memory_id)
        if record is None:
            raise MemoryError(f"memory not found: {memory_id}")
        return record

    def _safe_embed(self, content: str) -> list[float] | None:
        try:
            return self._embeddings.embed(content)
        except Exception as exc:  # noqa: BLE001 - embedding failure is non-fatal
            self._fail("memory_embed", f"embedding failed: {exc}")
            return None

    def _emit(self, event_type: EventType, **data: Any) -> None:
        if self._events is not None:
            self._events.emit(event_type, self._session_id, **data)

    def _audit_record(
        self, *, action: str, allowed: bool, reason: str | None = None, detail: dict[str, Any] | None = None
    ) -> None:
        if self._audit is not None:
            self._audit.record(
                session_id=self._session_id,
                action=action,
                allowed=allowed,
                reason=reason,
                detail=detail or {},
            )

    def _fail(self, action: str, reason: str) -> None:
        """Record a non-fatal memory subsystem failure (event + audit).

        Memory failures never corrupt task execution; they are surfaced so the
        caller can degrade gracefully (e.g. empty context) rather than fabricate
        results.
        """
        self._emit(EventType.AGENT_ERROR, action=action, reason=reason)
        self._audit_record(action=action, allowed=False, reason=reason)
