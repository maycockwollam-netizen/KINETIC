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

from kinetic.agent.session import AgentSession, SessionConfig
from kinetic.config import Settings


def _check_api_key() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        click.echo(
            "error: ANTHROPIC_API_KEY is not set. The agent needs a Claude API key "
            "to run live model calls. Set it and retry.",
            err=True,
        )
        sys.exit(2)


@click.group()
def cli() -> None:
    """KINETIC coding agent."""


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
def run(
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
    settings = Settings()
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
    from kinetic.config import Settings
    from kinetic.tasks.checkpoints import CheckpointStore
    from kinetic.tasks.models import Plan, Task

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
    from kinetic.config import Settings
    from kinetic.tasks.checkpoints import CheckpointStore, build_checkpoint, restore_checkpoint

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

    from kinetic.agent.session import AgentSession, SessionConfig
    from kinetic.config import Settings
    from kinetic.project.scanner import scan_project
    from kinetic.tasks.orchestrator import Orchestrator

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
