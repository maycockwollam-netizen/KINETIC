"""The Web Agent Test Console: a thin adapter over the existing KINETIC backend.

This module owns NO execution path of its own. For each task it builds the
real :class:`~agent.session.AgentSession` + :class:`~tasks.orchestrator.Orchestrator`
+ :class:`~environment.Environment` + :class:`~events.EventBus` stack and runs
the task in a background asyncio task. It only:

* routes HTTP requests to the existing TaskManager (state) and Orchestrator
  (execution);
* exposes the task's EventBus as a bounded event ring for SSE;
* forwards cancellation to the existing cooperative cancellation mechanism;
* tears every run down deterministically via :meth:`shutdown`.

The web layer never imports subprocess, never mutates the filesystem, and never
constructs a second ToolRegistry / PermissionPolicy — every tool call still
flows through the single safe path inside the existing AgentSession.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from agent.session import AgentSession, SessionConfig
from errors import (
    KINETICError,
    TaskError,
    TaskStateError,
)
from events import EventBus
from events.types import Event
from lifecycle import ShutdownCoordinator
from tasks.orchestrator import Orchestrator

if TYPE_CHECKING:
    from config import Settings
    from project.models import ProjectManifest
    from tasks.executor import ExecutionOutcome
    from tasks.manager import TaskManager


class OrchestratorFactory(Protocol):
    """Builds an Orchestrator for a session. Overridable for tests."""

    def __call__(self, session: AgentSession, *, settings: Settings,
                 manifest: ProjectManifest | None) -> Orchestrator: ...


def default_orchestrator_factory(
    session: AgentSession, *, settings: Settings, manifest: ProjectManifest | None,
) -> Orchestrator:
    return Orchestrator(session, settings=settings, manifest=manifest)


@dataclass
class TaskRun:
    """One in-flight (or finished) task run owned by the console."""

    task_id: str
    session: AgentSession
    orchestrator: Orchestrator
    events: EventBus
    event_log: deque[Event]
    background: asyncio.Task | None = None
    outcome: ExecutionOutcome | None = None
    error: str | None = None
    started_at: float = 0.0
    finished_at: float | None = None
    last_event_id: int = 0
    pump_task: asyncio.Task | None = None

    @property
    def is_terminal(self) -> bool:
        return self.background is None or self.background.done()


def _now() -> float:
    """Best-effort monotonic timestamp; safe when no loop is running."""
    try:
        return asyncio.get_running_loop().time()
    except RuntimeError:
        import time

        return time.monotonic()


class WebConsole:
    """Owns the set of task runs and routes operations to the backend.

    The console holds *references* to backend objects only; it never
    duplicates their state. Task state is always read from the TaskManager;
    execution is always driven by the Orchestrator; events always come from the
    session's EventBus.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        workspace: Path,
        orchestrator_factory: OrchestratorFactory = default_orchestrator_factory,
        require_api_key: bool = True,
    ) -> None:
        self._settings = settings
        self._workspace = Path(workspace).resolve()
        self._factory = orchestrator_factory
        self._require_api_key = require_api_key
        self._runs: dict[str, TaskRun] = {}
        self._lock = asyncio.Lock()
        self._shutdown = ShutdownCoordinator(timeout=30.0)
        self._closed = False

    # --- properties --------------------------------------------------------

    @property
    def workspace(self) -> Path:
        return self._workspace

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def is_closed(self) -> bool:
        return self._closed

    def tasks(self) -> list[str]:
        return list(self._runs.keys())

    # --- task operations ---------------------------------------------------

    def get_run(self, task_id: str) -> TaskRun | None:
        return self._runs.get(task_id)

    def manager(self, task_id: str) -> TaskManager:
        run = self._runs.get(task_id)
        if run is None:
            raise KeyError(task_id)
        return run.orchestrator.manager

    async def create_task(self, *, prompt: str) -> dict[str, Any]:
        """Create a task and start it in the background.

        Returns ``{task_id, state}``. The task runs through the existing
        Orchestrator safe path; the console only holds a reference.
        """
        self._reject_if_closed()
        if not prompt or not prompt.strip():
            raise ValueError("prompt must be non-empty")
        if self._require_api_key:
            import os

            if not os.environ.get("ANTHROPIC_API_KEY"):
                from errors import KINETICError

                raise KINETICError(
                    "ANTHROPIC_API_KEY is not set; the web console cannot run live "
                    "agent tasks without it."
                )

        async with self._lock:
            cfg = SessionConfig(workspace=self._workspace, prompt=prompt)
            metrics = self._build_metrics()
            session = AgentSession(self._settings, cfg, metrics=metrics)
            manifest = self._scan_manifest()
            orch = self._factory(session, settings=self._settings, manifest=manifest)
            from tasks.manager import default_task_id

            task_id = default_task_id()
            event_log: deque[Event] = deque(maxlen=self._settings.web_max_event_log)
            run = TaskRun(
                task_id=task_id, session=session, orchestrator=orch,
                events=session.events, event_log=event_log,
            )
            self._runs[task_id] = run
            run.background = asyncio.create_task(self._drive(run, prompt))
            self._shutdown.register(f"task:{task_id}", lambda tid=task_id: self._dispose(tid))
        try:
            task = orch.manager.load(task_id)
        except KeyError:
            task = None
        state = task.state.value if task else "created"
        return {"task_id": task_id, "state": state}

    async def _drive(self, run: TaskRun, prompt: str) -> None:
        """Run a task to completion, capturing events + outcome.

        The Orchestrator owns provisioning, planning, execution, verification,
        recovery, and teardown. We only (a) subscribe to capture events into
        the bounded per-task log, and (b) record the bounded outcome.
        """
        sub: Any = None
        run.started_at = _now()
        try:
            sub = await run.events.subscribe()
            run.pump_task = asyncio.create_task(self._pump_events(run, sub))
            outcome = await run.orchestrator.run_task(
                user_request=prompt, workspace=str(self._workspace), task_id=run.task_id,
            )
            run.outcome = outcome
        except asyncio.CancelledError:
            run.error = "cancelled"
            raise
        except KINETICError as exc:
            run.error = str(exc)
        except Exception as exc:  # noqa: BLE001 - never leak a raw trace to callers
            run.error = f"{type(exc).__name__}: {exc}"
        finally:
            run.finished_at = _now()
            if sub is not None:
                sub.close()
            pump = run.pump_task
            if pump is not None and not pump.done():
                try:
                    await asyncio.wait_for(pump, timeout=self._settings.web_event_poll_timeout)
                except (TimeoutError, asyncio.CancelledError, RuntimeError):
                    pump.cancel()

    async def _pump_events(self, run: TaskRun, sub: Any) -> None:
        """Copy subscribed events into the bounded per-task ring.

        The EventBus already redacts + caps payloads; we keep a bounded ring
        so a late SSE client still sees recent history without unbounded memory.
        """
        try:
            async for event in sub:
                run.event_log.append(event)
                run.last_event_id += 1
        except asyncio.CancelledError:
            return

    async def cancel(self, task_id: str, *, reason: str = "cancelled via web console") -> dict[str, Any]:
        """Cancel a running task via the existing cooperative mechanism."""
        self._reject_if_closed()
        run = self._runs.get(task_id)
        if run is None:
            raise KeyError(task_id)
        with contextlib.suppress(Exception):
            run.session.cancellation.cancel()
        try:
            run.orchestrator.cancel(task_id, reason=reason)
        except TaskStateError:
            # Already terminal — that's fine, return current state.
            pass
        except Exception:  # noqa: BLE001 - a failed cancel records but does not raise
            pass
        await asyncio.sleep(0)
        try:
            task = run.orchestrator.manager.load(task_id)
            state = task.state.value
        except KeyError:
            state = "cancelled"
        return {"task_id": task_id, "state": state, "cancelled": True}

    async def resume(self, task_id: str) -> dict[str, Any]:
        """Resume a checkpointed task via the existing Orchestrator path."""
        self._reject_if_closed()
        if self._require_api_key:
            import os

            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise KINETICError(
                    "ANTHROPIC_API_KEY is not set; resume requires a live model."
                )
        run = self._runs.get(task_id)
        if run is not None and not run.is_terminal:
            raise TaskError(f"task {task_id} is still running")
        async with self._lock:
            cfg = SessionConfig(workspace=self._workspace, prompt="(resume)")
            metrics = self._build_metrics()
            session = AgentSession(self._settings, cfg, metrics=metrics)
            manifest = self._scan_manifest()
            orch = self._factory(session, settings=self._settings, manifest=manifest)
            event_log: deque[Event] = deque(maxlen=self._settings.web_max_event_log)
            run = TaskRun(
                task_id=task_id, session=session, orchestrator=orch,
                events=session.events, event_log=event_log,
            )
            self._runs[task_id] = run
            run.background = asyncio.create_task(self._drive_resume(run, task_id))
            self._shutdown.register(f"task:{task_id}", lambda tid=task_id: self._dispose(tid))
        return {"task_id": task_id, "state": "resuming"}

    async def _drive_resume(self, run: TaskRun, task_id: str) -> None:
        sub: Any = None
        run.started_at = _now()
        try:
            sub = await run.events.subscribe()
            run.pump_task = asyncio.create_task(self._pump_events(run, sub))
            outcome = await run.orchestrator.resume_task(task_id)
            run.outcome = outcome
        except asyncio.CancelledError:
            run.error = "cancelled"
            raise
        except KINETICError as exc:
            run.error = str(exc)
        except Exception as exc:  # noqa: BLE001
            run.error = f"{type(exc).__name__}: {exc}"
        finally:
            run.finished_at = _now()
            if sub is not None:
                sub.close()
            pump = run.pump_task
            if pump is not None and not pump.done():
                try:
                    await asyncio.wait_for(pump, timeout=self._settings.web_event_poll_timeout)
                except (TimeoutError, asyncio.CancelledError, RuntimeError):
                    pump.cancel()

    # --- inspection --------------------------------------------------------

    def task_snapshot(self, task_id: str) -> dict[str, Any]:
        """A bounded, safe snapshot of a task's current state."""
        run = self._runs.get(task_id)
        if run is None:
            raise KeyError(task_id)
        from web.serialize import serialize_outcome, serialize_task

        try:
            task = run.orchestrator.manager.load(task_id)
            snapshot = serialize_task(task)
        except KeyError:
            snapshot = {"id": task_id, "state": "unknown"}
        snapshot["running"] = not run.is_terminal
        snapshot["error"] = run.error
        if run.outcome is not None:
            snapshot["outcome"] = serialize_outcome(run.outcome)
        return snapshot

    def recent_events(self, task_id: str, *, after: int = 0) -> list[dict[str, Any]]:
        """Return masked events with id > ``after`` for replay on reconnect."""
        run = self._runs.get(task_id)
        if run is None:
            raise KeyError(task_id)
        from web.serialize import serialize_event

        out: list[dict[str, Any]] = []
        idx = 0
        for event in list(run.event_log):
            idx += 1
            if idx <= after:
                continue
            payload = serialize_event(event)
            payload["id"] = idx
            out.append(payload)
        return out

    # --- lifecycle ---------------------------------------------------------

    def register_shutdown(self) -> ShutdownCoordinator:
        return self._shutdown

    async def shutdown(self, *, reason: str = "web console shutdown") -> None:
        """Cancel every running task and run registered cleanups."""
        if self._closed:
            return
        self._closed = True
        for _tid, run in list(self._runs.items()):
            if run.background is not None and not run.background.done():
                run.background.cancel()
        await self._shutdown.shutdown(reason=reason)

    def _dispose(self, task_id: str) -> None:
        run = self._runs.pop(task_id, None)
        if run is not None and run.background is not None and not run.background.done():
            run.background.cancel()

    def _reject_if_closed(self) -> None:
        if self._closed:
            raise KINETICError("web console is shutting down")

    def _build_metrics(self) -> Any:
        try:
            from observability import MetricsCollector

            return MetricsCollector()
        except Exception:  # noqa: BLE001
            return None

    def _scan_manifest(self) -> Any:
        try:
            from project.scanner import scan_project

            return scan_project(self._workspace)
        except Exception:  # noqa: BLE001 - scanning is best-effort
            return None


__all__ = ["OrchestratorFactory", "TaskRun", "WebConsole", "default_orchestrator_factory"]
