"""Context assembly engine.

Gathers and trims context from multiple sources into a bounded package:

  * current task (prompt)
  * relevant memories (hybrid retrieval, ranked)
  * project metadata (architecture, conventions)
  * workspace state
  * recent task events

Each source is capped by :class:`ContextBudget`; the whole package is capped by
``max_characters``. What gets cut is recorded in ``omissions``.

The engine is failure-safe: a memory backend failure yields a degraded but
valid package (no memories, a note) — it never invents memories or crashes the
task.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from context.budget import ContextBudget
from context.models import ContextPackage, ContextSection, OmissionRecord
from events import EventBus, EventType
from memory.lifecycle import MemoryManager
from memory.metadata import MemoryFilter
from memory.models import MemoryScope


class ContextEngine:
    """Assembles a bounded context package from memories + project state."""

    def __init__(
        self,
        *,
        memory: MemoryManager | None = None,
        budget: ContextBudget | None = None,
        events: EventBus | None = None,
        session_id: str = "context",
    ) -> None:
        self._memory = memory
        self._budget = budget or ContextBudget()
        self._events = events
        self._session_id = session_id

    async def build(
        self,
        *,
        task: str,
        project_metadata: dict[str, Any] | None = None,
        workspace_state: dict[str, Any] | None = None,
        recent_events: list[dict[str, Any]] | None = None,
        task_history: list[str] | None = None,
        project_id: str | None = None,
        query: str | None = None,
    ) -> ContextPackage:
        """Assemble the context package bounded by the configured budget."""
        sections: list[ContextSection] = []
        omissions: list[OmissionRecord] = []
        degraded = False
        note: str | None = None

        # 1. Current task.
        task_text = (task or "").strip()
        sections.append(ContextSection(name="Current Task", content=task_text, char_count=len(task_text)))

        # 2. Relevant memories (failure-safe).
        memory_count = 0
        if self._memory is not None:
            try:
                mem_filter = MemoryFilter.for_project(
                    project_id=project_id,
                    memory_types={MemoryScope.PROJECT, MemoryScope.AGENT, MemoryScope.TASK},
                )
                # Fetch more than the cap so we can record omissions for items
                # that were relevant but cut to respect the budget.
                fetch_limit = max(self._budget.max_memory_items * 2, self._budget.max_memory_items + 4)
                ranked = await self._memory.search(
                    query or task_text or "",
                    filter=mem_filter,
                    limit=fetch_limit,
                )
                capped = ranked[: self._budget.max_memory_items]
                if len(ranked) > len(capped):
                    omissions.append(OmissionRecord(
                        section="Memories", reason="budget cap",
                        count=len(ranked) - len(capped),
                    ))
                mem_lines = [
                    f"- [{r.record.memory_type.value}] {r.record.content}" for r in capped
                ]
                mem_text = "\n".join(mem_lines)
                memory_count = len(capped)
                sections.append(ContextSection(
                    name="Relevant Memories", content=mem_text, char_count=len(mem_text)
                ))
            except Exception as exc:  # noqa: BLE001 - degrade, do not fabricate
                degraded = True
                note = f"memory retrieval failed ({exc}); context assembled without memories"
                omissions.append(OmissionRecord(section="Memories", reason=f"retrieval error: {exc}"))
                self._emit_failure("context_memory", str(exc))

        # 3. Project metadata.
        if project_metadata:
            pm_text = self._render_dict(project_metadata)
            pm_text, pm_trunc = self._truncate(pm_text, self._budget.max_project_metadata_chars)
            sections.append(ContextSection(
                name="Project Metadata", content=pm_text, char_count=len(pm_text), truncated=pm_trunc,
            ))
            if pm_trunc:
                omissions.append(OmissionRecord(
                    section="Project Metadata", reason="char budget", count=1
                ))

        # 4. Workspace state.
        if workspace_state:
            ws_text = self._render_dict(workspace_state)
            sections.append(ContextSection(name="Workspace State", content=ws_text, char_count=len(ws_text)))

        # 5. Recent events.
        if recent_events:
            capped_events = recent_events[-self._budget.max_recent_events :]
            ev_text = self._render_events(capped_events)
            if len(recent_events) > len(capped_events):
                omissions.append(OmissionRecord(
                    section="Recent Events", reason="budget cap",
                    count=len(recent_events) - len(capped_events),
                ))
            sections.append(ContextSection(name="Recent Events", content=ev_text, char_count=len(ev_text)))

        # 6. Task history (bounded; NOT raw conversation dump).
        if task_history:
            capped_hist = task_history[-self._budget.max_task_history_items :]
            hist_text = "\n".join(f"- {h}" for h in capped_hist)
            if len(task_history) > len(capped_hist):
                omissions.append(OmissionRecord(
                    section="Task History", reason="budget cap",
                    count=len(task_history) - len(capped_hist),
                ))
            sections.append(ContextSection(name="Task History", content=hist_text, char_count=len(hist_text)))

        # 7. Global character budget across all sections.
        package = self._enforce_global_budget(sections, omissions, memory_count, degraded, note)
        self._emit(
            EventType.CONTEXT_BUILT,
            total_characters=package.total_characters,
            memory_count=package.memory_count,
            section_count=len(package.sections),
            degraded=package.degraded,
        )
        if package.omissions:
            self._emit(
                EventType.CONTEXT_BUDGET_EXCEEDED,
                omissions=len(package.omissions),
            )
        return package

    # --- helpers -----------------------------------------------------------

    @staticmethod
    def _render_dict(d: dict[str, Any]) -> str:
        return "\n".join(f"- {k}: {v}" for k, v in d.items() if v is not None)

    @staticmethod
    def _render_events(events: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for ev in events:
            t = ev.get("type") or ev.get("event") or "event"
            lines.append(f"- [{t}] {ev.get('summary', '')}".strip())
        return "\n".join(lines)

    @staticmethod
    def _truncate(text: str, limit: int) -> tuple[str, bool]:
        if limit and len(text) > limit:
            return text[:limit] + "\n…(truncated)", True
        return text, False

    def _enforce_global_budget(
        self,
        sections: list[ContextSection],
        omissions: list[OmissionRecord],
        memory_count: int,
        degraded: bool,
        note: str | None,
    ) -> ContextPackage:
        """Trim sections from the lowest-priority end until within budget.

        Sections are kept in insertion order; if the total exceeds
        ``max_characters``, later (lower-priority) sections are dropped first
        and recorded as omissions.
        """
        total = sum(s.char_count for s in sections)
        while total > self._budget.max_characters and len(sections) > 1:
            dropped = sections.pop()
            total -= dropped.char_count
            omissions.append(OmissionRecord(
                section=dropped.name, reason="global char budget", count=1
            ))
        return ContextPackage(
            sections=sections,
            omissions=omissions,
            total_characters=total,
            memory_count=memory_count,
            degraded=degraded,
            degradation_note=note,
        )

    def _emit(self, event_type: EventType, **data: Any) -> None:
        if self._events is not None:
            self._events.emit(event_type, self._session_id, **data)

    def _emit_failure(self, action: str, reason: str) -> None:
        self._emit(EventType.AGENT_ERROR, action=action, reason=reason)

    def now(self) -> datetime:
        return datetime.now(UTC)


def build_degraded_context(reason: str, budget: ContextBudget | None = None) -> ContextPackage:
    """A valid but empty context package used when assembly itself fails."""
    return ContextPackage(
        degraded=True,
        degradation_note=f"context assembly failed: {reason}",
    )
