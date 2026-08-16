# KINETIC

KINETIC is an autonomous coding agent built on the [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python).

It understands a software project, reads and modifies files, executes terminal commands, runs tests/builds, uses Git, installs dependencies, and streams its activity - all inside a controlled environment with explicit security and permission boundaries.

## Status

Phases 1–7.3+ are implemented:

- **Phase 1**: SDK adapter, agent session, event bus, tool registry, terminal + filesystem tools, permission policy, audit logging, configuration, CLI.
- **Phase 2**: project management, workspace management, Git tools, dependency detection + installation.
- **Phase 3**: sandboxed execution runtime (local + Docker) behind the Workspace interface, network policies, resource limits, environment-variable filtering.
- **Phase 4**: memory & context engine — selective hybrid-retrieval memory, deterministic embeddings, bounded context assembly.
- **Phase 5**: task planning & execution orchestration — state machine, planner, bounded recovery, checkpoints.
- **Phase 6**: coding intelligence — failure analysis, bounded repair, stuck detection, regression checking, deterministic final review.
- **Phase 7**: production hardening — configuration validation, structured logging, metrics, EventBus hardening, graceful shutdown, environment diagnostics, security audit, CLI hardening, packaging, documentation.
- **Phase 7.2**: repository/package namespace flattening — the `kinetic/` namespace was removed; source lives as top-level packages at the repo root.
- **Phase 7.3**: Web Agent Test Console — a thin HTTP/SSE adapter over the existing backend for observing real agent tasks in a browser.
- **Phase 7.3+**: replaced demo web surfaces with real backend endpoints (per-task LLM overrides, interactive approval, server-side agents/automations/files persistence).
- **Phase 7.4**: repository & developer infrastructure hardening — CI, MIT LICENSE, coverage measurement, type checking, repo docs.

The architecture is deliberately layered so that a future "AI Company" layer (CEO agent, departments, worker pools, etc.) can sit *above* the coding-agent core without rewriting it.

## Architecture

    Agent → AgentSession → AgentAdapter (SDK)
        → can_use_tool → ToolRegistry → PermissionPolicy → Tool
        → Environment → Runtime (Local | Docker)

There is exactly one safe execution path. Security is enforced at the runtime/tool layer (permission policy + environment boundary), never via prompt instructions. No second agent loop, no second tool registry, no alternate unrestricted execution path.

## Layout

    agent/           Claude Agent SDK adapter + agent sessions
    cli/             CLI entrypoint (run, task status/cancel/resume/inspect/failures/verify)
    config/          layered configuration (env vars > config file > defaults)
    context/         bounded context assembly engine
    dependencies/    dependency detection + installation
    environment/     sandboxed execution (local + Docker runtimes, diagnostics)
    events/          bounded async event bus + serializable event types
    intelligence/    failure analysis, repair, stuck detection, regression, review
    memory/          hybrid-retrieval memory (SQLite + deterministic embeddings)
    observability/   structured logging + metrics
    security/        tool permissions, audit logging, secret detection
    tasks/           task state machine, planner, executor, verifier, checkpoints
    tools/           tool registry + terminal / filesystem / git / project / memory tools
    web/             Web Agent Test Console (Phase 7.3) — HTTP/SSE adapter over the backend
    errors.py        shared error types
    lifecycle.py     graceful shutdown coordinator
    paths.py         shared path-safety utilities

The application source is laid out as top-level packages and modules at the
repository root (the former `kinetic/` namespace was flattened in Phase 7.2).

## Security boundaries

- **Permission policy**: every tool call is gated by `can_use_tool` before execution, regardless of prompt. Capabilities are declared per tool; the policy enforces them.
- **Environment boundary**: `Environment.exec` enforces `ENVIRONMENT_EXEC` itself (defense-in-depth) — direct callers cannot bypass the gate.
- **Workspace safety**: `safe_resolve` rejects path traversal, absolute-path escapes, and symlink escapes. Git operations are workspace-scoped.
- **Environment variables**: host env is never forwarded wholesale. Secret-named variables are dropped; secret-shaped values are redacted.
- **No host fallback**: Docker unavailable → `RuntimeUnavailableError` (never silent host execution). Local runtime fail-closes on unenforceable limits.
- **Audit log**: every permission decision and tool invocation is recorded (append-only JSONL).
- **Secret detection**: credential-like values are masked in logs, events, observations, and memory before persistence.

## Install (dev)

    uv sync --all-extras

## CLI

    kinetic run "inspect this repo and list its files"
    kinetic run "..." --dry-run              # no API key needed
    kinetic run "..." --runtime docker       # sandboxed execution
    kinetic task status <task_id>            # read checkpoint without model
    kinetic task inspect <task_id>           # full state + repair info
    kinetic task cancel <task_id>
    kinetic task resume <task_id>            # requires API key
    kinetic task verify <task_id>            # re-run verification

## Web Agent Test Console (Phase 7.3)

> **This is a test/control surface, NOT the final KINETIC product UI.** It exists
> to make the existing P1–P7.2 backend executable and observable through a
> browser so real agent tasks can be tested before Phase 8.

A thin HTTP/SSE adapter over the existing backend. It owns no execution path:
task state comes from `TaskManager`, execution from `Orchestrator`, events from
`EventBus`. Every tool call still flows through the single safe path
(AgentSession → PermissionPolicy → Environment).

    kinetic web --workspace . --port 12000

Then open `http://127.0.0.1:12000/` in a browser. Set `ANTHROPIC_API_KEY` to
run live agent tasks; without it the console starts but task creation fails with
a clear error (use `--allow-no-key` to start the server regardless).

### Web API

| Method | Path | Purpose |
|--------|------|---------|
| GET  | `/api/health` | status, version, backend readiness, key presence |
| POST | `/api/tasks` | create + start a task (`{"prompt": "..."}`) |
| GET  | `/api/tasks` | list task snapshots |
| GET  | `/api/tasks/{id}` | bounded task state snapshot |
| POST | `/api/tasks/{id}/start` | returns current state (tasks start on creation) |
| POST | `/api/tasks/{id}/resume` | resume a checkpointed task (requires key) |
| POST | `/api/tasks/{id}/cancel` | cancel via the existing cooperative mechanism |
| GET  | `/api/tasks/{id}/events` | SSE stream of live task events |
| GET  | `/api/tasks/{id}/outcome` | bounded outcome of a finished task |

The SSE stream replays recent history (bounded, secret-masked) then streams live
events from the task's `EventBus` until the task terminates. Reconnect is
supported via the `Last-Event-ID` header.

### Web security model

- The web layer **never** calls subprocess, mutates the filesystem, or executes
  in the `Environment` directly — it routes everything through the existing
  `Orchestrator`/`AgentSession`.
- Secret redaction reuses the existing `SecretDetector`: every response and
  event payload is masked before crossing the application boundary.
- API keys / environment variables are never sent to the browser (only a boolean
  `api_key_configured` flag).
- A pure-ASGI origin guard rejects cross-origin requests to the API surface
  (the console binds to localhost by default and has no auth layer).
- The per-task event log is bounded (`web_max_event_log`); the EventBus already
  caps + redacts payloads at publish time.

### Web configuration

    KINETIC_WEB_ENABLED=true
    KINETIC_WEB_HOST=127.0.0.1
    KINETIC_WEB_PORT=12000
    KINETIC_WEB_EVENT_POLL_TIMEOUT=1.0   # SSE poll interval (s)
    KINETIC_WEB_MAX_EVENT_LOG=512        # per-task event ring size

Every numeric setting has validators and sensible bounds (validated at
construction, like all KINETIC settings).

## Configuration

Settings are layered: environment variables (`KINETIC_*`) > config file > defaults. All numeric values are bounded and validated at construction (invalid values fail early).

    KINETIC_MAX_TURNS=20
    KINETIC_NETWORK_POLICY=deny
    KINETIC_RUNTIME_TYPE=docker

Config file (JSON):

    kinetic --config settings.json run "..."

Or programmatically:

    from config import load_settings
    settings = load_settings("settings.json")

## Observability

- **Structured logging**: `observability.configure()` installs a JSON formatter with correlation IDs (session/task/workspace) and secret redaction.
- **Metrics**: `MetricsCollector` tracks tasks, steps, verification, repair, environment, permissions. `snapshot()` returns a plain dict for export.
- **Audit log**: append-only JSONL of security-sensitive operations.
- **Event bus**: bounded async stream of structured events.

## Testing

    uv run pytest          # full suite (asyncio_mode=auto)
    uv run pytest --cov    # with coverage report
    uv run ruff check .    # lint
    uv run mypy <source dirs>   # type check (see CONTRIBUTING.md)

Live SDK integration tests run only when `ANTHROPIC_API_KEY` is set; otherwise they skip cleanly. Docker integration tests run only when the daemon is available.

Coverage is measured with `pytest-cov` (config in `[tool.coverage.*]`). Baseline: **82%** branch coverage; a conservative `fail_under = 75` guards against major regressions. Type checking uses `mypy` (config in `[tool.mypy]`) — see `CONTRIBUTING.md` for the baseline and strategy.

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs on every pull request and push to `main`: Ruff → pytest+coverage → wheel build, on Python 3.11 and 3.13. There is no deployment or publishing automation.

## Development setup

    git clone <repo>
    cd KINETIC
    uv sync --extra dev
    uv run pytest

See `CONTRIBUTING.md` for the full development workflow (tests, coverage, lint, type checking, wheel build, web console, PR expectations, security expectations). A `.env.example` documents the supported environment variables.

## Production considerations

- Use the Docker runtime for true isolation (local runtime is honest about not isolating).
- Set `KINETIC_NETWORK_POLICY=deny` by default; allow only when needed.
- Gate `GIT_WRITE`, `DEPENDENCY_INSTALL`, `MEMORY_WRITE` per session.
- Monitor the metrics snapshot for task failure rates and repair attempts.
- Use `environment.diagnostics.find_stale_containers()` to detect leaked containers.
- Checkpoints are atomic and fail-closed on corruption.
- The web console defaults to `127.0.0.1` (localhost). It has **no auth, no
  rate limiting, no TLS** — do not expose it on a public interface. A future
  hardening phase should add these (see `CONTRIBUTING.md` → Security).

## License

MIT. See [`LICENSE`](LICENSE).

## Project documentation

- [`README.md`](README.md) — overview, architecture, layout, security.
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — development workflow + expectations.
- [`CHANGELOG.md`](CHANGELOG.md) — per-phase change history.
- [`AGENTS.md`](AGENTS.md) — detailed phase-by-phase engineering memory.
- [`.env.example`](.env.example) — supported environment variables.
