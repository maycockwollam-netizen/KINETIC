"""Minimal CLI entrypoint for Phase 1.

    kinetic run "inspect this repo and list its files" [--workspace PATH]

Streams agent events to stdout. Live model calls require ANTHROPIC_API_KEY;
without it the CLI exits with a clear error (no silent failure).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import click

from agent.session import AgentSession, SessionConfig
from config import Settings


def _check_api_key() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        click.echo(
            "error: ANTHROPIC_API_KEY is not set. The agent needs a Claude API key "
            "to run live model calls. Set it and retry.",
            err=True,
        )
        sys.exit(2)


def _load_settings(ctx: click.Context) -> Settings:
    """Load settings from an optional config file (in context) + env vars."""
    config_file = ctx.obj.get("config_file") if ctx.obj else None
    if config_file:
        from config import load_settings

        return load_settings(config_file)
    return Settings()


@click.group()
@click.option("--config", "config_file", default=None, type=click.Path(exists=True),
              help="Path to a JSON config file (env vars still override).")
@click.pass_context
def cli(ctx: click.Context, config_file: str | None) -> None:
    """KINETIC coding agent."""
    ctx.ensure_object(dict)
    ctx.obj["config_file"] = config_file


@cli.command()
@click.argument("prompt")
@click.option("--workspace", "-w", default=".", help="Project workspace path.")
@click.option("--model", default=None, help="Override the configured model.")
@click.option("--max-turns", default=40, type=int, help="Max agent turns.")
@click.option("--allow-network", is_flag=True, default=False, help="Enable network tools.")
@click.option(
    "--runtime",
    type=click.Choice(["local", "docker"]),
    default=None,
    help="Execution runtime (default: from settings).",
)
@click.option(
    "--network-policy",
    type=click.Choice(["deny", "allow", "restricted"]),
    default=None,
    help="Sandbox network policy (default: deny).",
)
@click.option("--dry-run", is_flag=True, default=False, help="Build the session without running the model.")
@click.pass_context
def run(
    ctx: click.Context,
    prompt: str,
    workspace: str,
    model: str | None,
    max_turns: int,
    allow_network: bool,
    runtime: str | None,
    network_policy: str | None,
    dry_run: bool,
) -> None:
    """Run the agent against a workspace with the given prompt."""
    settings = _load_settings(ctx)
    settings.ensure_directories()
    ws = Path(workspace).resolve()
    if not ws.exists():
        click.echo(f"error: workspace not found: {ws}", err=True)
        sys.exit(1)

    cfg = SessionConfig(
        workspace=ws,
        prompt=prompt,
        model=model,
        max_turns=max_turns,
        allow_network=allow_network,
        runtime_type=runtime,
        network_policy=network_policy,
    )
    session = AgentSession(settings, cfg)

    if dry_run:
        click.echo(f"session_id={session.session_id} workspace={ws} tools={session.registry.names()}")
        return

    _check_api_key()
    import anyio

    result = anyio.run(session.run)
    for ev in result.events:
        click.echo(f"[{ev['type']}] {ev.get('data', {})}")
    if result.success:
        click.echo("\n=== RESULT ===")
        click.echo(result.result_text or "(no text result)")
    else:
        click.echo(f"\n=== FAILED ===\n{result.error}", err=True)
        sys.exit(1)


def main() -> None:
    cli()


# --- Phase 5: task orchestration commands -----------------------------------

def _load_orchestrator_state(workspace: str, task_id: str) -> dict[str, object]:
    """Best-effort: load a task checkpoint to report status without a live model.

    Returns a status dict; never starts the agent. Used by ``kinetic task status``.
    Loads the raw checkpoint data directly so terminal tasks (which
    ``restore_checkpoint`` refuses to resume) can still be inspected.
    """
    from config import Settings
    from tasks.checkpoints import CheckpointStore
    from tasks.models import Plan, Task

    settings = Settings()
    store = CheckpointStore(settings.checkpoint_dir)
    if not store.exists(task_id):
        return {"error": f"no checkpoint for task {task_id}"}
    try:
        raw = store.load(task_id)
        task = Task.model_validate(raw.get("task", {}))
        plan: Plan | None = None
        if isinstance(raw.get("plan"), dict):
            plan = Plan.model_validate(raw["plan"])
    except Exception as exc:  # noqa: BLE001
        return {"error": f"checkpoint corrupt: {exc}"}
    return {
        "task": task.summary(),
        "plan_id": plan.plan_id if plan else None,
        "plan_steps": len(plan.steps) if plan else 0,
        "workspace": workspace,
    }


@cli.group()
def task() -> None:
    """Task orchestration commands (Phase 5)."""


@task.command("status")
@click.argument("task_id")
@click.option("--workspace", "-w", default=".", help="Project workspace path.")
def task_status(task_id: str, workspace: str) -> None:
    """Show the status of a task (from its checkpoint)."""
    state = _load_orchestrator_state(workspace, task_id)
    if "error" in state:
        click.echo(str(state["error"]), err=True)
        sys.exit(1)
    click.echo(f"Task: {task_id}")
    click.echo(f"State: {state['task']['state']}")
    click.echo(f"Current step: {state['task']['current_step']}")
    click.echo(f"Attempt count: {state['task']['attempt_count']}")
    click.echo(f"Replan count: {state['task']['replan_count']}")
    if state.get("plan_id"):
        click.echo(f"Plan: {state['plan_id']} ({state['plan_steps']} steps)")
    if state["task"].get("cancelled"):
        click.echo("Cancelled: yes")
    if state["task"].get("failure"):
        click.echo(f"Failure: {state['task']['failure']}")


@task.command("cancel")
@click.argument("task_id")
def task_cancel(task_id: str) -> None:
    """Record a cancellation request for a task.

    NOTE: this marks the checkpoint's task as cancelled on disk. A running task
    is cancelled cooperatively via its in-memory TaskManager; this command is
    for tasks whose state is persisted in a checkpoint.
    """
    from config import Settings
    from tasks.checkpoints import CheckpointStore, build_checkpoint, restore_checkpoint

    settings = Settings()
    store = CheckpointStore(settings.checkpoint_dir)
    if not store.exists(task_id):
        click.echo(f"error: no checkpoint for task {task_id}", err=True)
        sys.exit(1)
    # Load raw data; terminal tasks are loadable here even though
    # restore_checkpoint refuses them (we only inspect + mark, not resume).
    raw = store.load(task_id)
    raw_task = raw.get("task", {})
    state = raw_task.get("state")
    if state in ("completed", "failed", "cancelled"):
        click.echo(f"task already in terminal state: {state}")
        return
    task, plan, obs = restore_checkpoint(raw)
    task.cancelled = True
    task.cancellation_reason = "cancelled via CLI"
    store.save(build_checkpoint(task, plan, observations=obs))
    click.echo(f"task {task_id} marked cancelled")


@task.command("resume")
@click.argument("task_id")
@click.option("--workspace", "-w", default=".", help="Project workspace path.")
@click.option("--allow-network", is_flag=True, default=False, help="Enable network tools.")
@click.option(
    "--runtime", type=click.Choice(["local", "docker"]), default=None,
    help="Execution runtime (default: from settings).",
)
def task_resume(task_id: str, workspace: str, allow_network: bool, runtime: str | None) -> None:
    """Resume a task from its checkpoint (requires ANTHROPIC_API_KEY)."""
    _check_api_key()
    from pathlib import Path as _Path

    import anyio

    from agent.session import AgentSession, SessionConfig
    from config import Settings
    from project.scanner import scan_project
    from tasks.orchestrator import Orchestrator

    ws = _Path(workspace).resolve()
    if not ws.exists():
        click.echo(f"error: workspace not found: {ws}", err=True)
        sys.exit(1)
    settings = Settings()
    settings.ensure_directories()
    cfg = SessionConfig(workspace=ws, prompt="(resume)", allow_network=allow_network, runtime_type=runtime)
    session = AgentSession(settings, cfg)
    manifest = scan_project(ws)
    orch = Orchestrator(session, settings=settings, manifest=manifest)
    outcome = anyio.run(orch.resume_task, task_id)
    click.echo(f"task {task_id} -> {outcome.task.state}")
    if outcome.failure:
        click.echo(f"failure: {outcome.failure.message}")
    if outcome.task.state.value not in ("completed",):
        sys.exit(1)


# --- Phase 6: coding-intelligence task commands ----------------------------


@task.command("inspect")
@click.argument("task_id")
@click.option("--workspace", "-w", default=".", help="Project workspace path.")
def task_inspect(task_id: str, workspace: str) -> None:
    """Inspect a task's full state including Phase 6 repair/review info.

    Loads the checkpoint without a live model. Shows task state, plan, and any
    persisted repair state / final review.
    """
    from config import Settings
    from tasks.checkpoints import CheckpointStore, restore_repair_state

    settings = Settings()
    store = CheckpointStore(settings.checkpoint_dir)
    if not store.exists(task_id):
        click.echo(f"error: no checkpoint for task {task_id}", err=True)
        sys.exit(1)
    try:
        raw = store.load(task_id)
    except Exception as exc:  # noqa: BLE001
        click.echo(f"error: checkpoint corrupt: {exc}", err=True)
        sys.exit(1)
    state = _load_orchestrator_state(workspace, task_id)
    if "error" in state:
        click.echo(str(state["error"]), err=True)
        sys.exit(1)
    click.echo(f"Task: {task_id}")
    click.echo(f"State: {state['task']['state']}")
    click.echo(f"Current step: {state['task']['current_step']}")
    click.echo(f"Attempt count: {state['task']['attempt_count']}")
    click.echo(f"Replan count: {state['task']['replan_count']}")
    if state.get("plan_id"):
        click.echo(f"Plan: {state['plan_id']} ({state['plan_steps']} steps)")
    try:
        rs = restore_repair_state(raw)
    except Exception as exc:  # noqa: BLE001
        rs = None
        click.echo(f"(repair state corrupt: {exc})", err=True)
    if rs:
        click.echo("Repair state:")
        click.echo(f"  verification_attempts: {rs.get('verification_attempts', 0)}")
        click.echo(f"  total_recovery_attempts: {rs.get('total_recovery_attempts', 0)}")
        click.echo(f"  stuck: {rs.get('stuck')}")
        click.echo(f"  regression_detected: {rs.get('regression_detected', False)}")
        attempts = rs.get("attempts", [])
        click.echo(f"  repair attempts: {len(attempts)}")
        for a in attempts:
            status = "success" if a.get("success") else "failed"
            click.echo(f"    - attempt {a.get('attempt')}: {status}")


@task.command("failures")
@click.argument("task_id")
def task_failures(task_id: str) -> None:
    """Show the failure analysis for a task (from its checkpoint)."""
    from config import Settings
    from tasks.checkpoints import CheckpointStore, restore_repair_state

    settings = Settings()
    store = CheckpointStore(settings.checkpoint_dir)
    if not store.exists(task_id):
        click.echo(f"error: no checkpoint for task {task_id}", err=True)
        sys.exit(1)
    raw = store.load(task_id)
    failure = (raw.get("task") or {}).get("failure")
    if failure:
        click.echo(f"Task failure class: {failure.get('failure_class')}")
        click.echo(f"Message: {failure.get('message')}")
    else:
        click.echo("No task-level failure recorded.")
    try:
        rs = restore_repair_state(raw)
    except Exception as exc:  # noqa: BLE001
        click.echo(f"(repair state corrupt: {exc})", err=True)
        return
    if not rs:
        click.echo("No repair state recorded (Phase 6 repair not engaged).")
        return
    for a in rs.get("attempts", []):
        analysis = a.get("analysis") or {}
        click.echo(
            f"attempt {a.get('attempt')}: class={analysis.get('failure_class')} "
            f"exit_code={analysis.get('exit_code')} "
            f"failures={analysis.get('failure_count')}"
        )
        for tf in (analysis.get("test_failures") or [])[:8]:
            click.echo(f"  - {tf.get('name')} ({tf.get('file')}:{tf.get('line')})")


@task.command("verify")
@click.argument("task_id")
@click.option("--workspace", "-w", default=".", help="Project workspace path.")
@click.option("--allow-network", is_flag=True, default=False, help="Enable network tools.")
@click.option(
    "--runtime", type=click.Choice(["local", "docker"]), default=None,
    help="Execution runtime (default: from settings).",
)
def task_verify(task_id: str, workspace: str, allow_network: bool, runtime: str | None) -> None:
    """Re-run a task's verification command (requires ANTHROPIC_API_KEY).

    Provisions a sandboxed environment and runs the project verification
    command through the existing safe path. Does NOT call the model; only
    verifies the current working tree.
    """
    _check_api_key()
    from pathlib import Path as _Path

    import anyio

    from agent.session import AgentSession, SessionConfig
    from config import Settings
    from project.scanner import scan_project
    from tasks.verifier import Verifier

    ws = _Path(workspace).resolve()
    if not ws.exists():
        click.echo(f"error: workspace not found: {ws}", err=True)
        sys.exit(1)
    settings = Settings()
    settings.ensure_directories()
    cfg = SessionConfig(workspace=ws, prompt="(verify)", allow_network=allow_network, runtime_type=runtime)
    session = AgentSession(settings, cfg)
    manifest = scan_project(ws)
    verifier = Verifier.from_settings(settings, environment=session.environment, manifest=manifest)

    async def _run() -> None:
        try:
            await session.prepare()
        except Exception as exc:  # noqa: BLE001
            click.echo(f"error: provisioning failed: {exc}", err=True)
            sys.exit(1)
        try:
            result = await verifier.verify()
            click.echo(f"outcome: {result.outcome.value}")
            click.echo(f"command: {result.command}")
            if result.exit_code is not None:
                click.echo(f"exit_code: {result.exit_code}")
            if result.reason:
                click.echo(f"reason: {result.reason}")
            if result.outcome.value != "pass":
                sys.exit(1)
        finally:
            await session.finish()

    anyio.run(_run)


# --- Phase 7.3: web agent test console -------------------------------------


@cli.command()
@click.option("--workspace", "-w", default=".", help="Project workspace path.")
@click.option("--host", default=None, help="Bind host (default: from settings).")
@click.option("--port", default=None, type=int, help="Bind port (default: from settings).")
@click.option("--allow-no-key", is_flag=True, default=False,
              help="Start the server even without ANTHROPIC_API_KEY (tasks will fail to run).")
@click.pass_context
def web(ctx: click.Context, workspace: str, host: str | None, port: int | None,
        allow_no_key: bool) -> None:
    """Start the KINETIC Web Agent Test Console (Phase 7.3).

    A thin HTTP/SSE adapter over the existing backend. This is a test/control
    surface, NOT the final product UI. Open http://<host>:<port>/ in a browser.
    """
    import os
    from pathlib import Path as _Path

    settings = _load_settings(ctx)
    settings.ensure_directories()
    ws = _Path(workspace).resolve()
    if not ws.exists():
        click.echo(f"error: workspace not found: {ws}", err=True)
        sys.exit(1)

    if not allow_no_key and not os.environ.get("ANTHROPIC_API_KEY"):
        click.echo(
            "warning: ANTHROPIC_API_KEY is not set. The console will start, but "
            "creating tasks will fail until a key is set. Use --allow-no-key to "
            "suppress this warning.",
            err=True,
        )

    try:
        import uvicorn
    except ImportError as exc:
        click.echo(f"error: uvicorn is not installed: {exc}", err=True)
        sys.exit(1)

    from web import create_app
    from web.console import WebConsole

    bind_host = host or settings.web_host
    bind_port = port or settings.web_port
    require_key = not allow_no_key
    app = create_app(
        settings=settings, workspace=ws, require_api_key=require_key,
    )
    console: WebConsole = app.state.console

    import anyio

    from lifecycle import install_signal_handlers

    async def _serve() -> None:
        coordinator = console.register_shutdown()
        remove_signals = install_signal_handlers(coordinator)
        config = uvicorn.Config(
            app, host=bind_host, port=bind_port, log_level="info",
            access_log=False, lifespan="on",
        )
        server = uvicorn.Server(config)
        click.echo(f"KINETIC Web Agent Test Console on http://{bind_host}:{bind_port}")
        click.echo(f"workspace: {ws}")
        try:
            await server.serve()
        finally:
            await console.shutdown(reason="cli exit")
            remove_signals()

    import contextlib

    with contextlib.suppress(KeyboardInterrupt):
        anyio.run(_serve)

