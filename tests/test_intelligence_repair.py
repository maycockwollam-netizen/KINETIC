"""Tests for the RepairCoordinator and repair context (Phase 6).

Uses deterministic fake runners/verifiers — no live model, no ANTHROPIC_API_KEY.
Covers: successful repair, repeated failure (stuck), no-op repair, cancellation
during repair, permission denial during verification, environment failure during
verification, bounded retry limits, and secret masking in the repair context.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from environment import Environment
from environment.config import RUNTIME_LOCAL, EnvironmentConfig
from environment.network import NetworkPolicy
from errors import PermissionDeniedError, VerificationError
from events import EventBus
from intelligence.analyzer import FailureAnalyzer
from intelligence.diff import ChangeAnalyzer
from intelligence.regression import RegressionChecker
from intelligence.repair import RepairContextBuilder, RepairCoordinator
from intelligence.stuck import StuckDetector
from tasks.policies import VerificationOutcome
from tasks.verifier import VerificationResult, Verifier


class FakeRepairRunner:
    """Deterministic repair runner: returns scripted outcomes per call."""

    def __init__(self, outcomes: list[dict[str, Any]] | None = None) -> None:
        self.outcomes = outcomes or []
        self.calls = 0
        self.prompts: list[str] = []

    async def repair(self, *, prompt: str, session_id: str) -> dict[str, Any]:
        self.prompts.append(prompt)
        idx = self.calls
        self.calls += 1
        if idx < len(self.outcomes):
            return self.outcomes[idx]
        return {"success": True, "exit_code": 0}


class ScriptedVerifier:
    """Returns scripted verification results in sequence."""

    def __init__(self, results: list[VerificationResult]) -> None:
        self._results = results
        self.calls = 0

    async def verify(self, *, command: str | None = None) -> VerificationResult:
        idx = self.calls
        self.calls += 1
        if idx < len(self._results):
            return self._results[idx]
        return self._results[-1]

    def classify(self, result: VerificationResult) -> dict[str, Any]:
        return {"outcome": result.outcome.value}


def _failed_result(command: str = "uv run pytest -q") -> VerificationResult:
    return VerificationResult(
        outcome=VerificationOutcome.FAIL,
        command=command,
        exit_code=1,
        stdout="FAILED tests/test_app.py::test_add - assert 1==3\n1 failed",
        stderr="",
        reason="verification command exited with code 1",
    )


def _passed_result(command: str = "uv run pytest -q") -> VerificationResult:
    return VerificationResult(outcome=VerificationOutcome.PASS, command=command, exit_code=0)


def _coordinator(
    *,
    repair_runner: FakeRepairRunner,
    verifier: Verifier,
    events: EventBus,
    max_repair: int = 2,
    max_verify: int = 3,
    regression_checker: RegressionChecker | None = None,
    change_analyzer: ChangeAnalyzer | None = None,
) -> RepairCoordinator:
    return RepairCoordinator(
        runner=repair_runner,
        analyzer=FailureAnalyzer(max_stdout_chars=400, max_stderr_chars=400, events=events, session_id="t"),
        verifier=verifier,
        context_builder=RepairContextBuilder(max_chars=2000),
        stuck_detector=StuckDetector(repeat_threshold=2, events=events, session_id="t"),
        regression_checker=regression_checker,
        change_analyzer=change_analyzer,
        max_repair_attempts=max_repair,
        max_verification_attempts=max_verify,
        events=events,
        session_id="t",
    )


@pytest.fixture
def events() -> EventBus:
    return EventBus()


class TestSuccessfulRepair:
    async def test_repair_succeeds_then_passes(self, events: EventBus) -> None:
        runner = FakeRepairRunner([{"success": True, "exit_code": 0}])
        verifier = ScriptedVerifier([_passed_result()])
        coord = _coordinator(repair_runner=runner, verifier=verifier, events=events)
        outcome = await coord.repair(failed_verification=_failed_result(), task_request="fix tests")
        assert outcome.success
        assert outcome.state.repair_count == 1
        assert outcome.state.verification_attempts == 1

    async def test_repair_events_emitted(self, events: EventBus) -> None:
        runner = FakeRepairRunner([{"success": True, "exit_code": 0}])
        verifier = ScriptedVerifier([_passed_result()])
        coord = _coordinator(repair_runner=runner, verifier=verifier, events=events)
        await coord.repair(failed_verification=_failed_result())
        types = [e.type.value for e in events.history]
        assert "failure_analyzed" in types
        assert "repair_started" in types
        assert "repair_completed" in types
        assert "verification_retry" in types


class TestRepeatedFailure:
    async def test_repeated_failure_exhausts_budget(self, events: EventBus) -> None:
        # Repair always "succeeds" but verification keeps failing.
        runner = FakeRepairRunner([{"success": True, "exit_code": 0}] * 5)
        verifier = ScriptedVerifier([_failed_result()] * 6)
        coord = _coordinator(repair_runner=runner, verifier=verifier, events=events, max_repair=3)
        outcome = await coord.repair(failed_verification=_failed_result())
        assert not outcome.success
        assert "budget" in outcome.reason or "stuck" in outcome.reason

    async def test_stuck_detection_terminates(self, events: EventBus) -> None:
        # Identical failure repeated -> stuck.
        runner = FakeRepairRunner([{"success": True, "exit_code": 0}] * 5)
        verifier = ScriptedVerifier([_failed_result()] * 6)
        coord = _coordinator(repair_runner=runner, verifier=verifier, events=events, max_repair=5)
        outcome = await coord.repair(failed_verification=_failed_result())
        assert not outcome.success
        types = [e.type.value for e in events.history]
        assert "stuck_detected" in types


class TestNoOpRepair:
    async def test_repair_runner_fails_marks_failed(self, events: EventBus) -> None:
        runner = FakeRepairRunner([{"success": False, "exit_code": 1, "errors": ["could not fix"]}] * 3)
        verifier = ScriptedVerifier([_failed_result()] * 4)
        coord = _coordinator(repair_runner=runner, verifier=verifier, events=events, max_repair=3)
        outcome = await coord.repair(failed_verification=_failed_result())
        assert not outcome.success


class TestTerminalFailuresNotRepaired:
    async def test_permission_denied_not_repaired(self, events: EventBus) -> None:
        runner = FakeRepairRunner()
        verifier = ScriptedVerifier([_failed_result()])
        coord = _coordinator(repair_runner=runner, verifier=verifier, events=events)
        perm_failed = VerificationResult(
            outcome=VerificationOutcome.FAIL, command="pytest", exit_code=1,
            stderr="permission denied for tool", reason="permission denied",
        )
        outcome = await coord.repair(failed_verification=perm_failed)
        assert not outcome.success
        assert runner.calls == 0  # repair not attempted

    async def test_cancellation_not_repaired(self, events: EventBus) -> None:
        runner = FakeRepairRunner()
        verifier = ScriptedVerifier([_failed_result()])
        coord = _coordinator(repair_runner=runner, verifier=verifier, events=events)
        cancelled = VerificationResult(
            outcome=VerificationOutcome.FAIL, command="pytest", exit_code=1,
            stderr="cancelled by user", reason="cancelled",
        )
        outcome = await coord.repair(failed_verification=cancelled)
        assert not outcome.success
        assert runner.calls == 0


class TestRegressionDetection:
    async def test_regression_detected_after_repair(self, events: EventBus, tmp_path: Path) -> None:
        # Build a real environment so regression check runs a real command.
        cfg = EnvironmentConfig(runtime_type=RUNTIME_LOCAL, sandbox_mode=False, network=NetworkPolicy.ALLOW)
        env = Environment.create(tmp_path / "ws", cfg, events=events, session_id="t")
        await env.provision()
        try:
            # Repair "succeeds", re-verify passes, but broader regression check fails.
            runner = FakeRepairRunner([{"success": True, "exit_code": 0}])
            verifier = ScriptedVerifier([_passed_result()])
            regression = RegressionChecker(
                verifier=Verifier(environment=env, configured_command="false"),
                events=events, session_id="t",
            )
            coord = _coordinator(
                repair_runner=runner, verifier=verifier, events=events,
                regression_checker=regression,
            )
            outcome = await coord.repair(failed_verification=_failed_result())
            assert not outcome.success
            assert outcome.regression is True
            assert outcome.state.regression_detected
        finally:
            await env.destroy()

    async def test_no_regression_passes(self, events: EventBus, tmp_path: Path) -> None:
        cfg = EnvironmentConfig(runtime_type=RUNTIME_LOCAL, sandbox_mode=False, network=NetworkPolicy.ALLOW)
        env = Environment.create(tmp_path / "ws", cfg, events=events, session_id="t")
        await env.provision()
        try:
            runner = FakeRepairRunner([{"success": True, "exit_code": 0}])
            verifier = ScriptedVerifier([_passed_result()])
            regression = RegressionChecker(
                verifier=Verifier(environment=env, configured_command="true"),
                events=events, session_id="t",
            )
            coord = _coordinator(
                repair_runner=runner, verifier=verifier, events=events,
                regression_checker=regression,
            )
            outcome = await coord.repair(failed_verification=_failed_result())
            assert outcome.success
            assert not outcome.regression
        finally:
            await env.destroy()


class TestEnvironmentFailureDuringVerification:
    async def test_reverification_error_is_bounded_failure(self, events: EventBus) -> None:
        class ErrorVerifier:
            async def verify(self, *, command: str | None = None) -> VerificationResult:
                raise VerificationError("environment blew up")

            def classify(self, result: VerificationResult) -> dict[str, Any]:
                return {}

        runner = FakeRepairRunner([{"success": True, "exit_code": 0}])
        coord = _coordinator(
            repair_runner=runner, verifier=ErrorVerifier(), events=events,  # type: ignore[arg-type]
        )
        outcome = await coord.repair(failed_verification=_failed_result())
        assert not outcome.success
        assert "re-verification error" in outcome.reason


class TestPermissionDeniedDuringVerification:
    async def test_permission_denied_propagates_as_failure(self, events: EventBus) -> None:
        class PermVerifier:
            async def verify(self, *, command: str | None = None) -> VerificationResult:
                raise PermissionDeniedError("run_command", "environment exec disabled")

            def classify(self, result: VerificationResult) -> dict[str, Any]:
                return {}

        runner = FakeRepairRunner([{"success": True, "exit_code": 0}])
        coord = _coordinator(
            repair_runner=runner, verifier=PermVerifier(), events=events,  # type: ignore[arg-type]
        )
        outcome = await coord.repair(failed_verification=_failed_result())
        assert not outcome.success


class TestRepairContextBounded:
    def test_context_is_bounded_and_masks_secrets(self) -> None:
        analyzer = FailureAnalyzer(max_stdout_chars=400, max_stderr_chars=400)
        analysis = analyzer.analyze(
            command="pytest", exit_code=1,
            stderr="api_key=sk-1234567890abcdef1234567890abcdef failed",
            stdout="FAILED tests/x.py::test_a\n1 failed",
        )
        builder = RepairContextBuilder(max_chars=500)
        ctx = builder.build(analysis=analysis, task_request="fix it", previous_attempts=[])
        assert "sk-1234567890abcdef1234567890abcdef" not in ctx
        assert "test_a" in ctx
        assert len(ctx) <= 600

    def test_context_includes_previous_attempts(self) -> None:
        analyzer = FailureAnalyzer(max_stdout_chars=400, max_stderr_chars=400)
        analysis = analyzer.analyze(command="pytest", exit_code=1, stdout="FAILED tests/x.py::test_a\n1 failed")
        from intelligence.models import RepairAttempt

        builder = RepairContextBuilder()
        ctx = builder.build(
            analysis=analysis,
            previous_attempts=[RepairAttempt(attempt=1, analysis=analysis, success=False)],
        )
        assert "Previous repair attempts" in ctx
        assert "attempt 1" in ctx


class TestCancellationDuringRepair:
    async def test_repair_runner_error_is_bounded(self, events: EventBus) -> None:
        class CrashRunner:
            async def repair(self, *, prompt: str, session_id: str) -> dict[str, Any]:
                raise RuntimeError("cancelled")

        verifier = ScriptedVerifier([_failed_result()])
        coord = _coordinator(
            repair_runner=CrashRunner(), verifier=verifier, events=events,  # type: ignore[arg-type]
        )
        outcome = await coord.repair(failed_verification=_failed_result())
        assert not outcome.success
        assert "repair runner error" in outcome.reason


class TestBudgetLimits:
    async def test_verification_budget_exhausted(self, events: EventBus) -> None:
        # Each repair "succeeds" but verification fails; with max_verify=1 the
        # second verification attempt is refused.
        runner = FakeRepairRunner([{"success": True, "exit_code": 0}] * 5)
        verifier = ScriptedVerifier([_failed_result()] * 6)
        coord = _coordinator(
            repair_runner=runner, verifier=verifier, events=events,
            max_repair=5, max_verify=1,
        )
        outcome = await coord.repair(failed_verification=_failed_result())
        assert not outcome.success
        # Either verification budget or stuck detection terminates it.
        assert outcome.state.verification_attempts <= 2
