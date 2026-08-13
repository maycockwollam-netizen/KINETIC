"""Context package data models."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ContextSection(BaseModel):
    """One rendered section of the assembled context."""

    name: str
    content: str
    char_count: int = 0
    truncated: bool = False


class OmissionRecord(BaseModel):
    """What was left out of the context and why."""

    section: str
    reason: str
    count: int = 0


class ContextPackage(BaseModel):
    """A bounded context bundle handed to the agent session."""

    sections: list[ContextSection] = Field(default_factory=list)
    omissions: list[OmissionRecord] = Field(default_factory=list)
    total_characters: int = 0
    memory_count: int = 0
    degraded: bool = False
    degradation_note: str | None = None

    def render(self) -> str:
        """Render the package as a single system-prompt-ready string."""
        parts: list[str] = []
        for section in self.sections:
            if not section.content:
                continue
            parts.append(f"## {section.name}\n{section.content}")
        rendered = "\n\n".join(parts)
        if self.degraded and self.degradation_note:
            rendered = f"NOTE: {self.degradation_note}\n\n" + rendered
        return rendered
