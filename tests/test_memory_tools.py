"""Memory tools + permission tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kinetic.errors import PermissionDeniedError
from kinetic.events import EventBus, EventType
from kinetic.memory import (
    DeterministicEmbeddingProvider,
    MemoryManager,
    MemoryScope,
    Ranker,
    Retriever,
    SQLiteStore,
)
from kinetic.security import AuditLog, PermissionPolicy
from kinetic.tools.memory import memory_tools


@pytest.fixture
def policy() -> PermissionPolicy:
    return PermissionPolicy(
        allow_memory_read=True,
        allow_memory_write=True,
        allow_memory_delete=False,
    )


@pytest.fixture
def manager(tmp_path: Path) -> MemoryManager:
    store = SQLiteStore(tmp_path / "m.db")
    emb = DeterministicEmbeddingProvider(dimension=32)
    return MemoryManager(
        store=store, embeddings=emb, retriever=Retriever(store, emb, Ranker()),
        events=EventBus(), audit=AuditLog(tmp_path / "a.log"), session_id="t",
    )


@pytest.fixture
def tools(manager: MemoryManager, policy: PermissionPolicy, tmp_path: Path) -> dict:
    registry = {t.name: t for t in memory_tools(manager=manager, policy=policy, audit=AuditLog(tmp_path / "a2.log"), events=EventBus(), session_id="t")}
    return registry


class TestMemoryTools:
    async def test_memory_create_and_get(self, tools: dict, manager: MemoryManager) -> None:
        create_t = tools["memory_create"]
        result = await create_t.func({"content": "project uses uv", "memory_type": "project"})
        data = json.loads(result["content"][0]["text"])
        mid = data["id"]
        get_t = tools["memory_get"]
        out = await get_t.func({"id": mid})
        payload = json.loads(out["content"][0]["text"])
        assert payload["content"] == "project uses uv"

    async def test_memory_search(self, tools: dict, manager: MemoryManager) -> None:
        await manager.create(content="install deps with uv", memory_type=MemoryScope.PROJECT, project_id="p1")
        search_t = tools["memory_search"]
        result = await search_t.func({"query": "uv deps", "project_id": "p1"})
        data = json.loads(result["content"][0]["text"])
        assert len(data) >= 1
        assert "score" in data[0]

    async def test_memory_create_rejects_secret(self, tools: dict) -> None:
        create_t = tools["memory_create"]
        result = await create_t.func({"content": "api_key=sk_1234567890abcdef1234", "memory_type": "task"})
        assert result.get("isError") is True

    async def test_memory_update(self, tools: dict) -> None:
        create_t = tools["memory_create"]
        created = json.loads((await create_t.func({"content": "old fact", "memory_type": "task"}))["content"][0]["text"])
        update_t = tools["memory_update"]
        out = await update_t.func({"id": created["id"], "content": "new fact"})
        data = json.loads(out["content"][0]["text"])
        assert data["updated"] is True


class TestMemoryPermissions:
    async def test_write_denied_by_default(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "m.db")
        emb = DeterministicEmbeddingProvider(dimension=16)
        mgr = MemoryManager(store=store, embeddings=emb, retriever=Retriever(store, emb, Ranker()), events=EventBus(), audit=AuditLog(tmp_path / "a.log"), session_id="t")
        strict_policy = PermissionPolicy(allow_memory_read=True, allow_memory_write=False)
        reg = {t.name: t for t in memory_tools(manager=mgr, policy=strict_policy, audit=AuditLog(tmp_path / "a2.log"), events=EventBus(), session_id="t")}
        with pytest.raises(PermissionDeniedError):
            await reg["memory_create"].func({"content": "x", "memory_type": "task"})

    async def test_read_denied_when_disabled(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "m.db")
        emb = DeterministicEmbeddingProvider(dimension=16)
        mgr = MemoryManager(store=store, embeddings=emb, retriever=Retriever(store, emb, Ranker()), events=EventBus(), audit=AuditLog(tmp_path / "a.log"), session_id="t")
        strict_policy = PermissionPolicy(allow_memory_read=False, allow_memory_write=False)
        reg = {t.name: t for t in memory_tools(manager=mgr, policy=strict_policy, audit=AuditLog(tmp_path / "a2.log"), events=EventBus(), session_id="t")}
        with pytest.raises(PermissionDeniedError):
            await reg["memory_search"].func({"query": "x"})

    async def test_permission_denial_emits_event(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "m.db")
        emb = DeterministicEmbeddingProvider(dimension=16)
        bus = EventBus()
        mgr = MemoryManager(store=store, embeddings=emb, retriever=Retriever(store, emb, Ranker()), events=bus, audit=AuditLog(tmp_path / "a.log"), session_id="t")
        strict_policy = PermissionPolicy(allow_memory_write=False)
        reg = {t.name: t for t in memory_tools(manager=mgr, policy=strict_policy, audit=AuditLog(tmp_path / "a2.log"), events=bus, session_id="t")}
        with pytest.raises(PermissionDeniedError):
            await reg["memory_create"].func({"content": "x"})
        types = [ev.type for ev in bus.history]
        assert EventType.PERMISSION_DENIED in types

    async def test_memory_delete_restricted(self, tools: dict, manager: MemoryManager) -> None:
        rec = await manager.create(content="to delete", memory_type=MemoryScope.TASK)
        del_t = tools["memory_delete"]
        # delete disabled in fixture policy -> denied.
        with pytest.raises(PermissionDeniedError):
            await del_t.func({"id": rec.id})
