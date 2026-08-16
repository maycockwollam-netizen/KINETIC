# Changelog

All notable changes to KINETIC are documented here. The project is built in
incremental phases; each phase is verified before the next begins. Dates are
omitted because the repository history is a shallow clone — refer to phase
reports in AGENTS.md for full technical detail.

The format is based on [Keep a Changelog](https://keepachangelog.com/) and this
project adheres to [Semantic Versioning](https://semver.org/) at a coarse level
(pre-1.0).

## [Unreleased] — P7.4

### Added
- GitHub Actions CI (`.github/workflows/ci.yml`): Ruff, pytest+coverage, wheel
  build on PRs and pushes to `main`, Python 3.11 + 3.13 matrix.
- MIT `LICENSE` file (the project declared MIT in `pyproject.toml` from the
  start; the file was missing).
- Test coverage measurement via `pytest-cov` with a conservative regression
  guard (`fail_under = 75`). Baseline: **82%** branch coverage.
- Static type checking via `mypy` (configured, baseline documented; not yet a
  CI hard-fail). Baseline: 77 errors, concentrated in the SDK lazy-import
  adapter and a few pydantic-dynamic surfaces.
- `.env.example` documenting the supported `KINETIC_*` / `ANTHROPIC_API_KEY`
  environment variables.
- `CONTRIBUTING.md` covering setup, tests, lint, type-check, build, web console,
  PR expectations, and security expectations.
- `CHANGELOG.md` (this file).

### Changed
- `pyproject.toml`: added `pytest-cov`, `mypy` to dev extras; added
  `[tool.coverage.*]` and `[tool.mypy.*]` configuration.
- `web/app.py`: typed the route list as `list[Route | Mount]` (fixes a real
  type error from appending a `Mount` to a `list[Route]`).
- `cli/main.py`: `_load_orchestrator_state` return type narrowed from
  `dict[str, object]` to `dict[str, Any]` (callers index into the status dict;
  `object` made every access a mypy error).

## Phase 7.3+ — replacing demo web surfaces with real ones

### Added
- Per-task LLM overrides (model / base URL / API key) and `llm_base_url`
  settings (proxy/gateway support forwarded to the SDK via `ANTHROPIC_BASE_URL`).
- Interactive approval flow: `PendingApprovalRegistry`, `PERMISSION_REQUESTED`
  / `PERMISSION_RESOLVED` events, bounded-timeout auto-deny. Opt-in, never
  relaxes the static permission policy.
- `store/` package: `JsonStore` (atomic JSON under `~/.kinetic`) +
  `AgentConfig` / `AutomationConfig` / `FileEntry` models + bounded file
  upload service. Web layer delegates to it (no filesystem mutation in `web/*`).
- Web routes: `/api/llm`, `/api/agents`, `/api/automations[/{id}[/run]]`,
  `/api/files`, `/api/tasks/{id}/approvals[/{req}/resolve]`. API key is
  in-memory only, never persisted, never returned (only `api_key_set`).

### Changed
- Front-end API adapter wired to real backend calls; fake seed data removed;
  `loadPersisted` keeps only conversations + UI settings client-side.

## Phase 7.3 — Web Agent Test Console

### Added
- `web/` package: `serialize.py` (bounded, secret-masked JSON chokepoint),
  `console.py` (`WebConsole` owns per-task real `AgentSession`+`Orchestrator`
  stacks run in background asyncio tasks), `app.py` (Starlette ASGI with
  pure-ASGI `_OriginGuardMiddleware` + SSE streaming with replay).
- `web/static/index.html`: production-quality single-file vanilla-JS/CSS UI
  (chat-centric, plan/tool/approval cards, command palette, markdown renderer,
  mobile off-canvas sidebar).
- Settings: `web_enabled` / `web_host` / `web_port` / `web_event_poll_timeout`
  / `web_max_event_log`. CLI: `kinetic web`.

## Phase 7.2 — repository/package namespace flattening

### Changed
- Removed the `kinetic/` package namespace; application source now lives as
  top-level packages/modules at the repo root. Imports migrated from
  `from kinetic.X import ...` to `from X import ...`. Product identifiers
  (MCP server name, Docker labels, CLI command, `~/.kinetic/`, env prefix)
  preserved intentionally. No behavior change.

## Phase 7 — production hardening & operational readiness

### Added
- `Settings` as `pydantic_settings.BaseSettings` with `env_prefix="KINETIC_"`;
  `from_file` / `load_settings` precedence (env > file > defaults); field
  validators that fail early on invalid values.
- Structured JSON logging (`observability/logging.py`) with secret-redacting
  formatter; `MetricsCollector` (counters/gauges/timers, bounded, thread-safe);
  EventBus hardening (bounded subscriber queues, payload truncation, secret
  masking); `ShutdownCoordinator` + signal handlers; environment diagnostics
  (`list_managed_containers`, `find_stale_containers`, label-gated destroy).
- Comprehensive security/stress/failure-containment/E2E test suites.

## Phase 6 — coding intelligence, verification & recovery

### Added
- `intelligence/` package: `FailureAnalyzer`, test-output parsers
  (pytest/npm/cargo/go/generic), `ChangeAnalyzer` (pure git diff text
  analysis), `StuckDetector`, `RegressionChecker`, `FinalReviewer`
  (deterministic, no subjective AI score), `RepairCoordinator` (bounded loop
  reusing `AgentSession.query` — no second agent loop).

## Phase 5 — task planning & execution orchestration

### Added
- `tasks/` package: `TaskState` machine, `TaskManager` (single writer),
  `Planner` (cycle detection, topological order, model-plan parsing),
  `RecoveryPolicy`, `Observation` (bounded, secret-masked), `Verifier`
  (routes through `Environment.exec`), `CheckpointStore` (atomic JSON),
  `ExecutionController` + `Orchestrator`. `AgentSession` refactored into
  `prepare()`/`query()`/`finish()`.

## Phase 4 — memory & context engine

### Added
- `memory/` package: `MemoryStore` (SQLite) + `DeterministicEmbeddingProvider`
  (hashing trick, no network), hybrid lexical+semantic retrieval, `Ranker`,
  `MemoryManager`. `context/` package: `ContextBudget` + `ContextEngine`
  (bounded, failure-safe assembly). Memory tools via the existing registry.

## Phase 3 — sandboxed execution runtime

### Added
- `EnvironmentRuntime` ABC + `LocalRuntime` (dev, fail-closed on unenforceable
  limits) + `DockerRuntime` (real isolated sandbox: `--network none`/bridge,
  `--cpus`/`--memory`/`--pids-limit`, bind-mount only workspace, filtered env).
  `Environment` state machine; `NetworkPolicy`, `ResourceLimits`,
  `EnvironmentVariablePolicy` (no host env forwarding, secret-* vars dropped).
  Final-hardening pass: `ENVIRONMENT_EXEC` enforced inside `Environment.exec`;
  process-group kill; container ownership labels; fail-closed network.

## Phase 2 — project, workspace, Git, dependencies

### Added
- `scan_project`, `Workspace` abstraction, Git tools (read/write), dependency
  detection (pip/uv/poetry/npm/pnpm/yarn/cargo) + workspace-bound,
  permission-gated, audited installation with timeout/cancel.

## Phase 1 — core agent infrastructure

### Added
- Claude Agent SDK adapter (`AgentAdapter`), `AgentSession`, `EventBus`,
  `ToolRegistry`, terminal + filesystem tools, `PermissionPolicy`,
  `AuditLog`, `Settings`, `kinetic` CLI.
