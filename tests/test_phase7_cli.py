"""Phase 7 — CLI hardening tests.

Verifies clear exit codes, no tracebacks for expected user errors, --help,
dry-run without API key, and task status/inspect/failures commands.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from kinetic.cli.main import cli


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class TestHelpAndBasics:
    def test_help_works(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "KINETIC" in result.output or "kinetic" in result.output

    def test_run_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["run", "--help"])
        assert result.exit_code == 0
        assert "--workspace" in result.output
        assert "--dry-run" in result.output

    def test_task_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["task", "--help"])
        assert result.exit_code == 0
        assert "status" in result.output
        assert "inspect" in result.output


class TestDryRunNoApiKey:
    def test_dry_run_succeeds_without_key(self, runner: CliRunner, workspace: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        result = runner.invoke(cli, ["run", "test", "--workspace", str(workspace), "--dry-run"])
        assert result.exit_code == 0
        assert "session_id=" in result.output

    def test_live_run_fails_without_key(self, runner: CliRunner, workspace: Path,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        result = runner.invoke(cli, ["run", "test", "--workspace", str(workspace)])
        assert result.exit_code == 2
        assert "ANTHROPIC_API_KEY" in result.output
        # No raw traceback for an expected user error.
        assert "Traceback" not in result.output


class TestExitCodes:
    def test_missing_workspace_exit_1(self, runner: CliRunner, tmp_path: Path) -> None:
        result = runner.invoke(cli, ["run", "test", "--workspace", str(tmp_path / "nope")])
        assert result.exit_code == 1
        assert "workspace not found" in result.output

    def test_task_status_missing_exit_1(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["task", "status", "nonexistent"])
        assert result.exit_code == 1

    def test_task_inspect_missing_exit_1(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["task", "inspect", "nonexistent"])
        assert result.exit_code == 1

    def test_task_failures_missing_exit_1(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["task", "failures", "nonexistent"])
        assert result.exit_code == 1


class TestTaskCommands:
    def test_task_status_from_checkpoint(self, runner: CliRunner, tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
        from kinetic.config import Settings
        from kinetic.tasks.checkpoints import CheckpointStore, build_checkpoint
        from kinetic.tasks.models import Plan, Task
        from kinetic.tasks.states import TaskState

        ckpt_dir = tmp_path / "ckpts"
        monkeypatch.setenv("KINETIC_CHECKPOINT_DIR", str(ckpt_dir))
        settings = Settings()
        store = CheckpointStore(settings.checkpoint_dir)
        task = Task(id="t-cli-1", user_request="do thing", workspace=str(tmp_path),
                    state=TaskState.EXECUTING)
        plan = Plan(plan_id="p1", task_id="t-cli-1", goal="g", steps=[])
        store.save(build_checkpoint(task, plan, observations=[]))

        result = runner.invoke(cli, ["task", "status", "t-cli-1"])
        assert result.exit_code == 0, result.output
        assert "EXECUTING" in result.output.upper() or "executing" in result.output

    def test_task_inspect_from_checkpoint(self, runner: CliRunner, tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
        from kinetic.config import Settings
        from kinetic.tasks.checkpoints import CheckpointStore, build_checkpoint
        from kinetic.tasks.models import Plan, Task
        from kinetic.tasks.states import TaskState

        ckpt_dir = tmp_path / "ckpts"
        monkeypatch.setenv("KINETIC_CHECKPOINT_DIR", str(ckpt_dir))
        settings = Settings()
        store = CheckpointStore(settings.checkpoint_dir)
        task = Task(id="t-cli-2", user_request="do thing", workspace=str(tmp_path),
                    state=TaskState.VERIFYING)
        plan = Plan(plan_id="p2", task_id="t-cli-2", goal="g", steps=[])
        store.save(build_checkpoint(task, plan, observations=[]))

        result = runner.invoke(cli, ["task", "inspect", "t-cli-2"])
        assert result.exit_code == 0, result.output

    def test_task_cancel_from_checkpoint(self, runner: CliRunner, tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
        from kinetic.config import Settings
        from kinetic.tasks.checkpoints import CheckpointStore, build_checkpoint
        from kinetic.tasks.models import Plan, Task
        from kinetic.tasks.states import TaskState

        ckpt_dir = tmp_path / "ckpts"
        monkeypatch.setenv("KINETIC_CHECKPOINT_DIR", str(ckpt_dir))
        settings = Settings()
        store = CheckpointStore(settings.checkpoint_dir)
        task = Task(id="t-cli-3", user_request="do thing", workspace=str(tmp_path),
                    state=TaskState.EXECUTING)
        plan = Plan(plan_id="p3", task_id="t-cli-3", goal="g", steps=[])
        store.save(build_checkpoint(task, plan, observations=[]))

        result = runner.invoke(cli, ["task", "cancel", "t-cli-3"])
        assert result.exit_code == 0, result.output
        assert "cancelled" in result.output.lower()


class TestNoTracebackForExpectedErrors:
    def test_invalid_network_policy_no_traceback(self, runner: CliRunner, workspace: Path) -> None:
        result = runner.invoke(cli, ["run", "test", "--workspace", str(workspace),
                                      "--network-policy", "bogus", "--dry-run"])
        # Click should reject the invalid choice cleanly.
        assert result.exit_code != 0
        assert "Traceback" not in result.output
