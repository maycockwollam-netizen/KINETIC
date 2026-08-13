"""Context engine tests: assembly, budget, ranking, omission tracking, degradation."""

from __future__ import annotations

from pathlib import Path

import pytest

from context import ContextBudget, ContextEngine, ContextPackage
from events import EventBus, EventType
from memory import (
    DeterministicEmbeddingProvider,
    MemoryManager,
    Ranker,
    Retriever,
    SQLiteStore,
)
from memory.models import MemoryScope
from security import AuditLog


@pytest.fixture
def engine(tmp_path: Path) -> ContextEngine:
    store = SQLiteStore(tmp_path / "mem.db")
    emb = DeterministicEmbeddingProvider(dimension=32)
    mgr = MemoryManager(
        store=store, embeddings=emb,
        retriever=Retriever(store, emb, Ranker()),
        events=EventBus(), audit=AuditLog(tmp_path / "audit.log"),
        session_id="ctx-test",
    )
    return ContextEngine(memory=mgr, budget=ContextBudget(max_memory_items=3, max_characters=2000), events=EventBus(), session_id="ctx-test")


class TestContextAssembly:
    async def test_assembles_task_and_memories(self, engine: ContextEngine) -> None:
        await engine._memory.create(content="install deps with uv package manager", memory_type=MemoryScope.PROJECT, project_id="p1")
        pkg = await engine.build(task="install deps", project_id="p1")
        names = [s.name for s in pkg.sections]
        assert "Current Task" in names
        assert "Relevant Memories" in names
        assert pkg.memory_count >= 1
        assert pkg.total_characters > 0

    async def test_no_raw_conversation_dump(self, engine: ContextEngine) -> None:
        # A large task_history is bounded.
        history = [f"step {i} did something long" for i in range(100)]
        pkg = await engine.build(task="t", task_history=history, project_id="p1")
        hist_section = next((s for s in pkg.sections if s.name == "Task History"), None)
        assert hist_section is not None
        # Bounded by max_task_history_items (6).
        assert hist_section.content.count("\n") <= 5

    async def test_budget_caps_memories(self, engine: ContextEngine) -> None:
        for i in range(10):
            await engine._memory.create(content=f"fact number {i} about uv deps", memory_type=MemoryScope.PROJECT, project_id="p1")
        pkg = await engine.build(task="uv deps", project_id="p1")
        assert pkg.memory_count <= 3
        assert any(o.section == "Memories" for o in pkg.omissions)

    async def test_global_char_budget_trims_sections(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "m.db")
        emb = DeterministicEmbeddingProvider(dimension=16)
        mgr = MemoryManager(store=store, embeddings=emb, retriever=Retriever(store, emb, Ranker()), events=EventBus(), audit=AuditLog(tmp_path / "a.log"), session_id="s")
        # Tiny char budget forces trimming.
        eng = ContextEngine(memory=mgr, budget=ContextBudget(max_memory_items=1, max_characters=50), events=EventBus(), session_id="s")
        await mgr.create(content="a relevant memory about uv dependencies", memory_type=MemoryScope.PROJECT, project_id="p1")
        pkg = await eng.build(task="a fairly long task description here", project_id="p1")
        assert pkg.total_characters <= 50

    async def test_omission_tracking(self, engine: ContextEngine) -> None:
        for i in range(5):
            await engine._memory.create(content=f"fact number {i} about uv deps", memory_type=MemoryScope.PROJECT, project_id="p1")
        pkg = await engine.build(task="uv deps", project_id="p1")
        assert any(o.reason == "budget cap" for o in pkg.omissions)

    async def test_render_includes_sections(self, engine: ContextEngine) -> None:
        pkg = await engine.build(task="do something", project_metadata={"lang": "python"}, project_id="p1")
        rendered = pkg.render()
        assert "Current Task" in rendered
        assert "python" in rendered

    async def test_memory_failure_degrades_gracefully(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "m.db")
        emb = DeterministicEmbeddingProvider(dimension=16)
        mgr = MemoryManager(store=store, embeddings=emb, retriever=Retriever(store, emb, Ranker()), events=EventBus(), audit=AuditLog(tmp_path / "a.log"), session_id="s")
        eng = ContextEngine(memory=mgr, budget=ContextBudget(), events=EventBus(), session_id="s")
        # Close the store to force a retrieval failure.
        store.close()
        pkg = await eng.build(task="uv", project_id="p1")
        assert pkg.degraded is True
        assert pkg.degradation_note is not None
        # Never fabricates memories on failure.
        assert pkg.memory_count == 0

    async def test_context_built_event_emitted(self, engine: ContextEngine) -> None:
        await engine.build(task="t", project_id="p1")
        types = [ev.type for ev in engine._events.history]
        assert EventType.CONTEXT_BUILT in types

    async def test_project_isolation_in_context(self, engine: ContextEngine) -> None:
        await engine._memory.create(content="project A secret fact uv", memory_type=MemoryScope.PROJECT, project_id="A")
        pkg = await engine.build(task="uv", project_id="B")
        # No leak from project A.
        assert all("A secret" not in s.content for s in pkg.sections)

    async def test_empty_context_valid(self, engine: ContextEngine) -> None:
        pkg = await engine.build(task="t", project_id="p1")
        assert isinstance(pkg, ContextPackage)
        assert pkg.memory_count == 0
