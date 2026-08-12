# KINETIC — Agent Memory

## Project overview
KINETIC is an autonomous coding agent built on the **Claude Agent SDK** (`claude-agent-sdk`).
The SDK wraps the Claude Code CLI over stdio. Build incrementally in 8 phases; **verify each phase before moving on**.

## Runtime facts (verified)
- Python 3.13.14, uv 0.12, node v22.23.2 available.
- `claude-agent-sdk` latest = 0.2.136. Installed via `uv sync`.
- **No `ANTHROPIC_API_KEY` is set** in this environment. Agent runs that actually
  call the Claude model will fail at the SDK transport layer unless a key is
  provided. Tests must NOT depend on a live model call — unit-test the adapter
  with a fake/fake transport, and integration-test live calls only when a key
  is present. Always gate live calls behind a key check.
- SDK public API (verified against installed 0.2.x):
  - `ClaudeSDKClient(options, transport)`: `connect()`, `query(prompt, session_id='default')`,
    `receive_response()` (async iter of messages), `interrupt()`, `disconnect()`,
    `set_permission_mode(mode)`, `set_model(model)`.
  - `query(prompt, options, transport)` -> async iter of messages (one-shot).
  - Custom tools: `@tool(name, description, input_schema)` decorator on async fn
    returning `{"content":[{"type":"text","text":...}]}`; assemble with
    `create_sdk_mcp_server(name, version, tools=[...])`; pass via
    `ClaudeAgentOptions(mcp_servers={"name": server}, allowed_tools=["mcp__name__toolname"])`.
  - Runtime permission gate: `ClaudeAgentOptions(can_use_tool=async (tool_name, tool_input, context) -> PermissionResultAllow|PermissionResultDeny)`.
  - Message types: `UserMessage`, `AssistantMessage` (has `.content` blocks: TextBlock/ToolUseBlock/ThinkingBlock),
    `SystemMessage` (`.subtype`, `.data`), `ResultMessage` (`.is_error`, `.result`, `.session_id`, `.duration_ms`, `.num_turns`),
    `StreamEvent`, `RateLimitEvent`.
  - `ClaudeAgentOptions` key fields: `model`, `cwd`, `permission_mode`, `system_prompt`,
    `allowed_tools`, `disallowed_tools`, `max_turns`, `max_budget_usd`, `mcp_servers`,
    `can_use_tool`, `hooks`, `env`, `add_dirs`, `system_prompt`.

## Architecture principles (from spec)
- Do NOT reinvent the agent loop — the SDK provides it. We build a thin **adapter**.
- Keep infrastructure (tools, security, env, storage) **separate** from agent reasoning.
- Security is enforced at the **runtime/tool layer**, NOT via prompt instructions.
  Use `can_use_tool` + tool permission metadata, not system-prompt pleading.
- No giant files; cohesive modules; interfaces around infra boundaries; DI; no circular deps.
- Every long-running op needs cancellation/timeout. Every external process needs lifecycle mgmt.
- Never silently swallow errors. No placeholder implementations pretending to work.
- Future "AI Company" layer (CEO, departments, payroll, investors, simulation) must sit
  ABOVE the coding-agent core — DO NOT implement it now, just leave room.

## Phase 2 scope (current: DONE)
Project manager (scan_project), workspace management (Workspace abstraction,
no Docker — Phase 3 adds sandbox), Git tools (status/diff/log/branch/show/
checkout/commit), dependency detection (pip/uv/poetry/npm/pnpm/yarn/cargo) +
installation (workspace-bound, permission-gated, audited, events, timeout/cancel).
New capabilities: GIT_READ/GIT_WRITE/DEPENDENCY_READ/DEPENDENCY_INSTALL/
WORKSPACE_READ/WORKSPACE_WRITE. New errors: WorkspaceError/ProjectError/
GitError/DependencyError. New events: PROJECT_SCANNED/WORKSPACE_CREATED/
WORKSPACE_DELETED/GIT_COMMAND_STARTED/GIT_COMMAND_FINISHED/DEPENDENCY_DETECTED/
DEPENDENCY_INSTALL_STARTED/DEPENDENCY_INSTALL_FINISHED. 16 tools registered.
113 tests pass, ruff clean.

## Phase 2 gotchas
- Circular import: dependencies.installer imports tools.terminal; tools.project
  imports dependencies.installer. Fixed by lazy-importing DependencyInstaller
  inside ProjectTools.__init__. Keep that lazy import.
- Git commits need an identity; inject `-c user.name=... -c user.email=...` for
  git_commit only (never modify global/repo config).
- safe_resolve blocks symlink escapes only when the symlink target is genuinely
  outside the workspace root — tests must place targets as siblings, not inside.
- DependencyError does NOT take a `workspace=` kwarg (uses ecosystem/command/exit_code).
- PermissionPolicy now gates GIT_WRITE and DEPENDENCY_INSTALL (off by default);
  SessionConfig has allow_git_write / allow_dependency_install flags.

## TODO Phase 3
Environment/sandbox/container runtime behind the Workspace interface; network
policies; resource limits. Do NOT start until Phase 2 is committed & reported.

## Conventions
- Package import root: `kinetic`. Source under `src/kinetic`.
- Use `pydantic` v2 for structured data/config. Use `anyio` for async (SDK uses anyio).
- Tools expose permission metadata; the registry collects them; agent gate checks via security policy.
- Run tests with: `uv run pytest` (asyncio_mode=auto).
- Lint with: `uv run ruff check`.
