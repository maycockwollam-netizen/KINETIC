# KINETIC

KINETIC is an autonomous coding agent built on the [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python).

It understands a software project, reads and modifies files, executes terminal commands, runs tests/builds, uses Git, installs dependencies, and streams its activity - all inside a controlled environment with explicit security and permission boundaries.

## Status

Phases 1–7 are implemented:

- **Phase 1**: SDK adapter, agent session, event bus, tool registry, terminal + filesystem tools, permission policy, audit logging, configuration, CLI.
- **Phase 2**: project management, workspace management, Git tools, dependency detection + installation.
- **Phase 3**: sandboxed execution runtime (local + Docker) behind the Workspace interface, network policies, resource limits, environment-variable filtering.
- **Phase 4**: memory & context engine — selective hybrid-retrieval memory, deterministic embeddings, bounded context assembly.
- **Phase 5**: task planning & execution orchestration — state machine, planner, bounded recovery, checkpoints.
- **Phase 6**: coding intelligence — failure analysis, bounded repair, stuck detection, regression checking, deterministic final review.
- **Phase 7**: production hardening — configuration validation, structured logging, metrics, EventBus hardening, graceful shutdown, environment diagnostics, security audit, CLI hardening, packaging, documentation.

The architecture is deliberately layered so that a future "AI Company" layer (CEO agent, departments, worker pools, etc.) can sit *above* the coding-agent core without rewriting it.

## Architecture

    Agent → AgentSession → AgentAdapter (SDK)
        → can_use_tool → ToolRegistry → PermissionPolicy → Tool
        → Environment → Runtime (Local | Docker)

There is exactly one safe execution path. Security is enforced at the runtime/tool layer (permission policy + environment boundary), never via prompt instructions. No second agent loop, no second tool registry, no alternate unrestricted execution path.

## Layout

    kinetic/
        agent/          Claude Agent SDK adapter + agent sessions
        cli/            CLI entrypoint (run, task status/cancel/resume/inspect/failures/verify)
        config/         layered configuration (env vars > config file > defaults)
        context/        bounded context assembly engine
        dependencies/   dependency detection + installation
        environment/    sandboxed execution (local + Docker runtimes, diagnostics)
        events/         bounded async event bus + serializable event types
        intelligence/   failure analysis, repair, stuck detection, regression, review
        memory/         hybrid-retrieval memory (SQLite + deterministic embeddings)
        observability/  structured logging + metrics
        security/       tool permissions, audit logging, secret detection
        tasks/          task state machine, planner, executor, verifier, checkpoints
        tools/          tool registry + terminal / filesystem / git / project / memory tools
        lifecycle.py    graceful shutdown coordinator

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

## Configuration

Settings are layered: environment variables (`KINETIC_*`) > config file > defaults. All numeric values are bounded and validated at construction (invalid values fail early).

    KINETIC_MAX_TURNS=20
    KINETIC_NETWORK_POLICY=deny
    KINETIC_RUNTIME_TYPE=docker

Config file (JSON):

    kinetic --config settings.json run "..."

Or programmatically:

    from kinetic.config import load_settings
    settings = load_settings("settings.json")

## Observability

- **Structured logging**: `kinetic.observability.configure()` installs a JSON formatter with correlation IDs (session/task/workspace) and secret redaction.
- **Metrics**: `MetricsCollector` tracks tasks, steps, verification, repair, environment, permissions. `snapshot()` returns a plain dict for export.
- **Audit log**: append-only JSONL of security-sensitive operations.
- **Event bus**: bounded async stream of structured events.

## Testing

    uv run pytest          # full suite (asyncio_mode=auto)
    uv run ruff check      # lint

Live SDK integration tests run only when `ANTHROPIC_API_KEY` is set; otherwise they skip cleanly. Docker integration tests run only when the daemon is available.

## Development setup

    git clone <repo>
    cd KINETIC
    uv sync --all-extras
    uv run pytest

## Production considerations

- Use the Docker runtime for true isolation (local runtime is honest about not isolating).
- Set `KINETIC_NETWORK_POLICY=deny` by default; allow only when needed.
- Gate `GIT_WRITE`, `DEPENDENCY_INSTALL`, `MEMORY_WRITE` per session.
- Monitor the metrics snapshot for task failure rates and repair attempts.
- Use `kinetic.environment.diagnostics.find_stale_containers()` to detect leaked containers.
- Checkpoints are atomic and fail-closed on corruption.
