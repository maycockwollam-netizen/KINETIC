"""Configuration for KINETIC.

Settings are layered: environment variables > a config file > built-in defaults.
The settings object is a plain dependency that gets injected into the agent,
tools, and security policy — nothing reads global state at runtime.

Security-sensitive settings (network, sandbox mode, permission flags) never
silently fall back to a less-secure value: an invalid value raises
:class:`~errors.ConfigError` at construction time rather than being
coerced to a safe default.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from errors import ConfigError

if TYPE_CHECKING:
    from environment.config import EnvironmentConfig


class Settings(BaseSettings):
    """Runtime configuration for the coding agent."""

    model_config = SettingsConfigDict(
        env_prefix="KINETIC_",
        env_file=None,
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Model / SDK -------------------------------------------------------
    model: str = "claude-sonnet-4-5-20250929"
    fallback_model: str | None = None
    max_turns: int | None = 40
    max_budget_usd: float | None = None
    permission_mode: str = "default"
    # Optional LLM provider base URL (proxy / gateway / OpenAI-compatible
    # endpoint). When set, it is forwarded to the Claude Code subprocess via
    # the ``ANTHROPIC_BASE_URL`` env var. The API key is NEVER persisted here
    # or in a config file — it is read from ``ANTHROPIC_API_KEY`` (or supplied
    # per-task via the web console, held in memory only).
    llm_base_url: str | None = None

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

    # --- Phase 6: coding intelligence, verification & recovery ----------
    # Bounded repair: when final verification fails, analyze the failure and ask
    # the agent (via the SAME AgentSession.query safe path) to repair, then
    # re-verify. All budgets are bounded; off-by-default keeps Phase 5 behavior
    # when not requested.
    enable_repair: bool = False
    max_repair_attempts: int = 2
    max_verification_attempts: int = 3
    max_total_recovery_attempts: int = 4
    enable_stuck_detection: bool = True
    enable_regression_check: bool = True
    # Final review is off by default to preserve Phase 5 behavior; it is
    # automatically engaged when repair is enabled (see Orchestrator).
    enable_final_review: bool = False
    # Bounded repair context presented to the model (no raw log dump).
    repair_context_max_chars: int = 8000
    repair_context_max_test_failures: int = 8
    repair_context_max_changed_files: int = 20
    # Change-analysis heuristics.
    diff_max_changed_files: int = 200
    diff_broad_change_threshold: int = 50

    # --- Phase 7.3: web agent test console -----------------------------
    # A thin HTTP/SSE adapter over the existing backend. Default disabled so a
    # plain agent run never opens a listening socket; enabling is explicit.
    web_enabled: bool = False
    web_host: str = "127.0.0.1"
    web_port: int = 12000
    # Poll interval (seconds) used inside the SSE pump when no event is ready.
    web_event_poll_timeout: float = 1.0
    # Maximum events kept per task for late SSE clients (per-task ring).
    web_max_event_log: int = 512
    # How long (seconds) an interactive tool approval waits for a human before
    # auto-denying. Bounded so a forgotten approval never stalls the agent.
    web_approval_timeout: float = 300.0

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

    @field_validator("permission_mode", mode="after")
    @classmethod
    def _valid_permission_mode(cls, v: str) -> str:
        allowed = {"default", "acceptEdits", "plan", "bypassPermissions"}
        if v not in allowed:
            raise ValueError(f"permission_mode must be one of {allowed}, got {v!r}")
        return v

    @field_validator("llm_base_url", mode="after")
    @classmethod
    def _valid_llm_base_url(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        from urllib.parse import urlparse

        parsed = urlparse(v.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(
                "llm_base_url must be an http(s) URL, e.g. https://api.anthropic.com"
            )
        return v.strip()

    # --- Bounded numeric limits -------------------------------------------
    # Every untrusted or model-influenced quantity must have a bound so a
    # misconfiguration cannot create unbounded loops, negative retries, or
    # pathological resource consumption. Validators raise on invalid values
    # rather than silently coercing (fail early, fail loud).

    @field_validator("max_turns", mode="after")
    @classmethod
    def _bounded_turns(cls, v: int | None) -> int | None:
        if v is not None and v < 1:
            raise ValueError("max_turns must be >= 1")
        if v is not None and v > 200:
            raise ValueError("max_turns must be <= 200")
        return v

    @field_validator("max_budget_usd", mode="after")
    @classmethod
    def _bounded_budget(cls, v: float | None) -> float | None:
        if v is not None and v <= 0:
            raise ValueError("max_budget_usd must be positive")
        return v

    @field_validator(
        "default_command_timeout", "max_command_timeout", "execution_timeout",
        mode="after",
    )
    @classmethod
    def _positive_timeout(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("timeout values must be positive")
        return v

    @field_validator("max_command_timeout", mode="after")
    @classmethod
    def _max_timeout_cap(cls, v: float) -> float:
        if v > 86400:
            raise ValueError("max_command_timeout must be <= 86400 (24h)")
        return v

    @field_validator("execution_timeout", mode="after")
    @classmethod
    def _exec_timeout_cap(cls, v: float) -> float:
        if v > 86400:
            raise ValueError("execution_timeout must be <= 86400 (24h)")
        return v

    @field_validator(
        "max_step_attempts", "max_task_attempts", "max_replans",
        "max_plan_steps", "max_plan_dependencies",
        "max_repair_attempts", "max_verification_attempts",
        "max_total_recovery_attempts",
        mode="after",
    )
    @classmethod
    def _non_negative_int(cls, v: int) -> int:
        if v < 0:
            raise ValueError("retry/limit counts must be non-negative")
        return v

    @field_validator(
        "max_step_attempts", "max_task_attempts", "max_replans",
        "max_repair_attempts", "max_verification_attempts",
        "max_total_recovery_attempts",
        mode="after",
    )
    @classmethod
    def _bounded_retries(cls, v: int) -> int:
        if v > 20:
            raise ValueError("retry counts must be <= 20")
        return v

    @field_validator("max_plan_steps", mode="after")
    @classmethod
    def _bounded_plan_steps(cls, v: int) -> int:
        if v < 1:
            raise ValueError("max_plan_steps must be >= 1")
        if v > 100:
            raise ValueError("max_plan_steps must be <= 100")
        return v

    @field_validator("max_plan_dependencies", mode="after")
    @classmethod
    def _bounded_plan_deps(cls, v: int) -> int:
        if v < 1:
            raise ValueError("max_plan_dependencies must be >= 1")
        if v > 50:
            raise ValueError("max_plan_dependencies must be <= 50")
        return v

    @field_validator(
        "observation_max_stdout_chars", "observation_max_stderr_chars",
        "repair_context_max_chars", "repair_context_max_test_failures",
        "repair_context_max_changed_files",
        "context_max_characters", "context_max_project_metadata_chars",
        "context_max_memory_items", "context_max_recent_events",
        "context_max_task_history_items",
        "memory_candidate_limit", "memory_search_limit",
        "embedding_dimensions",
        "diff_max_changed_files", "diff_broad_change_threshold",
        mode="after",
    )
    @classmethod
    def _positive_limit(cls, v: int) -> int:
        if v < 1:
            raise ValueError("limits must be >= 1")
        return v

    @field_validator(
        "observation_max_stdout_chars", "observation_max_stderr_chars",
        "repair_context_max_chars", "context_max_characters",
        "context_max_project_metadata_chars",
        "diff_max_changed_files",
        mode="after",
    )
    @classmethod
    def _bounded_char_limit(cls, v: int) -> int:
        if v > 1_000_000:
            raise ValueError("character limits must be <= 1,000,000")
        return v

    @field_validator(
        "memory_candidate_limit", "memory_search_limit",
        "context_max_memory_items", "context_max_recent_events",
        "context_max_task_history_items",
        "repair_context_max_test_failures", "repair_context_max_changed_files",
        "diff_broad_change_threshold",
        mode="after",
    )
    @classmethod
    def _bounded_count_limit(cls, v: int) -> int:
        if v > 10_000:
            raise ValueError("count limits must be <= 10,000")
        return v

    @field_validator("memory_limit_mb", "disk_limit_mb", mode="after")
    @classmethod
    def _bounded_mb(cls, v: int | None) -> int | None:
        if v is not None and v < 1:
            raise ValueError("memory/disk limits must be >= 1 MB")
        return v

    @field_validator("cpu_limit", mode="after")
    @classmethod
    def _bounded_cpu(cls, v: float | None) -> float | None:
        if v is not None and v <= 0:
            raise ValueError("cpu_limit must be positive")
        return v

    @field_validator("process_limit", mode="after")
    @classmethod
    def _bounded_pids(cls, v: int | None) -> int | None:
        if v is not None and v < 1:
            raise ValueError("process_limit must be >= 1")
        return v

    @field_validator(
        "semantic_weight", "lexical_weight", "recency_weight", "importance_weight",
        mode="after",
    )
    @classmethod
    def _bounded_weight(cls, v: float) -> float:
        if v < 0:
            raise ValueError("ranking weights must be non-negative")
        return v

    @field_validator("embedding_dimensions", mode="after")
    @classmethod
    def _bounded_dim(cls, v: int) -> int:
        if v > 4096:
            raise ValueError("embedding_dimensions must be <= 4096")
        return v

    # --- Phase 7.3: web console validators ---------------------------------

    @field_validator("web_host", mode="after")
    @classmethod
    def _web_host_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("web_host must be a non-empty host")
        return v.strip()

    @field_validator("web_port", mode="after")
    @classmethod
    def _web_port_range(cls, v: int) -> int:
        if v < 1 or v > 65535:
            raise ValueError("web_port must be in 1..65535")
        return v

    @field_validator("web_event_poll_timeout", mode="after")
    @classmethod
    def _web_poll_timeout(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("web_event_poll_timeout must be positive")
        if v > 60:
            raise ValueError("web_event_poll_timeout must be <= 60")
        return v

    @field_validator("web_max_event_log", mode="after")
    @classmethod
    def _web_event_log_bounded(cls, v: int) -> int:
        if v < 1:
            raise ValueError("web_max_event_log must be >= 1")
        if v > 10_000:
            raise ValueError("web_max_event_log must be <= 10000")
        return v

    @field_validator("web_approval_timeout", mode="after")
    @classmethod
    def _web_approval_timeout_bounded(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("web_approval_timeout must be positive")
        if v > 3600:
            raise ValueError("web_approval_timeout must be <= 3600")
        return v

    @model_validator(mode="after")
    def _weights_not_all_zero(self) -> Settings:
        total = (
            self.semantic_weight + self.lexical_weight
            + self.recency_weight + self.importance_weight
        )
        if total <= 0:
            raise ValueError("ranking weights must sum to > 0")
        return self

    @model_validator(mode="after")
    def _timeout_ordering(self) -> Settings:
        if self.default_command_timeout > self.max_command_timeout:
            raise ValueError(
                "default_command_timeout must be <= max_command_timeout"
            )
        return self

    def environment_config(self) -> EnvironmentConfig:
        """Build an :class:`EnvironmentConfig` from these settings."""
        from environment.config import EnvironmentConfig
        from environment.envvars import EnvironmentVariablePolicy
        from environment.network import NetworkPolicy
        from environment.resources import ResourceLimits

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

    @classmethod
    def from_file(cls, path: str | Path) -> Settings:
        """Load settings from a JSON config file.

        Precedence: environment variables (KINETIC_*) override values in the
        file, which override built-in defaults. An invalid file raises
        :class:`~errors.ConfigError` — never a silent fallback.
        """
        p = Path(path).expanduser()
        if not p.exists():
            raise ConfigError(f"config file not found: {p}")
        try:
            raw: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"config file is not valid JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError("config file must contain a JSON object")
        # Build a BaseSettings from the file values first, then let env vars
        # override by re-validating through the normal constructor so the
        # env-prefix machinery applies. We do this by merging file values with
        # any env-provided overrides.
        try:
            # First construct from file (captures file values).
            from_file = cls(**raw)
            # Then overlay env overrides on top of file values for any env var
            # that is actually set, so precedence is env > file > default.
            import os

            overrides: dict[str, Any] = {}
            prefix = "KINETIC_"
            for key in cls.model_fields:
                env_name = prefix + key.upper()
                if env_name in os.environ:
                    overrides[key] = os.environ[env_name]
            if overrides:
                merged = {**from_file.model_dump(), **overrides}
                # Re-validate types (e.g. env strings -> int).
                return cls(**{k: v for k, v in merged.items()})
            return from_file
        except ConfigError:
            raise
        except Exception as exc:  # noqa: BLE001 - wrap pydantic errors as ConfigError
            raise ConfigError(f"invalid configuration: {exc}") from exc


def load_settings(config_file: str | Path | None = None) -> Settings:
    """Build a :class:`Settings` from a file (if given) + environment.

    This is the canonical entry point for CLI/production callers: it layers
    environment variables over an optional config file over the defaults, and
    wraps any validation failure in :class:`~errors.ConfigError`.
    """
    try:
        if config_file is not None:
            return Settings.from_file(config_file)
        return Settings()
    except ConfigError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(f"invalid configuration: {exc}") from exc
