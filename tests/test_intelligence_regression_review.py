"""Tests for the RegressionChecker and FinalReviewer (Phase 6)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kinetic.environment import Environment
from kinetic.environment.config import RUNTIME_LOCAL, EnvironmentConfig
from kinetic.environment.network import NetworkPolicy
from kinetic.events import EventBus
from kinetic.intelligence.models import ChangeAnalysis
from kinetic.intelligence.regression import RegressionChecker
from kinetic.intelligence.review import FinalReviewer
from kinetic.tasks.policies import VerificationOutcome
from kinetic.tasks.verifier import VerificationResult, Verifier


@pytest.fixture
def events() -> EventBus:
    return EventBus()


@pytest.fixture
async def env(tmp_path: Path, events: EventBus) -> Environment:
    cfg = EnvironmentConfig(runtime_type=RUNTIME_LOCAL, sandbox_mode=False, network=NetworkPolicy.ALLOW)
    e = Environment.create(tmp_path / "ws", cfg, events=events, session_id="t")
    await e.provision()
    yield e
    await e.destroy()


class TestRegressionChecker:
    async def test_regression_when_broader_fails_after_repair(self, env: Environment) -> None:
        # After a repair, broader verification failing is a regression.
        verifier = Verifier(environment=env, configured_command="false")
        checker = RegressionChecker(verifier=verifier, events=EventBus(), session_id="t")
        result = await checker.check(before_passed=False)
        assert result.regressed is True
        assert not result.after_passed

    async def test_regression_when_pass_to_fail(self, env: Environment) -> None:
        # before_passed=True (was passing), but verification now fails -> regression.
        verifier = Verifier(environment=env, configured_command="false")
        checker = RegressionChecker(verifier=verifier, events=EventBus(), session_id="t")
        result = await checker.check(before_passed=True)
        assert result.regressed is True
        assert not result.after_passed

    async def test_no_regression_when_passing(self, env: Environment) -> None:
        verifier = Verifier(environment=env, configured_command="true")
        checker = RegressionChecker(verifier=verifier, events=EventBus(), session_id="t")
        result = await checker.check(before_passed=False)
        assert not result.regressed
        assert result.after_passed

    async def test_regression_event_emitted(self, env: Environment) -> None:
        events = EventBus()
        verifier = Verifier(environment=env, configured_command="false")
        checker = RegressionChecker(verifier=verifier, events=events, session_id="t")
        await checker.check(before_passed=True)
        types = [e.type.value for e in events.history]
        assert "regression_detected" in types


class TestFinalReviewer:
    def _result(self, outcome: VerificationOutcome) -> VerificationResult:
        return VerificationResult(outcome=outcome, command="pytest", reason="r")

    def test_passes_on_clean_verification_and_changes(self) -> None:
        reviewer = FinalReviewer()
        change = ChangeAnalysis(
            changed=[type("C", (), {"path": "src/a.py", "status": "M", "is_added": False,
                                    "is_deleted": False, "is_modified": True,
                                    "to_dict": lambda self: {}})()],
            modified=["src/a.py"], empty=False,
        )
        result = reviewer.review(change=change, verification=self._result(VerificationOutcome.PASS))
        assert result.passed

    def test_fails_on_failed_verification(self) -> None:
        reviewer = FinalReviewer()
        change = ChangeAnalysis(modified=["a.py"], empty=False)
        result = reviewer.review(change=change, verification=self._result(VerificationOutcome.FAIL))
        assert not result.passed

    def test_fails_on_outside_workspace_changes(self) -> None:
        reviewer = FinalReviewer()
        change = ChangeAnalysis(
            modified=["a.py"], empty=False, outside_workspace=["../x.py"],
        )
        result = reviewer.review(change=change, verification=self._result(VerificationOutcome.PASS))
        assert not result.passed

    def test_fails_on_broad_change(self) -> None:
        reviewer = FinalReviewer()
        change = ChangeAnalysis(modified=["a.py"], empty=False, broad=True)
        result = reviewer.review(change=change, verification=self._result(VerificationOutcome.PASS))
        assert not result.passed

    def test_empty_diff_when_changes_expected_is_advisory(self) -> None:
        # Empty diff with changes_expected=True is a hard failure (diff_coherent).
        reviewer = FinalReviewer()
        change = ChangeAnalysis(empty=True)
        result = reviewer.review(change=change, verification=self._result(VerificationOutcome.PASS),
                                 changes_expected=True)
        assert not result.passed

    def test_generated_files_advisory_does_not_fail(self) -> None:
        reviewer = FinalReviewer()
        change = ChangeAnalysis(modified=["a.py"], empty=False, generated=["a.pyc"])
        result = reviewer.review(change=change, verification=self._result(VerificationOutcome.PASS))
        # Generated files alone is advisory -> still passes.
        assert result.passed

    def test_events_emitted(self) -> None:
        events = EventBus()
        reviewer = FinalReviewer(events=events, session_id="t")
        reviewer.review(change=ChangeAnalysis(modified=["a.py"], empty=False),
                        verification=self._result(VerificationOutcome.PASS))
        types = [e.type.value for e in events.history]
        assert "final_review_started" in types
        assert "final_review_completed" in types
