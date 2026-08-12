"""CLI tests."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from kinetic.cli.main import cli


def test_cli_dry_run(workspace: Path):
    runner = CliRunner()
    result = runner.invoke(cli, ["run", "hello", "--workspace", str(workspace), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "session_id=" in result.output
    assert "run_command" in result.output


def test_cli_missing_workspace(tmp_path: Path):
    runner = CliRunner()
    result = runner.invoke(cli, ["run", "hello", "--workspace", str(tmp_path / "nope")])
    assert result.exit_code == 1
    assert "workspace not found" in result.output


def test_cli_requires_api_key_when_not_dry_run(workspace: Path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    runner = CliRunner()
    result = runner.invoke(cli, ["run", "hello", "--workspace", str(workspace)])
    assert result.exit_code == 2
    assert "ANTHROPIC_API_KEY" in result.output
