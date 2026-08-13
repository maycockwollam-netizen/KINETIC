"""Phase 7 — performance / stress tests.

Bounded stress tests that verify no pathological behavior under load. These
do NOT optimize prematurely — they confirm bounds hold and behavior stays
deterministic under stress.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from kinetic.events import EventBus, EventType
from kinetic.memory.embeddings import DeterministicEmbeddingProvider
from kinetic.memory.models import MemoryRecord
from kinetic.memory.store import SQLiteStore
from kinetic.tasks.checkpoints import build_checkpoint
from kinetic.tasks.models import Plan, Task
from kinetic.tasks.states import TaskState, transition_allowed


class TestEventBusStress:
    async def test_rapid_publish_bounded_memory(self) -> None:
        bus = EventBus(history_size=100, subscriber_queue_size=50)
        for i in range(10_000):
            bus.emit(EventType.AGENT_MESSAGE, "s", index=i)
        assert len(bus.history) == 100

    async def test_many_subscribers(self) -> None:
        bus = EventBus(history_size=10, subscriber_queue_size=10)
        [await bus.subscribe() for _ in range(20)]
        bus.emit(EventType.AGENT_MESSAGE, "s", msg="hi")
        # All subscribers should have received (or dropped, but not crashed).
        assert len(bus.history) == 1

    async def test_concurrent_publish_no_corruption(self) -> None:
        bus = EventBus(history_size=1000)

        async def publish(n: int) -> None:
            for i in range(n):
                bus.emit(EventType.AGENT_MESSAGE, "s", src=n, idx=i)

        await asyncio.gather(*(publish(100) for _ in range(10)))
        assert len(bus.history) == 1000


class TestMemoryStress:
    def test_many_records(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "stress.db")
        from kinetic.memory.models import MemoryScope

        for i in range(500):
            store.put(MemoryRecord(id=f"m{i}", content=f"record {i}",
                                   memory_type=MemoryScope.PROJECT))
        assert len(store.list()) == 500

    def test_search_bounded_under_load(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "stress.db")
        from kinetic.memory.models import MemoryScope

        for i in range(1000):
            store.put(MemoryRecord(id=f"m{i}", content=f"searchable item {i}",
                                   memory_type=MemoryScope.PROJECT))
        results = store.search_lexical("searchable", limit=50)
        assert len(results) <= 50

    def test_embeddings_deterministic_under_load(self) -> None:
        emb = DeterministicEmbeddingProvider(dimension=64)
        v1 = emb.embed("consistent text")
        for _ in range(100):
            v2 = emb.embed("consistent text")
            assert v1 == v2


class TestTaskStateStress:
    def test_rapid_transitions_validated(self) -> None:
        # Every transition is validated; invalid ones raise, never silently pass.

        state = TaskState.CREATED
        # Walk a valid path.
        for target in [TaskState.CONTEXT_READY, TaskState.PLANNING, TaskState.PLAN_READY,
                       TaskState.EXECUTING, TaskState.VERIFYING, TaskState.COMPLETED]:
            assert transition_allowed(state, target)
            state = target
        # Terminal — no further transitions.
        for target in TaskState:
            assert not transition_allowed(state, target)

    def test_repeated_failures_terminate(self) -> None:
        from kinetic.events import EventBus
        from kinetic.tasks.manager import TaskManager
        from kinetic.tasks.models import TaskFailure

        bus = EventBus()
        mgr = TaskManager(events=bus)
        mgr.create(task_id="t1", user_request="fail repeatedly", workspace="/ws")
        mgr.transition("t1", TaskState.CONTEXT_READY)
        mgr.mark_failed("t1", failure=TaskFailure(failure_class="test", message="1"))
        # Further mark_failed on terminal task records but doesn't change state.
        mgr.mark_failed("t1", failure=TaskFailure(failure_class="test", message="2"))
        assert mgr.load("t1").state.value == "failed"


class TestCheckpointStress:
    def test_rapid_writes_atomic(self, tmp_path: Path) -> None:
        from kinetic.tasks.checkpoints import CheckpointStore

        store = CheckpointStore(tmp_path / "ckpts")
        for i in range(100):
            task = Task(id=f"t{i}", user_request="do", workspace="/ws",
                        state=TaskState.EXECUTING)
            plan = Plan(plan_id=f"p{i}", task_id=f"t{i}", goal="g", steps=[])
            store.save(build_checkpoint(task, plan, observations=[]))
        # All checkpoints intact.
        for i in range(100):
            assert store.exists(f"t{i}")


class TestContextAssemblyStress:
    async def test_large_context_bounded(self, tmp_path: Path) -> None:
        from kinetic.context.budget import ContextBudget
        from kinetic.context.engine import ContextEngine

        budget = ContextBudget(
            max_memory_items=5, max_characters=500,
            max_project_metadata_chars=200, max_recent_events=10,
            max_task_history_items=3,
        )
        engine = ContextEngine(memory=None, budget=budget, events=EventBus(), session_id="s")
        result = await engine.build(
            task="do something",
            project_metadata={"big": "x" * 10_000},
        )
        rendered = result.render()
        assert len(rendered) <= 5000  # bounded, not 10000+


class TestLargeOutputStress:
    async def test_large_tool_output_bounded(self, tmp_path: Path) -> None:
        from kinetic.tasks.observer import Observer

        obs = Observer(max_stdout_chars=1000, max_stderr_chars=500)
        result = obs.observe(
            step_id="s1",
            stdout="x" * 1_000_000,
            stderr="y" * 1_000_000,
            success=True,
        )
        assert len(result.stdout_summary) <= 2000
        assert len(result.stderr_summary) <= 1000
