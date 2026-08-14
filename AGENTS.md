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

## Phase 4 scope (DONE)
Memory & Context Engine. Selective hybrid-retrieval memory system — NOT a raw
conversation dump. Only validated facts become persistent memory.
- `memory/`: models (MemoryRecord, MemoryScope EPHEMERAL/TASK/
  PROJECT/AGENT, compute_content_hash), embeddings (EmbeddingProvider ABC +
  DeterministicEmbeddingProvider — hashing-trick, local, no network/API key),
  metadata (MemoryFilter with project isolation + SecretDetector), store
  (MemoryStore ABC + SQLiteStore — metadata+content+embeddings in one SQLite
  file; lexical=tokenized LIKE scan, semantic=in-process cosine over stored
  embeddings, both bounded by candidate limit; invalidate bumps version),
  retrieval (Retriever gathers lexical+semantic candidates, propagates store
  errors so callers degrade — never fabricates), ranking (Ranker combines
  semantic*lexical*recency*importance, weights normalized to sum=1, confidence
  modulates to resolve conflicts), lifecycle (MemoryManager: create/update/
  delete/retrieve/search/invalidate/scope/consolidate; secret-filtered;
  dedup via content_hash; invalidation-preferred over delete; failure-safe),
  providers/ (extension point).
- `context/`: ContextBudget (max memory items/chars/project
  metadata/recent events/task history), ContextEngine (assembles bounded
  package: task + relevant memories + project metadata + workspace state +
  recent events + task history; ranks/trims to budget; records omissions;
  failure-safe degradation — memory backend failure yields empty memories +
  note, never fabricates), ContextPackage.render().
- Memory tools (memory_search/get/create/update/delete) via EXISTING
  ToolRegistry; MEMORY_READ/WRITE/DELETE capabilities; write+delete off by
  default. Project-scoped memory enforces boundaries (WHERE clause, not
  after-the-fact pruning).
- New events: MEMORY_CREATED/UPDATED/DELETED/INVALIDATED/RETRIEVED/
  CONSOLIDATED, CONTEXT_BUILT, CONTEXT_BUDGET_EXCEEDED. New errors:
  MemoryError/MemorySecurityError/ContextError. Settings: memory_db_path,
  embedding dimensions, hybrid weights, candidate/search limits, context
  budget fields, allow_memory_read/write/delete. SessionConfig same flags.
- AgentSession integration: builds MemoryManager + ContextEngine; run()
  assembles a bounded context (failure-safe) and merges into system prompt
  BEFORE the model run. Assistant responses are NOT auto-persisted (only
  validated, explicit memory_create persists).
270 tests pass (183 prior + 87 new Phase 4). Ruff clean.

## Phase 4 gotchas
- Deterministic embeddings use a hashing trick (blake2b bucket assignment) so
  identical text -> identical vector; similar texts share buckets -> cosine>0.
  Tests must use queries that share tokens with stored memories.
- Project isolation is a WHERE clause (`project_id = ?`), enforced at the store
  layer — a bare `list()` excludes invalidated rows even with no filter.
- Retriever PROPAGATES store errors (does NOT swallow) so the manager raises
  MemoryError and ContextEngine can degrade gracefully. It only swallows
  per-signal "no candidates" (e.g. empty query).
- `MemoryManager._fail` emits `AGENT_ERROR` (no MEMORY_FAILED event type per
  spec) + audit; memory failures never corrupt task execution.
- Secret detection is conservative (false positives OK = reject; mask snippets
  in audit so raw secrets never land in logs).
- Confidence modulates final score via `base*(0.5+0.5*confidence)` — fresh
  high-confidence facts outrank stale low-confidence ones without zeroing a
  genuinely relevant memory.
- ContextEngine fetches `max_memory_items*2` candidates then caps, so omissions
  are recorded for relevant-but-cut memories.

## TODO Phase 5
Task planning & execution orchestration — DONE (see below).

## Phase 5 scope (DONE)
Task planning & execution orchestration. Turns KINETIC from an agent
infrastructure layer into a complete autonomous coding-task execution system.
The Claude Agent SDK remains responsible for model interaction; KINETIC owns
task lifecycle, planning state, execution state, observation, verification,
bounded recovery, checkpoints. NO second agent loop / ToolRegistry /
permission system / direct subprocess outside Environment.
- `tasks/`: states (TaskState machine: CREATED→CONTEXT_READY→
  PLANNING→PLAN_READY→EXECUTING→VERIFYING→COMPLETED/FAILED/CANCELLED +
  RECOVERING; invalid transitions raise TaskStateError), models (Task/Plan/
  PlanStep/StepStatus/TaskFailure — small, no raw model output), manager
  (TaskManager = authoritative state machine, only writer; cancel distinct
  from failure; never reinterprets cancellation as failure), planner
  (validate_plan: unique IDs, unknown/self deps, cycle detection (DFS),
  bounded size; topological_order (deterministic, tie-broken by plan order);
  next_executable_step; parse_model_plan — model JSON parsed+validated, never
  executed blindly), policies (FailureClass, RecoveryPolicy with RetryLimits,
  classify_failure, VerificationOutcome PASS/FAIL/INCONCLUSIVE), observer
  (Observation bounded: stdout/stderr truncated + SecretDetector-masked; no
  chain-of-thought), verifier (Verifier runs commands via Environment.exec →
  still through permission gate; command_for_manifest from Phase 2
  ProjectManifest — pytest/npm/cargo/go/make; INCONCLUSIVE when no command,
  never fakes success), recovery (RecoveryCoordinator: classify→decide→audit;
  PermissionDenied fails immediately unless state changed; INVALID_PLAN re-plan
  once; bounded retry; no unlimited retry), checkpoints (CheckpointStore JSON
  atomic write; build/restore; fail-closed on corruption/terminal-task resume),
  executor (ExecutionController: single safe path via StepRunner/PlanRunner
  Protocols — AgentSession.query → adapter → can_use_tool → registry → policy
  → environment; topo order; verify per-step + final; recover/re-plan/fail;
  no subprocess/filesystem direct access), orchestrator (Orchestrator wires
  AgentSession + tasks; AgentStepRunner/AgentPlanRunner back real session;
  run_task/resume_task provision+finish session in try/finally; catches
  OrchestrationError/PermissionDeniedError → bounded FAILED outcome, never
  fabricated success).
- New errors: TaskError/TaskStateError/PlanError/VerificationError/
  CheckpointError/OrchestrationError.
- New events: task_created/state_changed/planning_started/plan_created/
  step_started/completed/failed/verification_started/completed/recovery_started/
  completed/replanned/cancelled/failed/checkpoint_created (TASK_COMPLETED/FAILED
  already existed from Phase 1 — not redefined).
- Settings: max_step_attempts/max_task_attempts/max_replans/max_plan_steps/
  max_plan_dependencies/verification_command/observation_*_chars/checkpoint_dir/
  enable_checkpoints/enable_memory_capture.
- AgentSession refactor: split run() into prepare()/query()/finish() so the
  execution controller provisions once and issues many query calls (one per
  plan step). Adapter connect/disconnect duck-types both connect() and
  __aenter__/__aexit__ (legacy test adapters keep working). run() unchanged
  behavior, just factored.
- CLI: `kinetic task status/cancel/resume <id>` (status/cancel read checkpoint
  without a live model; resume requires API key).
- Security: orchestration adds NO new tool path. Verifier routes through
  Environment.exec (ENVIRONMENT_EXEC enforced at env boundary). Controller has
  no run_command/subprocess import. PermissionDeniedError propagates as FAILED,
  never fabricated success. Observations secret-masked before persistence/audit.
  Memory is NOT auto-persisted (enable_memory_capture flag reserved, off).
388 tests pass (270 prior + 118 new Phase 5). Ruff clean. No secrets committed.

## Phase 5 gotchas
- AgentSession.run() was refactored into prepare()/query()/finish(). Tests that
  inject a fake adapter via `session._adapter = FakeAdapter()` (Phase 3/4) work
  because prepare() duck-types connect/__aenter__ and finish() duck-types
  disconnect/__aexit__. Do NOT assume the adapter has connect().
- TaskState.EXECUTING cannot go directly to COMPLETED — it must pass through
  VERIFYING (the executor transitions EXECUTING→VERIFYING before final
  verification). The state machine enforces this.
- CANCELLED and FAILED are distinct terminals; neither transitions to the other.
  mark_failed on an already-cancelled task records the failure info WITHOUT
  changing state (cancellation is never reinterpreted as failure).
- restore_checkpoint REFUSES terminal tasks (nothing to resume) and validates
  plan.task_id matches task.id. CLI task status/cancel load raw JSON instead
  so terminal tasks remain inspectable.
- FakeStepRunner reuses the last scripted outcome for attempts beyond the
  scripted list (so a permanently-failing step keeps failing across retries).
- The SDK emits a CanUseToolShadowedWarning because allowed_tools blanket-
  auto-approves registered MCP tools (pre-existing Phase 1 behavior). The
  can_use_tool gate still runs for tools not blanket-allowed; this is filtered
  in pyproject pytest config, not a Phase 5 concern.
- Orchestrator.run_task/resume_task catch (OrchestrationError,
  PermissionDeniedError) and return a bounded FAILED ExecutionOutcome rather
  than raising — so callers see a structured result, never a fabricated success.

## TODO Phase 6
Not started. Do NOT implement until Phase 5 is committed & reported.

## Phase 6 scope (DONE)
Coding Intelligence, Verification & Recovery. Makes KINETIC substantially
better at completing real coding tasks *after* the Phase 5 orchestration layer
has planned and executed work. Flow: Task → Plan → Execute → Observe → Verify →
Failure analysis → Locate cause → Repair → Retest → Regression verification →
Diff/quality review → Complete OR bounded failure.
- `intelligence/` package:
  - `models.py`: FailureAnalysis (bounded+secret-masked), TestFailureInfo,
    ChangeRecord, ChangeAnalysis, RepairAttempt, RepairState, StuckSignal,
    RegressionResult, ReviewCheck, ReviewResult.
  - `parsers.py`: pure regex parsers for pytest/npm(jest)/cargo/go/generic;
    graceful fallback; `analyze_test_output` dispatcher by command heuristic.
  - `analyzer.py`: FailureAnalyzer → classify_failure + parsers + secret
    masking + bounded stdout/stderr; `failure_signature` (excludes volatile
    output) for stuck detection; `analysis_from_dict` round-trip.
  - `diff.py`: ChangeAnalyzer over fetched git status/diff TEXT (pure — no
    subprocess); GitInspector Protocol; GitToolsInspector delegates to existing
    GitTools (permission-gated GIT_READ). Detects added/deleted/modified,
    generated files, outside-workspace, broad changes, empty.
  - `stuck.py`: StuckDetector (identical failure signature repeated → stuck;
    bounded failure, never loops).
  - `regression.py`: RegressionChecker runs broader verification via existing
    Verifier (Environment.exec); FAIL after repair = regression.
  - `review.py`: FinalReviewer deterministic checks (workspace valid,
    verification passed, no unresolved failure, diff coherent, no outside-
    workspace changes, bounded scope); generated-files advisory only; NO
    subjective AI quality score.
  - `repair.py`: RepairCoordinator (bounded loop: preserve original failure →
    analyze → build bounded repair context → ask agent via RepairRunner Protocol
    (AgentRepairRunner = SAME AgentSession.query safe path — NO second agent
    loop/ToolRegistry/permission system) → re-verify via Verifier → stuck
    detect → regression check; bounded by max_repair_attempts +
    max_verification_attempts). RepairContextBuilder (bounded, secret-masked,
    includes previous attempts so model doesn't repeat).
- New FailureClass members: LINT_FAILURE, DEPENDENCY_FAILURE, COMMAND_FAILURE,
  CANCELLATION, VERIFICATION_INCONCLUSIVE (existing unchanged; CANCELLATION
  added to NON_RETRYABLE). New errors: IntelligenceError/RepairError. New events:
  FAILURE_ANALYZED, REPAIR_STARTED/COMPLETED/FAILED, VERIFICATION_RETRY,
  STUCK_DETECTED, REGRESSION_DETECTED, FINAL_REVIEW_STARTED/COMPLETED.
- Settings: enable_repair (off by default — preserves Phase 5 behavior),
  max_repair_attempts, max_verification_attempts, max_total_recovery_attempts,
  enable_stuck/regression/final_review, repair_context_*, diff_*.
- ExecutionController: optional repair_coordinator/change_analyzer/final_reviewer
  (None → Phase 5 behavior). On final-verify FAIL → _attempt_repair; on repair
  success → re-verify + final review. ExecutionOutcome gains `repair`/`review`.
- Orchestrator: _build_intelligence wires Phase 6 from settings; AgentRepairRunner
  wraps AgentSession.query (same safe path); GitToolsInspector wraps GitTools.
  enable_final_review auto-engaged when repair enabled.
- Checkpoints: version 2; build_checkpoint takes repair_state; restore_repair_state
  fail-closed on corrupt/typed fields (Phase 1-5 v1 checkpoints still restore).
- CLI: `kinetic task inspect <id>` (full state + repair state),
  `kinetic task failures <id>` (failure analysis), `kinetic task verify <id>`
  (re-run verification, requires API key — provisions env, no model).
- SECURITY: intelligence layer is pure analysis + orchestration — NO subprocess,
  NO os.system, NO shell=True, NO run_command import, NO direct GitTools
  instantiation, NO direct filesystem mutation. All execution goes through
  existing Environment.exec / GitTools / permission policy. Repair reuses
  AgentSession.query (same SDK loop). All retry/repair loops bounded; stuck
  tasks terminate deterministically; secrets masked before persist/audit/model.
485 tests pass (388 prior + 97 new Phase 6). Ruff clean. No secrets committed.

## Phase 6 gotchas
- enable_repair/enable_final_review default False → Phase 5 behavior unchanged
  for existing tests/orchestrator. Final review auto-engages ONLY when repair
  enabled (to avoid failing inspect-only tasks on empty diff).
- Step-level verification runs on the FINAL step (`_is_final_step`), so a failing
  verification command causes step failure (Phase 5 behavior) BEFORE the final-
  verify repair path. To test repair, inject a scripted verifier where step-level
  verify passes but final verify fails, AND inject it on BOTH controller.verifier
  AND repair_coordinator._verifier (they are separate references).
- FailureAnalyzer._refine_class upgrades TOOL_FAILURE→BUILD/LINT/TEST based on
  command, and detects CANCELLATION/TIMEOUT/PERMISSION/ENV/DEPENDENCY/INCONCLUSIVE
  from output text. classify_failure is the Phase 5 base; _refine_class layers on.
- RegressionChecker treats broader-verify FAIL after repair as regression
  (INCONCLUSIVE is NOT a regression — no command/cannot run). before_passed is
  recorded but the decisive signal is after_failed.
- Circular import: executor.py imports intelligence modules under TYPE_CHECKING
  only (string annotations) + lazy import of ChangeAnalysis inside _final_review.
  intelligence.models imports tasks.policies (not the tasks package), so
  importing intelligence does NOT trigger tasks.__init__.
- frozen TestFailureInfo file/line set via object.__setattr__ in npm/go parsers
  (attaching FAIL-file/package to already-created failure records).
- Parser line extraction: pytest assertion location is on a SEPARATE line
  (`file.py:NN:`), so _find_assertion_location scans the full output for it.

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
- Package import root: repository root (top-level packages). Source under the
  root-level domain directories (`agent/`, `cli/`, `config/`, ...). The former
  `kinetic/` namespace was flattened in Phase 7.2 — imports are now
  `from agent import ...`, `from tasks import ...`, etc.
- Use `pydantic` v2 for structured data/config. Use `anyio` for async (SDK uses anyio).
- Tools expose permission metadata; the registry collects them; agent gate checks via security policy.
- Run tests with: `uv run pytest` (asyncio_mode=auto).
- Lint with: `uv run ruff check`.

## Phase 7 scope (DONE — production hardening & operational readiness)
The final core-hardening phase. NOT a feature-expansion phase. Makes KINETIC
operationally reliable, observable, configurable, testable, and ready for
real-world use. All existing architecture and security boundaries preserved.

### Configuration hardening (`config/settings.py`)
- `Settings` is now a `pydantic_settings.BaseSettings` with `env_prefix="KINETIC_"`
  so environment variables override defaults (e.g. `KINETIC_MAX_TURNS=10`).
- `Settings.from_file(path)` + `load_settings(config_file)` layer env > file >
  defaults; invalid files raise `ConfigError` (never silent fallback).
- Comprehensive field validators: non-negative retries (bounded ≤20), positive
  timeouts (bounded ≤24h), bounded limits (char ≤1M, count ≤10k), bounded
  weights (sum>0), timeout ordering (default≤max), valid enums (network_policy,
  runtime_type, permission_mode). Invalid values fail EARLY at construction.
- Tests: `tests/test_phase7_config.py` (precedence + invalid config).

### Structured logging (`observability/logging.py`)
- Centralized JSON structured logging: `configure()`, `get_logger()`,
  `bind_context()` (session/task/workspace/environment correlation IDs).
- `_SecretRedactingJsonFormatter` masks credential-like values in messages +
  extras before emission. Reuses the memory `SecretDetector` for consistency.
- DISTINCT from `AuditLog`: logging = operational diagnostics; audit = security
  accountability. Audit info is never duplicated into normal logs.
- Tests: `tests/test_phase7_logging.py` (secret fixtures never appear).

### Metrics (`observability/metrics.py`)
- `MetricsCollector` with Counters, Gauges, Timers — bounded (max_metrics cap
  drops new names; timer samples bounded). Thread-safe. Snapshotable.
- Standard metric names: tasks started/completed/failed/cancelled, task
  duration, steps executed, verification attempts, repair attempts, recovery
  failures, env created/destroyed, tool failures, permission denials.
- Wired into: TaskManager (task lifecycle), Environment (create/destroy,
  permission denials), ExecutionController (steps, verification, repair).
  AgentSession + Orchestrator propagate the collector.
- Tests: `tests/test_phase7_metrics.py`.

### EventBus hardening (`events/bus.py`)
- Bounded subscriber queues (maxsize); slow consumers drop oldest (publisher
  never blocks). Subscriber failure (closed loop) → subscriber dropped, never
  crashes producer.
- Bounded payloads: oversized `data` truncated with marker; non-JSON-serializable
  values replaced with repr; secrets masked before publication.
- Tests: `tests/test_phase7_events.py` (stress, slow/cancelled/failed consumer,
  concurrent publishers, payload safety).

### Graceful shutdown (`lifecycle.py`)
- `ShutdownCoordinator`: registers named cleanup callbacks (async/sync), runs
  them LIFO within a bounded timeout. Timed-out callbacks abandoned; failed
  callbacks recorded but don't stop others. Cancellation distinct from failure.
- `install_signal_handlers()`: wires SIGINT/SIGTERM to a CancellationToken
  (main thread only; never an import side effect).
- Tests: `tests/test_phase7_lifecycle.py`.

### Environment diagnostics (`environment/diagnostics.py`)
- `list_managed_containers()`, `find_stale_containers()`: read-only inspection
  of `kinetic.managed=true` labeled containers. NEVER destroys automatically.
- `destroy_container(id)`: explicit, label-gated (refuses non-managed).
- Tests: `tests/test_phase7_diagnostics.py` (fakes; Docker unavailable).

### Security audit
- Grep-verified: no `os.system`, no `shell=True` in subprocess, no `eval()`,
  no `exec()` builtin, no `pickle.load`, no unsafe `yaml.load`.
- Single execution path confirmed: Agent → AgentSession → PermissionPolicy →
  ToolRegistry → Tool → Environment → Runtime. No alternate unrestricted path.
- Tests: `tests/test_phase7_security.py` (pattern grep + permission + path safety).

### Additional hardening
- Resource limits: every untrusted quantity bounded (`test_phase7_resource_limits.py`).
- Git/workspace safety: symlink/traversal/absolute-path regression tests
  (`test_phase7_git_safety.py`).
- Memory/context: corrupt SQLite, bounded retrieval, deterministic embeddings
  (`test_phase7_memory.py`).
- Task state/checkpoint: exhaustive transition matrix, corrupt-checkpoint
  fail-closed, version/ID integrity (`test_phase7_task_state.py`).
- Failure containment: subscriber/task/tool/env failure isolation
  (`test_phase7_failure_containment.py`).
- CLI: exit codes, no tracebacks for expected errors, dry-run without key
  (`test_phase7_cli.py`).
- End-to-end fake integration: full scenario (workspace → project → task →
  plan → execute → verify → failure → repair → re-verify → review → commit →
  checkpoint → complete → metrics/events/audit/no-secrets/working-tree)
  (`test_phase7_e2e.py`).
- Stress: EventBus, memory, context, state transitions, large output, checkpoints
  (`test_phase7_stress.py`).
- Live SDK integration: optional test gated on `ANTHROPIC_API_KEY`
  (`test_phase7_live_sdk.py`); skips cleanly without key.
- Packaging: wheel builds, installs in clean venv, CLI entrypoint works.

725 tests pass (485 prior + 240 new Phase 7). 16 skipped (12 docker + 4 live
SDK without key). Ruff clean. No secrets committed.

## Phase 7.2 scope (DONE — repository/package namespace flattening)
A surgical layout migration only — NO behavior change. The `kinetic/` package
namespace was removed; application source now lives as top-level packages and
modules directly at the repository root.
- Moved `kinetic/<domain>/` → `<domain>/` for all 14 domains (agent, cli, config,
  context, dependencies, environment, events, intelligence, memory,
  observability, project, security, tasks, tools) + `kinetic/{errors,lifecycle,
  paths}.py` → root + `kinetic/__init__.py` → root `__init__.py`.
- Imports migrated: `from kinetic.X import ...` → `from X import ...` across all
  source + tests; dotted-string refs (unittest.patch, importlib.import_module,
  __import__) updated too.
- Packaging (`pyproject.toml`): Hatchling `only-include` lists the 14 packages +
  the 3 root-level modules; entrypoint `kinetic = "cli.main:main"`. The wheel
  ships exactly the application source (no tests/, no .venv/, no build/).
- Product identifiers PRESERVED (NOT imports, unchanged): MCP server name
  `"kinetic"`, Docker ownership labels (`kinetic.managed`, `kinetic.environment`,
  `kinetic.session_id`), CLI command `kinetic`, `~/.kinetic/` data dir, env
  prefix `KINETIC_`. These are product names, not the removed Python namespace.
- `test_phase7_security.py` SRC path (was `src/kinetic`) now scans the 14
  root-level source dirs + root modules — same security-audit coverage, new
  location.
- No `kinetic/` or `src/` directory remains. No compatibility wrappers left.
725 tests pass, 16 skipped. Ruff clean. Wheel builds + installs in clean venv +
editable; CLI `kinetic` + all task subcommands work. No behavior change.

## Phase 7.2 gotchas
- The `kinetic` package namespace is GONE. Do NOT reintroduce `from kinetic...`
  imports — they will fail at runtime in the installed wheel (only root-level
  packages exist). New code imports `from agent`, `from tasks`, etc.
- `kinetic` as a STRING (MCP server name, Docker labels, CLI command, data dir,
  env prefix) is a product identifier and is INTENTIONALLY kept — grep must
  distinguish product-name strings from Python import paths.
- Hatchling `only-include` (not `packages`) is required because the layout mixes
  package directories with loose root-level `.py` modules; `packages` alone would
  omit `errors.py`/`lifecycle.py`/`paths.py` from the wheel.
- `test_phase7_security.py` no longer has a single `SRC` dir; it scans an
  explicit list of source roots so the unsafe-pattern grep still covers all
  application source without scanning tests/.venv/build.

## Phase 7.3 scope (DONE — web agent test console)
A thin HTTP/SSE adapter over the existing P1–P7.2 backend. NOT the final product
UI — a test/control surface so real agent tasks can be observed in a browser
before Phase 8. The web layer is an adapter/interface, NOT a new execution system.
- `web/` package:
  - `serialize.py`: single chokepoint that turns domain objects into bounded,
    JSON-serializable dicts + masks credential-like content via the existing
    `SecretDetector`. Identifier keys (task_id/session_id/plan_id/step_id) are
    exempt from masking (they are system UUIDs the browser needs, not secrets);
    free-form content is always masked + capped.
  - `console.py`: `WebConsole` owns per-task `TaskRun`s. For each task it builds
    the REAL `AgentSession`+`Orchestrator`+`Environment`+`EventBus` stack and
    runs the task in a background asyncio task. Routes HTTP to TaskManager
    (state) + Orchestrator (execution); exposes the EventBus as a bounded
    per-task event ring; forwards cancel to the existing cooperative
    `CancellationToken`; tears every run down via `ShutdownCoordinator`. NO
    subprocess, NO filesystem mutation, NO second ToolRegistry/PermissionPolicy,
    NO direct Environment access.
  - `app.py`: Starlette ASGI app. Routes: health, create/list/get/start/resume/
    cancel/outcome tasks, SSE events. Pure-ASGI `_OriginGuardMiddleware` (NOT
    BaseHTTPMiddleware — avoids buffering the SSE stream + TestClient teardown
    artifacts) rejects cross-origin requests. SSE replays history then streams
    live events until the task terminates; reconnect via `Last-Event-ID`.
  - `static/index.html`: responsive vanilla-JS frontend (no framework). Task
    composer, status, live event console, agent output, tool activity,
    failure/recovery panel, cancel.
- Config (`config/settings.py`): `web_enabled`(F)/`web_host`/`web_port`/
  `web_event_poll_timeout`/`web_max_event_log` with validators (port 1..65535,
  timeout 0< ≤60, log 1..10000). Env prefix `KINETIC_WEB_*`.
- CLI: `kinetic web [--workspace] [--host] [--port] [--allow-no-key]` starts
  uvicorn, wires `ShutdownCoordinator`+signal handlers, cleans up on exit.
- Packaging: `web` added to hatchling `only-include`; `starlette`+`uvicorn`
  added to deps. Wheel builds + installs in clean venv; CLI entrypoint works.
- Security: `test_phase7_security.py` `_SOURCE_DIRS` now includes `web` so the
  unsafe-pattern grep (no subprocess/os.system/shell=True/eval/exec) covers the
  web layer. Verified: no subprocess import, no filesystem mutation, no
  PermissionPolicy/Environment bypass in the web layer.
760 tests pass (725 prior + 35 new Phase 7.3). 17 skipped (16 prior + 1 gated
live-agent test). Ruff clean. Wheel builds + installs. CLI works. E2E passes.

## Phase 7.3 gotchas
- The web layer must NOT mask system-generated identifiers. The `SecretDetector`
  matches `token_blob` = ≥32-char hex/base64, so a `uuid4().hex` task_id
  (32 hex chars) looks like a secret. `web.serialize._IDENTIFIER_KEYS` exempts
  id/task_id/session_id/plan_id/step_id/project_id/name from masking (a UUID is
  not a credential; masking it breaks the API contract). Free-form content
  (prompts, tool output, error messages) is ALWAYS masked.
- The EventBus's own `_redact_secrets` (Phase 7) runs at publish time BEFORE the
  web layer sees an event — so a task_id inside event `data` may appear as
  `<redacted>`. This is PRE-EXISTING EventBus behavior preserved per spec; the
  frontend gets the task_id from the URL/creation response (which the web layer
  correctly preserves), and SSE is already task-scoped, so it doesn't break.
- The SSE handler is a pure `StreamingResponse` (not `sse-starlette`) for full
  control over the bounded poll loop + `stream_end` sentinel. It drains the
  per-task ring on connect (replay), then polls with `web_event_poll_timeout`
  until `run.is_terminal`, emits `stream_end`, returns.
- `_OriginGuardMiddleware` is a pure ASGI middleware (`__call__(scope, receive,
  send)`), NOT `BaseHTTPMiddleware`. The latter buffers the response stream
  (breaking SSE) and surfaces unraisable GeneratorExit warnings under the
  Starlette TestClient portal. Pure ASGI avoids both.
- TestClient (httpx-based) runs each request in a synced portal whose loop is
  suspended between requests, so a background `asyncio.create_task` from
  `create_task` only progresses while a request is being served (the loop runs
  during request handling). Tests use `delay=0.0` (yield-only) fakes so the
  background task completes across requests; SSE tests keep the loop alive
  during streaming so real delays would also work.
- `_now()` uses `asyncio.get_running_loop().time()` with a `time.monotonic()`
  fallback — safe during GC when a background task is destroyed with no running
  loop (the TestClient teardown path).
- Pytest `filterwarnings` adds three test-only suppressions: the httpx raw-bytes
  deprecation, the `_pump_events` never-awaited RuntimeWarning (test portal
  closes before the coroutine is awaited), and the Starlette/TestClient
  unraisable GeneratorExit (portal loop closes while a background task is
  pending). Production runs under uvicorn where the loop persists across
  requests, so none of these occur in real use.

## TODO Phase 8
Not started.
