"""Configuration for KINETIC.

Settings are layered: environment variables > a config file > built-in defaults.
The settings object is a plain dependency that gets injected into the agent,
tools, and security policy — nothing reads global state at runtime.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field, field_validator


class Settings(BaseModel):
    """Runtime configuration for the coding agent."""

    # --- Model / SDK -------------------------------------------------------
    model: str = "claude-sonnet-4-5-20250929"
    fallback_model: str | None = None
    max_turns: int | None = 40
    max_budget_usd: float | None = None
    permission_mode: str = "default"

    # --- Workspace ---------------------------------------------------------
    workspace_root: Path = Field(default_factory=lambda: Path.home() / ".kinetic" / "workspaces")
    session_root: Path = Field(default_factory=lambda: Path.home() / ".kinetic" / "sessions")
    audit_log_path: Path = Field(default_factory=lambda: Path.home() / ".kinetic" / "audit.log")

    # --- Terminal tool -----------------------------------------------------
    default_command_timeout: float = 120.0
    max_command_timeout: float = 1800.0

    # --- Security ----------------------------------------------------------
    allow_network: bool = False
    allowed_writable_roots: list[Path] = Field(default_factory=list)

    @field_validator("workspace_root", "session_root", "audit_log_path", mode="after")
    @classmethod
    def _abs(cls, v: Path) -> Path:
        return v.expanduser().resolve()

    def ensure_directories(self) -> None:
        for p in (self.workspace_root, self.session_root):
            p.mkdir(parents=True, exist_ok=True)
        self.audit_log_path.parent.mkdir(parents=True, exist_ok=True)

    def writable_roots(self) -> list[Path]:
        roots = list(self.allowed_writable_roots) or [self.workspace_root]
        return [r.expanduser().resolve() for r in roots]
