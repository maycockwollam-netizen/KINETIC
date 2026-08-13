"""Phase 7 — memory/context hardening tests.

Verifies bounded retrieval, deterministic ranking/embeddings, secret masking,
corrupt SQLite handling, and duplicate memory behavior.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memory.embeddings import DeterministicEmbeddingProvider, cosine_similarity
from memory.metadata import DEFAULT_SECRET_DETECTOR, MemoryFilter
from memory.models import MemoryRecord, MemoryScope
from memory.store import SQLiteStore


@pytest.fixture
def store(tmp_path: Path) -> SQLiteStore:
    return SQLiteStore(tmp_path / "memory.db")


def _make_record(id: str, content: str, **kw) -> MemoryRecord:
    kw.setdefault("memory_type", MemoryScope.PROJECT)
    return MemoryRecord(id=id, content=content, **kw)


class TestDeterministicEmbeddings:
    def test_identical_text_same_embedding(self) -> None:
        emb = DeterministicEmbeddingProvider(dimension=64)
        v1 = emb.embed("hello world")
        v2 = emb.embed("hello world")
        assert v1 == v2

    def test_similar_text_high_cosine(self) -> None:
        emb = DeterministicEmbeddingProvider(dimension=128)
        v1 = emb.embed("the quick brown fox")
        v2 = emb.embed("the quick brown fox jumps")
        sim = cosine_similarity(v1, v2)
        assert sim > 0.5

    def test_dimension_correct(self) -> None:
        emb = DeterministicEmbeddingProvider(dimension=32)
        v = emb.embed("test")
        assert len(v) == 32


class TestSecretDetection:
    @pytest.mark.parametrize("text", [
        "api_key=sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "password=supersecret12345",
        "token=ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "AKIAABCDEFGHIJKLMNOP",
    ])
    def test_secret_detected(self, text: str) -> None:
        assert DEFAULT_SECRET_DETECTOR.contains_secret(text)

    def test_plain_text_not_flagged(self) -> None:
        assert not DEFAULT_SECRET_DETECTOR.contains_secret("just a normal memory note")


class TestSQLiteStore:
    def test_put_and_get(self, store: SQLiteStore) -> None:
        rec = _make_record("m1", "hello world")
        store.put(rec)
        got = store.get("m1")
        assert got is not None
        assert got.content == "hello world"

    def test_delete(self, store: SQLiteStore) -> None:
        store.put(_make_record("m1", "data"))
        assert store.delete("m1") is True
        assert store.get("m1") is None

    def test_invalidate(self, store: SQLiteStore) -> None:
        store.put(_make_record("m1", "data"))
        result = store.invalidate("m1")
        assert result is not None
        assert result.invalidated is True

    def test_list_excludes_invalidated(self, store: SQLiteStore) -> None:
        store.put(_make_record("m1", "active"))
        store.put(_make_record("m2", "old"))
        store.invalidate("m2")
        records = store.list()
        ids = {r.id for r in records}
        assert "m1" in ids
        assert "m2" not in ids

    def test_project_isolation(self, store: SQLiteStore) -> None:
        store.put(_make_record("m1", "proj A", project_id="projA"))
        store.put(_make_record("m2", "proj B", project_id="projB"))
        filter_a = MemoryFilter.for_project(project_id="projA")
        results = store.list(filter=filter_a)
        assert all(r.project_id == "projA" for r in results)
        assert len(results) == 1

    def test_duplicate_detection(self, store: SQLiteStore) -> None:
        rec = _make_record("m1", "duplicate content")
        store.put(rec)
        dupes = store.find_duplicates(rec.content_hash)
        assert len(dupes) >= 1

    def test_corrupt_db_raises(self, tmp_path: Path) -> None:
        db_path = tmp_path / "corrupt.db"
        db_path.write_text("not a database")
        with pytest.raises((Exception,)):  # noqa: B017
            SQLiteStore(db_path)


class TestBoundedRetrieval:
    def test_search_lexical_bounded(self, store: SQLiteStore) -> None:
        for i in range(100):
            store.put(_make_record(f"m{i}", f"item number {i}"))
        results = store.search_lexical("item", limit=10)
        assert len(results) <= 10

    def test_search_vector_bounded(self, store: SQLiteStore) -> None:
        emb = DeterministicEmbeddingProvider(dimension=32)
        for i in range(50):
            rec = _make_record(f"m{i}", f"vector item {i}")
            rec.embedding = emb.embed(rec.content)
            store.put(rec)
        query_emb = emb.embed("vector item")
        results = store.search_vector(query_emb, limit=5)
        assert len(results) <= 5


class TestMemoryFailureClassification:
    """Memory failures must be classified explicitly, not silently corrupt tasks."""

    def test_memory_error_is_structured(self) -> None:
        from errors import MemoryError, MemorySecurityError

        err = MemoryError("store failed")
        assert isinstance(err, Exception)
        sec = MemorySecurityError("secret rejected", reason="contains_api_key")
        assert sec.reason == "contains_api_key"

    def test_corrupt_db_does_not_silently_pass(self, tmp_path: Path) -> None:
        db_path = tmp_path / "bad.db"
        db_path.write_text("corrupt")
        with pytest.raises((Exception,)):  # noqa: B017
            SQLiteStore(db_path)
