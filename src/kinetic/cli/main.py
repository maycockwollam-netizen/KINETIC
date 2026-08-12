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
@click.option("--dry-run", is_flag=True, default=False, help="Build the session without running the model.")
def run(prompt: str, workspace: str, model: str | None, max_turns: int, allow_network: bool, dry_run: bool) -> None:
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


if __name__ == "__main__":
    main()
