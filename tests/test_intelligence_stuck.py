"""Tests for the StuckDetector (Phase 6)."""

from __future__ import annotations

from events import EventBus
from intelligence.analyzer import FailureAnalyzer, failure_signature
from intelligence.models import RepairAttempt, RepairState
from intelligence.stuck import StuckDetector


def _analysis(command: str = "pytest", exit_code: int = 1, stdout: str = "FAILED tests/x.py::test_a\n1 failed"):
    return FailureAnalyzer(max_stdout_chars=200, max_stderr_chars=200).analyze(
        command=command, exit_code=exit_code, stdout=stdout,
    )


class TestStuckDetector:
    def test_not_stuck_with_few_attempts(self) -> None:
        det = StuckDetector(repeat_threshold=2)
        state = RepairState()
        assert not det.evaluate(state).stuck

    def test_stuck_on_identical_failure(self) -> None:
        det = StuckDetector(repeat_threshold=2, events=EventBus())
        a = _analysis()
        state = RepairState(attempts=[
            RepairAttempt(attempt=1, analysis=a),
            RepairAttempt(attempt=2, analysis=a),
        ])
        sig = det.evaluate(state)
        assert sig.stuck is True
        assert "identical" in sig.reason

    def test_not_stuck_when_failures_differ(self) -> None:
        det = StuckDetector(repeat_threshold=2)
        state = RepairState(attempts=[
            RepairAttempt(attempt=1, analysis=_analysis(stdout="FAILED tests/x.py::test_a\n1 failed")),
            RepairAttempt(attempt=2, analysis=_analysis(stdout="FAILED tests/x.py::test_b\n1 failed")),
        ])
        assert not det.evaluate(state).stuck

    def test_stuck_event_emitted(self) -> None:
        events = EventBus()
        det = StuckDetector(repeat_threshold=2, events=events, session_id="t")
        a = _analysis()
        state = RepairState(attempts=[
            RepairAttempt(attempt=1, analysis=a),
            RepairAttempt(attempt=2, analysis=a),
        ])
        det.evaluate(state)
        types = [e.type.value for e in events.history]
        assert "stuck_detected" in types

    def test_budget_exhausted(self) -> None:
        det = StuckDetector()
        assert det.is_budget_exhausted(attempts=3, max_attempts=3)
        assert not det.is_budget_exhausted(attempts=2, max_attempts=3)

    def test_signature_excludes_volatile_output(self) -> None:
        a1 = _analysis(stdout="FAILED tests/x.py::test_a\n1 failed\nlog noise AAAA")
        a2 = _analysis(stdout="FAILED tests/x.py::test_a\n1 failed\nlog noise BBBB")
        assert failure_signature(a1) == failure_signature(a2)
