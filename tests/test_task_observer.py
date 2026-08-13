"""Observer tests: bounded output, secret filtering."""

from __future__ import annotations

from kinetic.tasks.observer import Observer, summarize


class TestObserver:
    def test_bounded_stdout_stderr(self) -> None:
        obs = Observer(max_stdout_chars=10, max_stderr_chars=5)
        o = obs.observe(step_id="s1", stdout="x" * 100, stderr="y" * 100, exit_code=0)
        assert len(o.stdout_summary) <= 25  # truncated + marker
        assert len(o.stderr_summary) <= 25
        assert "truncated" in o.stdout_summary

    def test_success_inference(self) -> None:
        obs = Observer()
        o = obs.observe(step_id="s1", exit_code=0)
        assert o.success is True
        o = obs.observe(step_id="s1", exit_code=1)
        assert o.success is False

    def test_secret_masking(self) -> None:
        obs = Observer()
        o = obs.observe(
            step_id="s1",
            stderr="error: AKIAIOSFODNN7EXAMPLE is not valid",
            errors=["token ghp_abcdefghijklmnopqrstuvwxyz leaked"],
            exit_code=1,
        )
        assert "AKIA" not in o.stderr_summary
        assert "secret-hidden" in o.stderr_summary
        assert "ghp_" not in o.errors[0]

    def test_changed_files_and_tool_calls(self) -> None:
        obs = Observer()
        o = obs.observe(
            step_id="s1", exit_code=0,
            changed_files=["a.py", "b.py"], tool_calls=["run_command", "write_file"],
        )
        assert o.changed_files == ["a.py", "b.py"]
        assert o.tool_calls == ["run_command", "write_file"]

    def test_observation_serializable(self) -> None:
        obs = Observer()
        o = obs.observe(step_id="s1", exit_code=0, stdout="ok")
        d = o.to_dict()
        assert d["step_id"] == "s1"
        assert d["success"] is True

    def test_summarize_truncates(self) -> None:
        assert summarize("x" * 1000, limit=10).endswith("truncated)")
        assert summarize(None) == ""
        assert summarize("short") == "short"
