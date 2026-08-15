"""Plain data models for persisted web-console configuration.

These are small, user-authored records (agent definitions, automations,
uploaded-file metadata). They never hold secrets: API keys are kept in memory
only (``WebConsole``), never persisted here. ``pydantic`` v2 models give us
validation + JSON round-tripping for free.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


def _now() -> str:
    return datetime.now(UTC).isoformat()


class AgentConfig(BaseModel):
    """A user-defined agent preset (system prompt + model + tool list)."""

    id: str
    name: str = "Agent"
    description: str = ""
    icon: str = "🤖"
    model: str | None = None
    system_prompt: str = ""
    tools: list[str] = Field(default_factory=list)
    active: bool = False
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)


class AutomationConfig(BaseModel):
    """A user-defined automation (cron + prompt + agent + honest run history).

    ``last_run_at`` / ``history`` are populated only from actual runs initiated
    through the console — never faked. ``next_run_at`` is computed from the cron
    expression when a scheduler runs; with no scheduler it stays ``None``.
    """

    id: str
    name: str = "Automation"
    cron: str = ""
    schedule_label: str = ""
    prompt: str = ""
    agent_id: str | None = None
    enabled: bool = True
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)
    last_run_at: str | None = None
    last_run_status: str | None = None
    last_run_task_id: str | None = None
    next_run_at: str | None = None
    history: list[dict[str, Any]] = Field(default_factory=list)


class FileEntry(BaseModel):
    """Metadata for a file uploaded via the web console into the workspace."""

    id: str
    name: str
    size: int = 0
    content_type: str = "application/octet-stream"
    path: str
    created_at: str = Field(default_factory=_now)


__all__ = ["AgentConfig", "AutomationConfig", "FileEntry"]
