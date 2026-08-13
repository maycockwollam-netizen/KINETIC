"""Phase 7 — metrics tests."""

from __future__ import annotations

import time
from pathlib import Path

from observability.metrics import (
    METRIC_TASKS_COMPLETED,
    METRIC_TASKS_FAILED,
    MetricsCollector,
)


class TestCounters:
    def test_increment(self) -> None:
        m = MetricsCollector()
        m.inc("a")
        m.inc("a")
        m.inc("b")
        assert m.counter("a") == 2.0
        assert m.counter("b") == 1.0

    def test_increment_by_amount(self) -> None:
        m = MetricsCollector()
        m.inc("x", 5)
        assert m.counter("x") == 5.0

    def test_negative_increment_ignored(self) -> None:
        m = MetricsCollector()
        m.inc("x", -1)
        assert m.counter("x") == 0.0


class TestGauges:
    def test_set(self) -> None:
        m = MetricsCollector()
        m.set_gauge("g", 42)
        assert m.gauge("g") == 42.0

    def test_inc_dec(self) -> None:
        m = MetricsCollector()
        m.set_gauge("g", 10)
        m.inc_gauge("g")
        m.dec_gauge("g", 2)
        assert m.gauge("g") == 9.0


class TestTimers:
    def test_timer_records(self) -> None:
        m = MetricsCollector()
        with m.timer("op"):
            time.sleep(0.01)
        stats = m.timer_stats("op")
        assert stats["count"] == 1
        assert stats["sum"] > 0
        assert stats["min"] == stats["max"]

    def test_multiple_samples(self) -> None:
        m = MetricsCollector()
        for _ in range(3):
            with m.timer("op"):
                time.sleep(0.005)
        stats = m.timer_stats("op")
        assert stats["count"] == 3


class TestBounded:
    def test_max_metrics_cap(self) -> None:
        m = MetricsCollector(max_metrics=5)
        for i in range(10):
            m.inc(f"counter_{i}")
        snap = m.snapshot()
        # Only 5 should be stored; the rest dropped.
        assert len(snap["counters"]) <= 5
        assert snap["dropped_metrics"] > 0

    def test_timer_samples_bounded(self) -> None:
        m = MetricsCollector()
        for _ in range(500):
            m._record_timer("t", 0.001)
        stats = m.timer_stats("t")
        assert stats["count"] <= 200  # bounded


class TestSnapshot:
    def test_snapshot_structure(self) -> None:
        m = MetricsCollector()
        m.inc(METRIC_TASKS_COMPLETED)
        m.inc(METRIC_TASKS_FAILED, 2)
        m.set_gauge("active", 3)
        snap = m.snapshot()
        assert "counters" in snap
        assert "gauges" in snap
        assert "timers" in snap
        assert snap["counters"][METRIC_TASKS_COMPLETED] == 1.0
        assert snap["counters"][METRIC_TASKS_FAILED] == 2.0
        assert snap["gauges"]["active"] == 3.0

    def test_reset(self) -> None:
        m = MetricsCollector()
        m.inc("a")
        m.reset()
        assert m.counter("a") == 0.0


class TestWiring:
    """Verify metrics are recorded by the wired-in components."""

    def test_task_manager_records_metrics(self) -> None:
        from events import EventBus
        from tasks.manager import TaskManager
        from tasks.states import TaskState

        m = MetricsCollector()
        bus = EventBus()
        mgr = TaskManager(events=bus, metrics=m)
        mgr.create(task_id="t1", user_request="do thing", workspace="/ws")
        # Walk the state machine to COMPLETED.
        mgr.transition("t1", TaskState.CONTEXT_READY)
        mgr.transition("t1", TaskState.PLANNING)
        mgr.transition("t1", TaskState.PLAN_READY)
        mgr.transition("t1", TaskState.EXECUTING)
        mgr.transition("t1", TaskState.VERIFYING)
        mgr.mark_completed("t1")
        snap = m.snapshot()
        assert snap["counters"].get("tasks.started") == 1
        assert snap["counters"].get("tasks.completed") == 1

    def test_task_failed_metric(self) -> None:
        from events import EventBus
        from tasks.manager import TaskManager
        from tasks.models import TaskFailure
        from tasks.states import TaskState

        m = MetricsCollector()
        bus = EventBus()
        mgr = TaskManager(events=bus, metrics=m)
        mgr.create(task_id="t2", user_request="fail", workspace="/ws")
        mgr.transition("t2", TaskState.CONTEXT_READY)
        mgr.mark_failed("t2", failure=TaskFailure(failure_class="test_failure", message="boom"))
        snap = m.snapshot()
        assert snap["counters"].get("tasks.failed") == 1

    def test_environment_metrics(self) -> None:
        from environment import Environment
        from environment.config import EnvironmentConfig

        m = MetricsCollector()
        cfg = EnvironmentConfig(runtime_type="local", sandbox_mode=False)
        Environment(Path("/tmp"), cfg, metrics=m)
        snap = m.snapshot()
        assert snap["counters"].get("environment.created") == 1
