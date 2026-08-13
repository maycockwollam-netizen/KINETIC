"""Memory engine tests: models, embeddings, store, retrieval, lifecycle, security."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from errors import MemoryError, MemorySecurityError
from events import EventBus, EventType
from memory import (
    DeterministicEmbeddingProvider,
    MemoryManager,
    MemoryRecord,
    MemoryScope,
    Ranker,
    RankingWeights,
    Retriever,
    SQLiteStore,
)
from memory.metadata import MemoryFilter, SecretDetector
from memory.models import compute_content_hash
from security import AuditLog

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> SQLiteStore:
    return SQLiteStore(tmp_path / "mem.db")


@pytest.fixture
def embeddings() -> DeterministicEmbeddingProvider:
    return DeterministicEmbeddingProvider(dimension=32)


@pytest.fixture
def manager(store: SQLiteStore, embeddings: DeterministicEmbeddingProvider, tmp_path: Path) -> MemoryManager:
    return MemoryManager(
        store=store,
        embeddings=embeddings,
        retriever=Retriever(store, embeddings, Ranker()),
        events=EventBus(),
        audit=AuditLog(tmp_path / "audit.log"),
        session_id="test",
    )


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class TestMemoryModels:
    def test_scope_persistent_property(self) -> None:
        assert MemoryScope.PROJECT.persistent is True
        assert MemoryScope.AGENT.persistent is True
        assert MemoryScope.EPHEMERAL.persistent is False
        assert MemoryScope.TASK.persistent is False

    def test_content_hash_deterministic(self) -> None:
        h1 = compute_content_hash("uv", scope=MemoryScope.PROJECT, project_id="p1")
        h2 = compute_content_hash("uv", scope=MemoryScope.PROJECT, project_id="p1")
        assert h1 == h2

    def test_content_hash_differs_by_scope_or_project(self) -> None:
        h1 = compute_content_hash("uv", scope=MemoryScope.PROJECT, project_id="p1")
        h2 = compute_content_hash("uv", scope=MemoryScope.TASK, project_id="p1")
        h3 = compute_content_hash("uv", scope=MemoryScope.PROJECT, project_id="p2")
        assert h1 != h2
        assert h1 != h3

    def test_record_validation_importance_confidence_bounds(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            MemoryRecord(
                id="x", content="c", memory_type=MemoryScope.TASK, importance=1.5
            )
        with pytest.raises(ValidationError):
            MemoryRecord(
                id="x", content="c", memory_type=MemoryScope.TASK, confidence=-0.1
            )

    def test_record_storage_dict_roundtrip(self) -> None:
        rec = MemoryRecord(
            id="x", content="hello", memory_type=MemoryScope.PROJECT,
            tags=["a", "b"], embedding=[0.1, 0.2],
        )
        d = rec.to_storage_dict()
        assert d["memory_type"] == "project"
        assert d["embedding"] is not None


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------


class TestEmbeddings:
    def test_deterministic_identical(self, embeddings: DeterministicEmbeddingProvider) -> None:
        a = embeddings.embed("project uses uv")
        b = embeddings.embed("project uses uv")
        assert a == b

    def test_dimension(self, embeddings: DeterministicEmbeddingProvider) -> None:
        assert len(embeddings.embed("anything")) == 32

    def test_batch(self, embeddings: DeterministicEmbeddingProvider) -> None:
        out = embeddings.embed_batch(["a", "b"])
        assert len(out) == 2
        assert all(len(v) == 32 for v in out)

    def test_different_text_different_vector(self, embeddings: DeterministicEmbeddingProvider) -> None:
        a = embeddings.embed("python dependency management")
        b = embeddings.embed("docker container runtime")
        assert a != b

    def test_zero_dimension_rejected(self) -> None:
        with pytest.raises(ValueError):
            DeterministicEmbeddingProvider(dimension=0)

    def test_empty_text_zero_vector(self, embeddings: DeterministicEmbeddingProvider) -> None:
        v = embeddings.embed("")
        assert all(x == 0.0 for x in v)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class TestStore:
    async def test_put_and_get(self, store: SQLiteStore) -> None:
        rec = MemoryRecord(id="m1", content="hello", memory_type=MemoryScope.PROJECT)
        store.put(rec)
        got = store.get("m1")
        assert got is not None
        assert got.content == "hello"
        assert got.memory_type == MemoryScope.PROJECT

    async def test_get_missing(self, store: SQLiteStore) -> None:
        assert store.get("nope") is None

    async def test_update(self, store: SQLiteStore) -> None:
        store.put(MemoryRecord(id="m1", content="old", memory_type=MemoryScope.TASK))
        updated = store.update("m1", {"content": "new", "importance": 0.9})
        assert updated is not None
        assert updated.content == "new"
        assert updated.importance == 0.9

    async def test_delete(self, store: SQLiteStore) -> None:
        store.put(MemoryRecord(id="m1", content="x", memory_type=MemoryScope.TASK))
        assert store.delete("m1") is True
        assert store.get("m1") is None
        assert store.delete("m1") is False

    async def test_invalidate(self, store: SQLiteStore) -> None:
        store.put(MemoryRecord(id="m1", content="x", memory_type=MemoryScope.PROJECT))
        inv = store.invalidate("m1")
        assert inv is not None
        assert inv.invalidated is True
        assert inv.invalidated_at is not None
        assert inv.version == 2

    async def test_invalidate_missing(self, store: SQLiteStore) -> None:
        assert store.invalidate("nope") is None

    async def test_dedup(self, store: SQLiteStore) -> None:
        ch = compute_content_hash("uv", scope=MemoryScope.PROJECT, project_id="p1")
        rec = MemoryRecord(
            id="m1", content="uv", memory_type=MemoryScope.PROJECT,
            project_id="p1", content_hash=ch,
        )
        store.put(rec)
        dups = store.find_duplicates(ch, project_id="p1")
        assert len(dups) == 1
        assert dups[0].id == "m1"

    async def test_list_filter_by_project(self, store: SQLiteStore) -> None:
        store.put(MemoryRecord(id="a", content="x", memory_type=MemoryScope.PROJECT, project_id="p1"))
        store.put(MemoryRecord(id="b", content="y", memory_type=MemoryScope.PROJECT, project_id="p2"))
        result = store.list(MemoryFilter.for_project("p1"))
        assert {r.id for r in result} == {"a"}

    async def test_list_excludes_invalidated_by_default(self, store: SQLiteStore) -> None:
        store.put(MemoryRecord(id="a", content="x", memory_type=MemoryScope.PROJECT))
        store.invalidate("a")
        result = store.list()
        assert {r.id for r in result} == set()
        result2 = store.list(MemoryFilter(include_invalidated=True))
        assert {r.id for r in result2} == {"a"}

    async def test_search_lexical(self, store: SQLiteStore) -> None:
        store.put(MemoryRecord(id="a", content="project uses uv for deps", memory_type=MemoryScope.PROJECT))
        store.put(MemoryRecord(id="b", content="docker runtime", memory_type=MemoryScope.PROJECT))
        results = store.search_lexical("uv deps")
        ids = [r.id for r, _ in results]
        assert "a" in ids
        assert "b" not in ids

    async def test_search_vector(self, store: SQLiteStore, embeddings: DeterministicEmbeddingProvider) -> None:
        store.put(MemoryRecord(
            id="a", content="python dependency management with uv",
            memory_type=MemoryScope.PROJECT, embedding=embeddings.embed("python dependency management with uv"),
        ))
        store.put(MemoryRecord(
            id="b", content="docker container runtime",
            memory_type=MemoryScope.PROJECT, embedding=embeddings.embed("docker container runtime"),
        ))
        q = embeddings.embed("python dependency")
        results = store.search_vector(q)
        assert results[0][0].id == "a"

    async def test_touch_updates_access(self, store: SQLiteStore) -> None:
        store.put(MemoryRecord(id="a", content="x", memory_type=MemoryScope.TASK))
        rec = store.get("a")
        assert rec is not None
        before = rec.last_accessed_at
        store.touch("a")
        after = store.get("a")
        assert after is not None
        assert after.last_accessed_at >= before


# ---------------------------------------------------------------------------
# Retrieval + ranking
# ---------------------------------------------------------------------------


class TestRetrieval:
    async def test_hybrid_search_ranks_relevant(
        self, manager: MemoryManager
    ) -> None:
        await manager.create(content="project uses uv for dependency management", memory_type=MemoryScope.PROJECT, project_id="p1", confidence=0.9)
        await manager.create(content="docker container runtime isolation", memory_type=MemoryScope.PROJECT, project_id="p1", confidence=0.9)
        ranked = await manager.search("dependency management uv", filter=MemoryFilter.for_project("p1"))
        assert len(ranked) >= 1
        assert ranked[0].record.content.startswith("project uses uv")
        assert ranked[0].components["semantic"] > 0.0 or ranked[0].components["lexical"] > 0.0

    async def test_retrieval_explains_signals(self, manager: MemoryManager) -> None:
        await manager.create(content="uv is the package manager", memory_type=MemoryScope.PROJECT, project_id="p1")
        ranked = await manager.search("uv package", filter=MemoryFilter.for_project("p1"))
        assert ranked[0].components  # non-empty explanation

    async def test_project_isolation_in_search(self, manager: MemoryManager) -> None:
        await manager.create(content="secret shared fact", memory_type=MemoryScope.PROJECT, project_id="p1")
        ranked = await manager.search("secret shared fact", filter=MemoryFilter.for_project("p2"))
        assert all(r.record.project_id != "p1" for r in ranked)
        # Without a project filter, project-scoped memories should not leak by default
        # since search uses project filter; explicit None returns global+all.
        ranked2 = await manager.search("secret shared fact")
        assert any(r.record.project_id == "p1" for r in ranked2)


class TestRanking:
    def test_weights_normalized(self) -> None:
        w = RankingWeights(semantic=2.0, lexical=2.0, recency=1.0, importance=1.0)
        n = w.normalized()
        assert abs(sum(n.values()) - 1.0) < 1e-9
        assert abs(n["semantic"] - (2.0 / 6.0)) < 1e-9
        assert abs(n["lexical"] - (2.0 / 6.0)) < 1e-9

    def test_weights_reject_all_zero(self) -> None:
        with pytest.raises(ValidationError):
            RankingWeights(semantic=0, lexical=0, recency=0, importance=0)

    def test_confidence_modulates(self) -> None:
        from datetime import UTC, datetime

        from memory.models import RankedMemory

        ranker = Ranker()
        rec_high = MemoryRecord(id="h", content="x", memory_type=MemoryScope.PROJECT, confidence=1.0, importance=0.5)
        rec_low = MemoryRecord(id="l", content="x", memory_type=MemoryScope.PROJECT, confidence=0.1, importance=0.5)
        ranked: list[RankedMemory] = ranker.rank(
            [(rec_high, {"semantic": 0.5}), (rec_low, {"semantic": 0.5})],
            now=datetime.now(UTC),
        )
        assert ranked[0].record.id == "h"


# ---------------------------------------------------------------------------
# Lifecycle (manager)
# ---------------------------------------------------------------------------


class TestLifecycle:
    async def test_create_and_retrieve(self, manager: MemoryManager) -> None:
        rec = await manager.create(content="a fact", memory_type=MemoryScope.TASK)
        got = await manager.retrieve(rec.id)
        assert got is not None
        assert got.content == "a fact"

    async def test_create_dedups(self, manager: MemoryManager) -> None:
        r1 = await manager.create(content="same fact", memory_type=MemoryScope.PROJECT, project_id="p1")
        r2 = await manager.create(content="same fact", memory_type=MemoryScope.PROJECT, project_id="p1")
        assert r1.id == r2.id

    async def test_update(self, manager: MemoryManager) -> None:
        rec = await manager.create(content="old", memory_type=MemoryScope.TASK)
        updated = await manager.update(rec.id, content="new", importance=0.9)
        assert updated.content == "new"
        assert updated.importance == 0.9

    async def test_invalidate(self, manager: MemoryManager) -> None:
        rec = await manager.create(content="x", memory_type=MemoryScope.PROJECT)
        inv = await manager.invalidate(rec.id)
        assert inv.invalidated is True
        assert inv.version >= 2

    async def test_delete(self, manager: MemoryManager) -> None:
        rec = await manager.create(content="x", memory_type=MemoryScope.TASK)
        assert await manager.delete(rec.id) is True
        assert await manager.retrieve(rec.id) is None

    async def test_consolidate(self, manager: MemoryManager) -> None:
        s1 = await manager.create(content="project uses uv", memory_type=MemoryScope.TASK, project_id="p1")
        s2 = await manager.create(content="deps managed via uv", memory_type=MemoryScope.TASK, project_id="p1")
        consolidated = await manager.consolidate(
            source_ids=[s1.id, s2.id],
            content="Project uses uv for dependency management.",
            project_id="p1",
        )
        assert consolidated.memory_type == MemoryScope.PROJECT
        assert consolidated.source == "consolidation"
        # Sources invalidated + linked.
        inv1 = await manager.retrieve(s1.id)
        assert inv1 is not None and inv1.invalidated is True
        assert inv1.superseded_by == consolidated.id

    async def test_consolidate_rejects_empty_sources(self, manager: MemoryManager) -> None:
        with pytest.raises(MemoryError):
            await manager.consolidate(source_ids=[], content="x")

    async def test_get_missing_raises(self, manager: MemoryManager) -> None:
        with pytest.raises(MemoryError):
            await manager.update("nope", content="x")

    async def test_empty_content_rejected(self, manager: MemoryManager) -> None:
        with pytest.raises(MemoryError):
            await manager.create(content="   ", memory_type=MemoryScope.TASK)

    async def test_create_emits_event(self, manager: MemoryManager) -> None:
        # manager fixture has an EventBus; capture via a subscriber.
        sub = await manager._events.subscribe()
        await manager.create(content="emit me", memory_type=MemoryScope.TASK)
        types = []
        for ev in list(manager._events.history):
            types.append(ev.type)
        assert EventType.MEMORY_CREATED in types
        sub.close()


# ---------------------------------------------------------------------------
# Security: secret filtering + cross-project prevention
# ---------------------------------------------------------------------------


class TestMemorySecurity:
    @pytest.mark.parametrize("content", [
        "my api_key is sk-1234567890abcdef1234567890",
        "password=hunter2secretvalue123",
        "-----BEGIN RSA PRIVATE KEY-----",
        "ghp_abcdefghijklmnopqrstuvwxyz123456",
        "xoxb-1234567890-abcdef",
    ])
    async def test_secret_rejected(self, manager: MemoryManager, content: str) -> None:
        with pytest.raises(MemorySecurityError):
            await manager.create(content=content, memory_type=MemoryScope.TASK)

    async def test_secret_in_update_rejected(self, manager: MemoryManager) -> None:
        rec = await manager.create(content="clean fact", memory_type=MemoryScope.TASK)
        with pytest.raises(MemorySecurityError):
            await manager.update(rec.id, content="api_key=sk_live_1234567890abcdef")

    async def test_secret_in_consolidate_rejected(self, manager: MemoryManager) -> None:
        s = await manager.create(content="clean", memory_type=MemoryScope.TASK, project_id="p1")
        with pytest.raises(MemorySecurityError):
            await manager.consolidate(source_ids=[s.id], content="token=ghp_abcdef1234567890")

    async def test_cross_project_retrieval_prevented(self, manager: MemoryManager) -> None:
        await manager.create(content="project a fact", memory_type=MemoryScope.PROJECT, project_id="A")
        ranked = await manager.search("project a fact", filter=MemoryFilter.for_project("B"))
        assert all(r.record.project_id != "A" for r in ranked)
        assert ranked == []

    def test_secret_detector_masks_snippet(self) -> None:
        det = SecretDetector()
        matches = det.detect("api_key=sk_1234567890abcdef")
        assert matches
        # Snippet should be masked, not the raw secret.
        assert "sk_1234567890abcdef" not in matches[0].snippet

    def test_secret_detector_clean_text(self) -> None:
        det = SecretDetector()
        assert not det.contains_secret("the project uses uv for dependencies")

    async def test_secret_denial_audited(self, manager: MemoryManager) -> None:
        with pytest.raises(MemorySecurityError):
            await manager.create(content="api_key=sk_1234567890abcdef1234", memory_type=MemoryScope.TASK)
        entries = manager._audit.read()
        denied = [e for e in entries if e["action"] == "memory_create_denied"]
        assert denied
        # No raw secret in audit detail.
        import json
        blob = json.dumps(denied)
        assert "sk_1234567890" not in blob


# ---------------------------------------------------------------------------
# Memory consistency: conflicting / stale memories
# ---------------------------------------------------------------------------


class TestMemoryConsistency:
    async def test_confidence_resolves_conflict(self, manager: MemoryManager) -> None:
        """A fresh high-confidence fact outranks a stale low-confidence one."""
        await manager.create(
            content="project uses npm for deps",
            memory_type=MemoryScope.PROJECT, project_id="p1",
            confidence=0.3, importance=0.5,
        )
        await manager.create(
            content="project migrated to pnpm for deps",
            memory_type=MemoryScope.PROJECT, project_id="p1",
            confidence=0.95, importance=0.8,
        )
        ranked = await manager.search("deps", filter=MemoryFilter.for_project("p1"))
        assert ranked[0].record.content == "project migrated to pnpm for deps"

    async def test_invalidation_does_not_delete(self, manager: MemoryManager) -> None:
        rec = await manager.create(content="old fact deps", memory_type=MemoryScope.PROJECT, project_id="p1")
        await manager.invalidate(rec.id)
        # Record still exists (non-destructive).
        assert await manager.retrieve(rec.id) is not None
        # But excluded from default search.
        ranked = await manager.search("old fact deps", filter=MemoryFilter.for_project("p1"))
        assert all(r.record.id != rec.id for r in ranked)

    async def test_audit_records_create_and_update(self, manager: MemoryManager) -> None:
        rec = await manager.create(content="audited fact", memory_type=MemoryScope.TASK)
        await manager.update(rec.id, importance=0.9)
        entries = manager._audit.read()
        actions = {e["action"] for e in entries}
        assert "memory_create" in actions
        assert "memory_update" in actions
