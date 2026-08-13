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

    # --- Phase 4: memory & context --------------------------------------
    # Root directory for the persistent memory database (SQLite).
    memory_db_path: Path = Field(default_factory=lambda: Path.home() / ".kinetic" / "memory.db")
    # Embedding provider type: "deterministic" (local, no network) or
    # "openai"/"anthropic"-style in future. Phase 4 ships deterministic only.
    embedding_provider: str = "deterministic"
    embedding_dimensions: int = 64
    # Hybrid retrieval weights (must sum > 0; none dominates unless one is ~0).
    semantic_weight: float = 0.4
    lexical_weight: float = 0.3
    recency_weight: float = 0.15
    importance_weight: float = 0.15
    # Bounded retrieval defaults.
    memory_candidate_limit: int = 50
    memory_search_limit: int = 10
    # Context budget defaults.
    context_max_memory_items: int = 8
    context_max_characters: int = 12000
    context_max_project_metadata_chars: int = 4000
    context_max_recent_events: int = 20
    context_max_task_history_items: int = 6
    # Permission flags for memory (read on by default; write/delete off).
    allow_memory_read: bool = True
    allow_memory_write: bool = False
    allow_memory_delete: bool = False

    # --- Phase 5: task orchestration -------------------------------------
    max_step_attempts: int = 2
    max_task_attempts: int = 3
    max_replans: int = 1
    max_plan_steps: int = 12
    max_plan_dependencies: int = 8
    verification_command: str | None = None
    observation_max_stdout_chars: int = 4000
    observation_max_stderr_chars: int = 2000
    checkpoint_dir: Path = Field(default_factory=lambda: Path.home() / ".kinetic" / "checkpoints")
    enable_checkpoints: bool = True
    enable_memory_capture: bool = False

    @field_validator(
        "workspace_root", "session_root", "audit_log_path", "memory_db_path",
        "checkpoint_dir", mode="after",
    )
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
        for p in (self.workspace_root, self.session_root, self.checkpoint_dir):
            p.mkdir(parents=True, exist_ok=True)
        self.audit_log_path.parent.mkdir(parents=True, exist_ok=True)
        self.memory_db_path.parent.mkdir(parents=True, exist_ok=True)

    def writable_roots(self) -> list[Path]:
        roots = list(self.allowed_writable_roots) or [self.workspace_root]
        return [r.expanduser().resolve() for r in roots]
