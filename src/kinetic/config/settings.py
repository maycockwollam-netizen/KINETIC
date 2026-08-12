"""Configuration for KINETIC.

Settings are layered: environment variables > a config file > built-in defaults.
The settings object is a plain dependency that gets injected into the agent,
tools, and security policy — nothing reads global state at runtime.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, field_validator

if TYPE_CHECKING:
    from kinetic.environment.config import EnvironmentConfig


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

    # --- Phase 3: environment / sandbox -----------------------------------
    # Runtime type: "local" (host subprocess) or "docker" (isolated container).
    runtime_type: str = "local"
    # Strict sandbox mode (true isolation). Local dev may set False to allow
    # an honest non-isolating runtime; docker is always isolated.
    sandbox_mode: bool = False
    # Network policy: "deny" | "allow" | "restricted". Safe default: deny.
    network_policy: str = "deny"
    # Resource limits (None/0 = no limit requested).
    cpu_limit: float | None = None
    memory_limit_mb: int | None = None
    process_limit: int | None = None
    execution_timeout: float = 600.0
    disk_limit_mb: int | None = None
    # Environment variables allowed to be forwarded to a sandbox.
    allowed_env_vars: list[str] = Field(default_factory=list)
    # Docker image used by the docker runtime.
    docker_image: str = "python:3.11-slim"
    # Whether to allow environment exec via the permission policy.
    allow_environment_exec: bool = True
    allow_environment_network: bool = False
    allow_environment_admin: bool = False

    @field_validator("workspace_root", "session_root", "audit_log_path", mode="after")
    @classmethod
    def _abs(cls, v: Path) -> Path:
        return v.expanduser().resolve()

    @field_validator("network_policy", mode="after")
    @classmethod
    def _valid_network(cls, v: str) -> str:
        allowed = {"deny", "allow", "restricted"}
        if v not in allowed:
            raise ValueError(f"network_policy must be one of {allowed}, got {v!r}")
        return v

    @field_validator("runtime_type", mode="after")
    @classmethod
    def _valid_runtime(cls, v: str) -> str:
        allowed = {"local", "docker"}
        if v not in allowed:
            raise ValueError(f"runtime_type must be one of {allowed}, got {v!r}")
        return v

    def environment_config(self) -> EnvironmentConfig:
        """Build an :class:`EnvironmentConfig` from these settings."""
        from kinetic.environment.config import EnvironmentConfig
        from kinetic.environment.envvars import EnvironmentVariablePolicy
        from kinetic.environment.network import NetworkPolicy
        from kinetic.environment.resources import ResourceLimits

        net = NetworkPolicy(self.network_policy)
        resources = ResourceLimits(
            cpu=self.cpu_limit,
            memory_bytes=(self.memory_limit_mb * 1024 * 1024) if self.memory_limit_mb else None,
            process_count=self.process_limit,
            execution_timeout=self.execution_timeout,
            disk_bytes=(self.disk_limit_mb * 1024 * 1024) if self.disk_limit_mb else None,
        )
        env_vars = EnvironmentVariablePolicy(allowed=set(self.allowed_env_vars))
        return EnvironmentConfig(
            runtime_type=("docker" if self.runtime_type == "docker" else "local"),
            sandbox_mode=self.sandbox_mode or self.runtime_type == "docker",
            network=net,
            resources=resources,
            env_vars=env_vars,
            image=self.docker_image,
        )

    def ensure_directories(self) -> None:
        for p in (self.workspace_root, self.session_root):
            p.mkdir(parents=True, exist_ok=True)
        self.audit_log_path.parent.mkdir(parents=True, exist_ok=True)

    def writable_roots(self) -> list[Path]:
        roots = list(self.allowed_writable_roots) or [self.workspace_root]
        return [r.expanduser().resolve() for r in roots]
