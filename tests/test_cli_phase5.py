"""CLI Phase 5 task commands tests."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from cli.main import cli
from config import Settings
from tasks.checkpoints import CheckpointStore, build_checkpoint
from tasks.models import Plan, PlanStep, Task
from tasks.states import TaskState


def _settings_with_dir(tmp_path: Path) -> None:
    """Point the global Settings at a temp checkpoint dir for these tests."""
    import cli.main as cli_mod

    orig = cli_mod.Settings
    cli_mod.Settings = lambda *a, **k: Settings(  # type: ignore[assignment]
        checkpoint_dir=tmp_path / "ckpt",
        audit_log_path=tmp_path / "audit.log",
    )
    Settings(checkpoint_dir=tmp_path / "ckpt", audit_log_path=tmp_path / "audit.log").ensure_directories()
    return orig


def test_task_status_missing_checkpoint(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "config.Settings",
        lambda *a, **k: Settings(checkpoint_dir=tmp_path / "ckpt", audit_log_path=tmp_path / "a.log"),
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["task", "status", "nonexistent"])
    assert result.exit_code == 1
    assert "no checkpoint" in result.output


def test_task_status_reports_state(tmp_path: Path, monkeypatch) -> None:
    ckpt_dir = tmp_path / "ckpt"
    monkeypatch.setattr(
        "config.Settings",
        lambda *a, **k: Settings(checkpoint_dir=ckpt_dir, audit_log_path=tmp_path / "a.log"),
    )
    Settings(checkpoint_dir=ckpt_dir, audit_log_path=tmp_path / "a.log").ensure_directories()
    store = CheckpointStore(ckpt_dir)
    task = Task(id="t1", user_request="do X", workspace=str(tmp_path))
    task.state = TaskState.EXECUTING
    task.current_step = "s2"
    task.plan_id = "p1"
    plan = Plan(plan_id="p1", task_id="t1", steps=[PlanStep(step_id="s1"), PlanStep(step_id="s2")])
    store.save(build_checkpoint(task, plan))
    runner = CliRunner()
    result = runner.invoke(cli, ["task", "status", "t1"])
    assert result.exit_code == 0
    assert "executing" in result.output
    assert "s2" in result.output
    assert "p1" in result.output


def test_task_cancel_marks_checkpoint(tmp_path: Path, monkeypatch) -> None:
    ckpt_dir = tmp_path / "ckpt"
    monkeypatch.setattr(
        "config.Settings",
        lambda *a, **k: Settings(checkpoint_dir=ckpt_dir, audit_log_path=tmp_path / "a.log"),
    )
    Settings(checkpoint_dir=ckpt_dir, audit_log_path=tmp_path / "a.log").ensure_directories()
    store = CheckpointStore(ckpt_dir)
    task = Task(id="t1", user_request="x", workspace=str(tmp_path))
    task.state = TaskState.EXECUTING
    store.save(build_checkpoint(task, None))
    runner = CliRunner()
    result = runner.invoke(cli, ["task", "cancel", "t1"])
    assert result.exit_code == 0
    assert "cancelled" in result.output
    # Re-load and verify cancelled.
    from tasks.checkpoints import restore_checkpoint

    t2, _, _ = restore_checkpoint(store.load("t1"))
    assert t2.cancelled is True


def test_task_cancel_on_terminal_is_noop(tmp_path: Path, monkeypatch) -> None:
    ckpt_dir = tmp_path / "ckpt"
    monkeypatch.setattr(
        "config.Settings",
        lambda *a, **k: Settings(checkpoint_dir=ckpt_dir, audit_log_path=tmp_path / "a.log"),
    )
    Settings(checkpoint_dir=ckpt_dir, audit_log_path=tmp_path / "a.log").ensure_directories()
    store = CheckpointStore(ckpt_dir)
    task = Task(id="t1", user_request="x", workspace=str(tmp_path))
    task.state = TaskState.COMPLETED
    store.save(build_checkpoint(task, None))
    runner = CliRunner()
    result = runner.invoke(cli, ["task", "cancel", "t1"])
    assert result.exit_code == 0
    assert "terminal" in result.output


def test_task_resume_requires_api_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    runner = CliRunner()
    result = runner.invoke(cli, ["task", "resume", "t1", "--workspace", str(tmp_path)])
    assert result.exit_code == 2
    assert "ANTHROPIC_API_KEY" in result.output
