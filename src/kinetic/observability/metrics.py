"""Lightweight, bounded in-process metrics for KINETIC.

No heavyweight monitoring dependency. The abstraction is intentionally minimal
so it can later be exported to Prometheus / OpenTelemetry by an adapter, but
the core is just counters, gauges, and timers held in a single
:class:`MetricsCollector`.

Design constraints:

* **Bounded + memory-safe**: the number of distinct metric *names* is capped.
  Beyond the cap, new names are dropped (counted as ``dropped_metrics``) rather
  than growing unboundedly. Counter/gauge values are scalars (no per-label
  cardinality explosion).
* **Thread-safe**: a single ``threading.Lock`` guards all mutations (metrics
  are written from tool callbacks, the event bus, and the task layer).
* **No secrets**: metric values are numbers only; no payloads are stored.
* **Snapshotable**: :meth:`snapshot` returns a plain dict for export/logging.

Wiring is opt-in: components that hold a reference to a collector record into
it; components without one (tests, legacy paths) are unaffected.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any


@dataclass
class _Counter:
    value: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def inc(self, amount: float = 1.0) -> None:
        if amount < 0:
            return
        with self._lock:
            self.value += amount


@dataclass
class _Gauge:
    value: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def set(self, value: float) -> None:
        with self._lock:
            self.value = float(value)

    def inc(self, amount: float = 1.0) -> None:
        with self._lock:
            self.value += amount

    def dec(self, amount: float = 1.0) -> None:
        with self._lock:
            self.value -= amount


class MetricsCollector:
    """A bounded, thread-safe collector of counters and gauges."""

    def __init__(self, *, max_metrics: int = 256) -> None:
        self._max = max(1, max_metrics)
        self._counters: dict[str, _Counter] = {}
        self._gauges: dict[str, _Gauge] = {}
        self._timers: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        self._dropped = 0

    # --- counters ----------------------------------------------------------

    def inc(self, name: str, amount: float = 1.0) -> None:
        """Increment a counter (creates it if room remains)."""
        with self._lock:
            c = self._counters.get(name)
            if c is None:
                if self._full():
                    self._dropped += 1
                    return
                c = _Counter()
                self._counters[name] = c
            c.inc(amount)

    # --- gauges ------------------------------------------------------------

    def set_gauge(self, name: str, value: float) -> None:
        with self._lock:
            g = self._gauges.get(name)
            if g is None:
                if self._full():
                    self._dropped += 1
                    return
                g = _Gauge()
                self._gauges[name] = g
            g.set(value)

    def inc_gauge(self, name: str, amount: float = 1.0) -> None:
        with self._lock:
            g = self._gauges.get(name)
            if g is None:
                if self._full():
                    self._dropped += 1
                    return
                g = _Gauge()
                self._gauges[name] = g
            g.inc(amount)

    def dec_gauge(self, name: str, amount: float = 1.0) -> None:
        self.inc_gauge(name, -amount)

    # --- timers ------------------------------------------------------------

    @contextmanager
    def timer(self, name: str):
        """Context manager that records an elapsed duration under ``name``.

        Timers keep the last ``max_timers_samples`` samples per name (bounded).
        """
        start = time.monotonic()
        try:
            yield
        finally:
            elapsed = time.monotonic() - start
            self._record_timer(name, elapsed)

    def _record_timer(self, name: str, elapsed: float) -> None:
        with self._lock:
            samples = self._timers.get(name)
            if samples is None:
                if self._full():
                    self._dropped += 1
                    return
                samples = []
                self._timers[name] = samples
            samples.append(elapsed)
            if len(samples) > _MAX_TIMER_SAMPLES:
                del samples[: len(samples) - _MAX_TIMER_SAMPLES]

    # --- inspection --------------------------------------------------------

    def counter(self, name: str) -> float:
        with self._lock:
            c = self._counters.get(name)
            return c.value if c else 0.0

    def gauge(self, name: str) -> float:
        with self._lock:
            g = self._gauges.get(name)
            return g.value if g else 0.0

    def timer_stats(self, name: str) -> dict[str, float]:
        with self._lock:
            samples = list(self._timers.get(name, []))
        if not samples:
            return {"count": 0, "sum": 0.0, "avg": 0.0, "min": 0.0, "max": 0.0}
        return {
            "count": len(samples),
            "sum": sum(samples),
            "avg": sum(samples) / len(samples),
            "min": min(samples),
            "max": max(samples),
        }

    def snapshot(self) -> dict[str, Any]:
        """Return a plain-dict snapshot of all metrics (for export/logging)."""
        with self._lock:
            counters = {k: v.value for k, v in self._counters.items()}
            gauges = {k: v.value for k, v in self._gauges.items()}
            timers = {
                k: {
                    "count": len(v),
                    "sum": sum(v),
                    "avg": sum(v) / len(v) if v else 0.0,
                    "min": min(v) if v else 0.0,
                    "max": max(v) if v else 0.0,
                }
                for k, v in self._timers.items()
            }
        return {
            "counters": counters,
            "gauges": gauges,
            "timers": timers,
            "dropped_metrics": self._dropped,
        }

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._timers.clear()
            self._dropped = 0

    # --- internals ---------------------------------------------------------

    def _full(self) -> bool:
        total = len(self._counters) + len(self._gauges) + len(self._timers)
        return total >= self._max


_MAX_TIMER_SAMPLES = 128

#: Standard metric names used across the system.
METRIC_TASKS_STARTED = "tasks.started"
METRIC_TASKS_COMPLETED = "tasks.completed"
METRIC_TASKS_FAILED = "tasks.failed"
METRIC_TASKS_CANCELLED = "tasks.cancelled"
METRIC_TASK_DURATION = "tasks.duration_seconds"
METRIC_STEPS_EXECUTED = "steps.executed"
METRIC_VERIFICATION_ATTEMPTS = "verification.attempts"
METRIC_REPAIR_ATTEMPTS = "repair.attempts"
METRIC_RECOVERY_FAILURES = "recovery.failures"
METRIC_ENV_CREATED = "environment.created"
METRIC_ENV_DESTROYED = "environment.destroyed"
METRIC_TOOL_FAILURES = "tools.failures"
METRIC_PERMISSION_DENIALS = "permissions.denied"


__all__ = [
    "METRIC_ENV_CREATED",
    "METRIC_ENV_DESTROYED",
    "METRIC_PERMISSION_DENIALS",
    "METRIC_RECOVERY_FAILURES",
    "METRIC_REPAIR_ATTEMPTS",
    "METRIC_STEPS_EXECUTED",
    "METRIC_TASKS_CANCELLED",
    "METRIC_TASKS_COMPLETED",
    "METRIC_TASKS_FAILED",
    "METRIC_TASKS_STARTED",
    "METRIC_TASK_DURATION",
    "METRIC_TOOL_FAILURES",
    "METRIC_VERIFICATION_ATTEMPTS",
    "MetricsCollector",
]
