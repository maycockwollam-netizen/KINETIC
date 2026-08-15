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
import os
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from agent.approvals import PendingApprovalRegistry
from agent.session import AgentSession, SessionConfig
from errors import (
    KINETICError,
    TaskError,
    TaskStateError,
)
from events import EventBus
from events.types import Event
from lifecycle import ShutdownCoordinator
from store import AgentConfig, AutomationConfig, FileEntry, JsonStore
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
        # LLM provider overrides (base_url + model are persisted; api_key is
        # held in memory ONLY — never persisted, never returned to the client).
        self._llm_base_url: str | None = settings.llm_base_url
        self._llm_model: str | None = None
        self._llm_api_key: str | None = None
        self._interactive_approval: bool = False
        # Persistent stores (small JSON files under the data dir). The web
        # layer calls these methods; no filesystem mutation happens in web/*.py.
        data_dir = Path.home() / ".kinetic"
        self._agents_store: JsonStore[AgentConfig] = JsonStore(
            data_dir / "agents.json", AgentConfig
        )
        self._automations_store: JsonStore[AutomationConfig] = JsonStore(
            data_dir / "automations.json", AutomationConfig
        )
        self._files_store: JsonStore[FileEntry] = JsonStore(
            data_dir / "files.json", FileEntry
        )
        # In-memory registry of pending tool approvals, keyed by task_id.
        self._approvals = PendingApprovalRegistry()

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

    async def create_task(
        self,
        *,
        prompt: str,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        agent_id: str | None = None,
        interactive_approval: bool | None = None,
        enable_repair: bool | None = None,
    ) -> dict[str, Any]:
        """Create a task and start it in the background.

        Returns ``{task_id, state}``. The task runs through the existing
        Orchestrator safe path; the console only holds a reference.

        Per-task overrides: ``model`` / ``base_url`` / ``api_key`` let the
        browser choose the LLM provider per request (proxy support). ``api_key``
        is held in memory only for the duration of this task run — never
        persisted, never returned. ``agent_id`` applies a saved agent preset's
        model + system prompt. ``interactive_approval`` engages the human
        approval gate; ``enable_repair`` toggles Phase 6 repair.
        """
        self._reject_if_closed()
        if not prompt or not prompt.strip():
            raise ValueError("prompt must be non-empty")

        # Resolve the effective LLM config: per-task override > console default.
        eff_model = model
        eff_base_url = base_url or self._llm_base_url
        eff_api_key = api_key or self._llm_api_key
        eff_system_prompt: str | None = None
        eff_repair = enable_repair
        # Apply a saved agent preset (model + system prompt) if requested.
        if agent_id:
            agent = self._agents_store.get(agent_id)
            if agent is None:
                raise ValueError(f"unknown agent: {agent_id}")
            if eff_model is None and agent.model:
                eff_model = agent.model
            if agent.system_prompt:
                eff_system_prompt = agent.system_prompt

        # API key resolution: per-task > console override > env. We require a
        # key only when none of the three is available.
        has_key = bool(eff_api_key or os.environ.get("ANTHROPIC_API_KEY"))
        if self._require_api_key and not has_key:
            raise KINETICError(
                "ANTHROPIC_API_KEY is not set; the web console cannot run live "
                "agent tasks without it."
            )

        if interactive_approval is None:
            interactive_approval = self._interactive_approval

        async with self._lock:
            cfg = SessionConfig(
                workspace=self._workspace,
                prompt=prompt,
                model=eff_model,
                system_prompt=eff_system_prompt,
                base_url=eff_base_url,
                api_key=eff_api_key,
                interactive_approval=bool(interactive_approval),
                enable_repair=eff_repair,
            )
            metrics = self._build_metrics()
            session = AgentSession(self._settings, cfg, metrics=metrics)
            if cfg.interactive_approval:
                session.approval_registry = self._approvals
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

    # --- configuration & resources ---------------------------------------
    # These methods back the new web API surfaces: LLM config, agents,
    # automations, file uploads, and interactive approvals. All persistence
    # goes through the store package (no filesystem mutation in web/*.py);
    # the API key is held in memory only and NEVER returned to the client.

    def get_llm_config(self) -> dict[str, Any]:
        """Return the current LLM config. The API key is NEVER included —
        only a boolean ``api_key_set`` flag."""
        return {
            "base_url": self._llm_base_url,
            "model": self._llm_model,
            "api_key_set": bool(self._llm_api_key),
            "interactive_approval": self._interactive_approval,
            "env_api_key_set": bool(os.environ.get("ANTHROPIC_API_KEY")),
        }

    def set_llm_config(
        self, *, base_url: str | None = None, api_key: str | None = None,
        model: str | None = None, interactive_approval: bool | None = None,
    ) -> dict[str, Any]:
        """Update LLM config defaults. ``base_url``/``model`` may persist; the
        ``api_key`` is held in memory only (cleared when the process exits)."""
        self._reject_if_closed()
        if base_url is not None and base_url.strip() == "":
            base_url = None
        if base_url is not None:
            from urllib.parse import urlparse

            parsed = urlparse(base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("base_url must be an http(s) URL")
            self._llm_base_url = base_url
        if api_key is not None:
            # Empty string explicitly clears the override.
            self._llm_api_key = api_key or None
        if model is not None:
            self._llm_model = model or None
        if interactive_approval is not None:
            self._interactive_approval = bool(interactive_approval)
        return self.get_llm_config()

    # --- agents ----------------------------------------------------------

    def list_agents(self) -> list[dict[str, Any]]:
        return [a.model_dump(mode="json") for a in self._agents_store.list()]

    def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        a = self._agents_store.get(agent_id)
        return a.model_dump(mode="json") if a else None

    def save_agent(self, data: dict[str, Any]) -> dict[str, Any]:
        self._reject_if_closed()
        agent_id = data.get("id") or uuid.uuid4().hex
        existing = self._agents_store.get(agent_id)
        if existing is not None:
            existing.name = str(data.get("name", existing.name))
            existing.description = str(data.get("description", existing.description))
            existing.icon = str(data.get("icon", existing.icon))
            existing.model = data.get("model", existing.model)
            existing.system_prompt = str(data.get("system_prompt", existing.system_prompt))
            tools = data.get("tools")
            if isinstance(tools, list):
                existing.tools = [str(t) for t in tools]
            if "active" in data:
                existing.active = bool(data["active"])
            from datetime import UTC, datetime

            existing.updated_at = datetime.now(UTC).isoformat()
            record = existing
        else:
            record = AgentConfig(
                id=agent_id,
                name=str(data.get("name", "Agent")),
                description=str(data.get("description", "")),
                icon=str(data.get("icon", "🤖")),
                model=data.get("model"),
                system_prompt=str(data.get("system_prompt", "")),
                tools=[str(t) for t in data.get("tools", []) if isinstance(t, str)],
                active=bool(data.get("active", False)),
            )
        self._agents_store.upsert(record)
        return record.model_dump(mode="json")

    def delete_agent(self, agent_id: str) -> bool:
        return self._agents_store.delete(agent_id)

    # --- automations ----------------------------------------------------

    def list_automations(self) -> list[dict[str, Any]]:
        return [a.model_dump(mode="json") for a in self._automations_store.list()]

    def get_automation(self, automation_id: str) -> dict[str, Any] | None:
        a = self._automations_store.get(automation_id)
        return a.model_dump(mode="json") if a else None

    def save_automation(self, data: dict[str, Any]) -> dict[str, Any]:
        self._reject_if_closed()
        automation_id = data.get("id") or uuid.uuid4().hex
        existing = self._automations_store.get(automation_id)
        if existing is not None:
            existing.name = str(data.get("name", existing.name))
            existing.cron = str(data.get("cron", existing.cron))
            existing.schedule_label = str(data.get("schedule_label", existing.schedule_label))
            existing.prompt = str(data.get("prompt", existing.prompt))
            existing.agent_id = data.get("agent_id", existing.agent_id)
            if "enabled" in data:
                existing.enabled = bool(data["enabled"])
            from datetime import UTC, datetime

            existing.updated_at = datetime.now(UTC).isoformat()
            record = existing
        else:
            record = AutomationConfig(
                id=automation_id,
                name=str(data.get("name", "Automation")),
                cron=str(data.get("cron", "")),
                schedule_label=str(data.get("schedule_label", "")),
                prompt=str(data.get("prompt", "")),
                agent_id=data.get("agent_id"),
                enabled=bool(data.get("enabled", True)),
            )
        self._automations_store.upsert(record)
        return record.model_dump(mode="json")

    def delete_automation(self, automation_id: str) -> bool:
        return self._automations_store.delete(automation_id)

    async def run_automation_now(self, automation_id: str) -> dict[str, Any]:
        """Run an automation immediately by creating a real task.

        This is NOT a scheduled run — it kicks a real task now and records the
        honest outcome (task_id + started timestamp). A background cron scheduler
        is not part of the web console; scheduling is a separate concern.
        """
        self._reject_if_closed()
        a = self._automations_store.get(automation_id)
        if a is None:
            raise KeyError(automation_id)
        result = await self.create_task(
            prompt=a.prompt,
            model=None,
            agent_id=a.agent_id,
        )
        # Record the honest last-run info (no faking next_run).
        from datetime import UTC, datetime

        a.last_run_at = datetime.now(UTC).isoformat()
        a.last_run_status = "started"
        a.last_run_task_id = result.get("task_id")
        a.history.insert(0, {
            "task_id": result.get("task_id"),
            "at": a.last_run_at,
            "status": "started",
        })
        a.history = a.history[:20]
        a.updated_at = a.last_run_at
        self._automations_store.upsert(a)
        result["automation_id"] = automation_id
        return result

    # --- files ----------------------------------------------------------

    def list_files(self) -> list[dict[str, Any]]:
        return [f.model_dump(mode="json") for f in self._files_store.list()]

    async def save_upload(self, *, name: str, content: bytes, content_type: str = "") -> dict[str, Any]:
        """Persist an uploaded file into the workspace upload dir + record it.

        The actual filesystem write is delegated to :mod:`store.files` so that
        no write pattern appears in the web layer (enforced by the security
        test). The web layer only reads the upload stream and calls this method.
        """
        from store.files import save_upload_file

        self._reject_if_closed()
        entry = await save_upload_file(
            store=self._files_store,
            workspace=self._workspace,
            name=name,
            content=content,
            content_type=content_type,
        )
        return entry.model_dump(mode="json")

    def delete_file(self, file_id: str) -> bool:
        from store.files import delete_upload_file

        entry = self._files_store.get(file_id)
        if entry is None:
            return False
        return delete_upload_file(self._files_store, self._workspace, file_id)

    # --- approvals -----------------------------------------------------

    def list_pending_approvals(self, task_id: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for req in self._approvals.list_pending(task_id=task_id):
            out.append({
                "request_id": req.request_id,
                "task_id": req.task_id,
                "tool": req.tool,
                "reason": req.reason,
            })
        return out

    def resolve_approval(self, task_id: str, request_id: str, *, allow: bool) -> bool:
        """Resolve a pending interactive approval (allow/deny) from the UI."""
        req = self._approvals.get(request_id)
        if req is None or req.task_id != task_id or req.resolved:
            return False
        return self._approvals.resolve(request_id, allow=allow, decision="user")

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
