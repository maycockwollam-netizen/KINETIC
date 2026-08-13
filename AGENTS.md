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

## Phase 3 scope (DONE)
Sandboxed execution runtime behind the existing Workspace abstraction.
Architecture: Agent -> ToolRegistry -> PermissionPolicy -> Tool -> Environment
-> Runtime -> Process. Security enforced at runtime/tool layer, never prompts.
- EnvironmentRuntime abstract interface (create/start/exec/stop/destroy/inspect);
  only concrete runtimes know Docker. LocalRuntime (host subprocess, dev mode,
  fail-closed on unenforceable limits) + DockerRuntime (real isolated sandbox:
  --network none for DENY, --cpus/--memory/--pids-limit, bind-mount only
  workspace + explicitly allowed mounts, filtered --env-file).
- Environment: state machine CREATING/READY/RUNNING/STOPPING/STOPPED/FAILED/
  DESTROYED with invalid-transition safety; owns workspace+runtime+config;
  emits events + audit. `env.provision()` -> create+start; `env.exec(spec)`;
  `env.stop()`/`env.destroy()`.
- NetworkPolicy(DENY/ALLOW/RESTRICTED, default DENY), ResourceLimits(cpu/memory/
  process_count/execution_timeout/disk), EnvironmentVariablePolicy(NO host env
  forwarding; secret-NAME vars dropped entirely even if allowlisted; secret-shaped
  VALUES redacted; explicit inject), ProcessSpec/ProcessResult(+ProcessState).
- New capabilities: ENVIRONMENT_CREATE/EXEC/NETWORK/ADMIN (gated by
  PermissionPolicy; network+admin off by default, create+exec on).
- New errors: SandboxError/EnvironmentStateError/RuntimeUnavailableError.
- New events: ENVIRONMENT_CREATED/STARTED/STOPPED/DESTROYED/FAILED,
  PROCESS_STARTED/FINISHED/CANCELLED/TIMEOUT, PERMISSION_DENIED.
- Settings: runtime_type/sandbox_mode/network_policy/cpu_limit/memory_limit_mb/
  process_limit/execution_timeout/disk_limit_mb/allowed_env_vars/docker_image/
  allow_environment_exec|network|admin. `settings.environment_config()`.
- SessionConfig: runtime_type/sandbox_mode/network_policy/allow_environment_*.
- TerminalTool: accepts optional `environment=`; routes through env.exec when
  provided, else low-level run_command (backwards compat). AgentSession builds +
  provisions the environment (fail-closed on provision) and tears it down in run().
- CLI: --runtime/--network-policy options.
- FAIL-CLOSED: local sandbox_mode=True + network DENY/RESTRICTED raises
  SandboxError (can't enforce); docker unavailable raises RuntimeUnavailableError
  (never falls back to host); unsupported limits raise rather than ignored.
185 tests pass (113 Phase1/2 + 72 new). 10 Docker integration tests pass when
daemon available (skipped otherwise). Ruff clean. Commit d93323c.

## Phase 3 gotchas
- LocalRuntime is honest: it provides NO fs/network isolation. With
  sandbox_mode=False (dev) it proceeds with a recorded warning; with
  sandbox_mode=True it fail-closes on network DENY/RESTRICTED + hard resource
  limits. Docker is always sandbox_mode=True.
- Docker daemon needs `sudo` in this sandbox: docker.py uses `_USE_SUDO=True`
  prefix. Change to False on hosts where the user is in the docker group.
- EnvVarPolicy drops secret-*named* vars entirely (not value-redacted); only
  non-secret-named vars with secret-shaped *values* get value-redacted.
- DockerRuntime.exec runs commands via `sh -c` inside the container at /workspace.
  Spec cwd is mapped workspace-relative to /workspace/<rel> (traversal rejected).
- RESTRICTED network with rules raises (no proxy infra); without rules falls
  back to DENY (--network none). disk_bytes always raises (daemon storage-opt
  unsupported) — fail closed.
- AgentSession.run() provisions environment before the model run and stops/
  destroys it after; provisioning failure returns a SessionResult with an error
  (no model call attempted).
- Lazy runtime import: environment.environment._select_runtime imports
  LocalRuntime/DockerRuntime lazily so the local path stays Docker-free.

## TODO Phase 4
Not started. Do NOT implement until Phase 3 is committed & reported.

## Phase 3 FINAL HARDENING (DONE — security/correctness/reliability only, no new features)
Verified & fixed only security/correctness/reliability/integration issues:
- **Permission boundary**: `Environment.exec` now enforces
  `ENVIRONMENT_EXEC` itself (via `_enforce_exec_permission`) before running —
  direct callers can no longer bypass the gate. Denials emit
  `PERMISSION_DENIED` + audit. Tool-level `can_use_tool` checks kept.
- **Network semantics**: docker `RESTRICTED` now FAILS CLOSED as unsupported
  (clear `SandboxError`) instead of silently becoming `DENY`. DENY=`--network
  none`, ALLOW=default bridge. Updated test accordingly.
- **Env vars**: `LocalRuntime.exec` now builds the subprocess env via
  `env_vars.filter(host_env)` + spec overrides — NEVER `env=None` (which had
  silently inherited the full host env). Matches docker runtime behavior.
- **Process lifecycle**: `run_command` rewritten with `start_new_session=True`
  + `os.killpg` (process-group kill) + a concurrent cancellation watcher.
  Timeout & cancellation are now PROMPT (was: only checked in a finally after
  communicate returned → long sleeps ran to completion). No orphan children
  keep pipes open. Verified elapsed <5s for 30s sleeps.
- **Container lifecycle**: containers now carry `--label kinetic.managed=true`,
  `kinetic.environment`, `kinetic.session_id` for ownership/leak tracking.
  `EnvironmentRuntime` base has `session_id` attr set by `Environment`.
  `DockerRuntime.destroy` surfaces real cleanup failures (rm fails + container
  still exists → `SandboxError`) instead of silently ignoring.
- **Session teardown**: `AgentSession.run` wraps the run in try/finally so the
  environment is ALWAYS stopped+destroyed — provisioning failure, model error,
  or interruption no longer leak containers. Fixed a PRE-EXISTING bug:
  `async with self.events.subscribe()` was wrong (`subscribe()` is a coroutine
  returning an object with `close()`, not an async context manager) → now
  `sub = await self.events.subscribe()` + `finally: sub.close()`. No prior test
  had exercised `session.run()` past provisioning, so the bug was latent.
- **Docker config**: hard-coded `_USE_SUDO=True` replaced with explicit env
  config: `KINETIC_DOCKER_SUDO` (1/true) and `KINETIC_DOCKER_CMD`. sudo is
  never invoked silently. conftest.py sets `KINETIC_DOCKER_SUDO=1` for tests
  in the root-owned-socket sandbox.
- **Audit/event**: exec denials audited + emitted; destroy failures surfaced as
  `ENVIRONMENT_FAILED` event + audit. No secret values logged (ProcessSpec
  logs env_keys only; audit detail logs command+cwd only).
195 tests pass (175 prior + 10 docker + 10 new hardening). Ruff clean.

## Conventions
- Package import root: `kinetic`. Source under `src/kinetic`.
- Use `pydantic` v2 for structured data/config. Use `anyio` for async (SDK uses anyio).
- Tools expose permission metadata; the registry collects them; agent gate checks via security policy.
- Run tests with: `uv run pytest` (asyncio_mode=auto).
- Lint with: `uv run ruff check`.
