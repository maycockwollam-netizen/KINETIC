"""Context budget: configurable limits on assembled context size.

When a budget is exceeded the engine ranks the available information, retains
the highest-value items, discards lower-value items, and records what was
omitted — it never silently exceeds the configured budget.
"""

from __future__ import annotations

from pydantic import BaseModel, field_validator


class ContextBudget(BaseModel):
    """Hard limits on what enters the model context."""

    max_memory_items: int = 8
    max_characters: int = 12000
    max_project_metadata_chars: int = 4000
    max_recent_events: int = 20
    max_task_history_items: int = 6

    @field_validator(
        "max_memory_items",
        "max_characters",
        "max_project_metadata_chars",
        "max_recent_events",
        "max_task_history_items",
    )
    @classmethod
    def _positive(cls, v: int) -> int:
        if v < 0:
            raise ValueError("budget limits must be non-negative")
        return v
