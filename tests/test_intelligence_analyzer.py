"""Tests for the FailureAnalyzer (Phase 6).

Covers failure classification, structured extraction, bounded output, and
secret masking. No execution, no model.
"""

from __future__ import annotations

from events import EventBus
from intelligence.analyzer import FailureAnalyzer, analysis_from_dict, failure_signature
from tasks.policies import FailureClass


def _analyzer(events: EventBus | None = None) -> FailureAnalyzer:
    return FailureAnalyzer(
        max_stdout_chars=200, max_stderr_chars=200, events=events, session_id="t",
    )


class TestClassification:
    def test_test_failure_from_pytest_output(self) -> None:
        a = _analyzer().analyze(
            command="uv run pytest -q",
            exit_code=1,
            stdout="FAILED tests/test_app.py::test_add - assert 1==3\n1 failed",
            verification_failed=True,
        )
        assert a.failure_class is FailureClass.TEST_FAILURE
        assert a.failure_count == 1
        assert a.test_failures[0].name == "test_add"

    def test_build_failure(self) -> None:
        a = _analyzer().analyze(command="make build", exit_code=2, stderr="error: build failed")
        assert a.failure_class is FailureClass.BUILD_FAILURE

    def test_lint_failure(self) -> None:
        a = _analyzer().analyze(command="ruff check .", exit_code=1, stderr="E501 line too long")
        assert a.failure_class is FailureClass.LINT_FAILURE

    def test_dependency_failure(self) -> None:
        a = _analyzer().analyze(
            command="pip install foo", exit_code=1, stderr="no matching distribution found for foo"
        )
        assert a.failure_class is FailureClass.DEPENDENCY_FAILURE

    def test_timeout_failure(self) -> None:
        a = _analyzer().analyze(command="pytest", exit_code=124, stderr="timed out")
        assert a.failure_class is FailureClass.TIMEOUT
        assert a.transient is True

    def test_permission_denied_failure(self) -> None:
        a = _analyzer().analyze(command="pytest", exit_code=1, stderr="permission denied")
        assert a.failure_class is FailureClass.PERMISSION_DENIED
        assert a.is_terminal
        assert a.retryable is False

    def test_environment_failure(self) -> None:
        a = _analyzer().analyze(command="pytest", exit_code=1, stderr="runtime unavailable")
        assert a.failure_class is FailureClass.ENVIRONMENT_FAILURE

    def test_cancellation(self) -> None:
        a = _analyzer().analyze(command="pytest", exit_code=1, stderr="cancelled by user")
        assert a.failure_class is FailureClass.CANCELLATION
        assert a.is_terminal

    def test_inconclusive(self) -> None:
        a = _analyzer().analyze(command="", stderr="no verification command available")
        assert a.failure_class is FailureClass.VERIFICATION_INCONCLUSIVE
        assert a.retryable is False

    def test_generic_command_failure(self) -> None:
        a = _analyzer().analyze(command="some-tool", exit_code=3, stderr="boom")
        assert a.failure_class in (FailureClass.COMMAND_FAILURE, FailureClass.TOOL_FAILURE, FailureClass.UNKNOWN)


class TestBoundingAndMasking:
    def test_output_is_truncated(self) -> None:
        long = "line of output\n" * 500
        a = _analyzer().analyze(command="pytest", exit_code=1, stdout=long, stderr=long)
        assert len(a.stdout_bounded) < len(long)
        assert "truncated" in a.stdout_bounded
        assert len(a.stderr_bounded) < len(long)

    def test_secret_is_masked(self) -> None:
        a = _analyzer().analyze(
            command="pytest",
            exit_code=1,
            stderr="api_key=sk-1234567890abcdef1234567890abcdef test failed",
        )
        assert "sk-1234567890abcdef1234567890abcdef" not in a.stderr_bounded
        assert "<secret-hidden>" in a.stderr_bounded

    def test_to_dict_is_serializable(self) -> None:
        a = _analyzer().analyze(command="pytest", exit_code=1, stdout="1 failed")
        d = a.to_dict()
        assert d["failure_class"] == "test_failure"
        assert isinstance(d["test_failures"], list)


class TestSignatureAndRestore:
    def test_signature_stable_across_identical_failures(self) -> None:
        a1 = _analyzer().analyze(command="pytest", exit_code=1, stdout="FAILED tests/x.py::test_a\n1 failed")
        a2 = _analyzer().analyze(command="pytest", exit_code=1, stdout="FAILED tests/x.py::test_a\n1 failed")
        assert failure_signature(a1) == failure_signature(a2)

    def test_signature_differs_for_different_tests(self) -> None:
        a1 = _analyzer().analyze(command="pytest", exit_code=1, stdout="FAILED tests/x.py::test_a\n1 failed")
        a2 = _analyzer().analyze(command="pytest", exit_code=1, stdout="FAILED tests/x.py::test_b\n1 failed")
        assert failure_signature(a1) != failure_signature(a2)

    def test_round_trip_through_dict(self) -> None:
        a = _analyzer().analyze(command="pytest", exit_code=1, stdout="FAILED tests/x.py::test_a\n1 failed")
        restored = analysis_from_dict(a.to_dict())
        assert restored.failure_class is a.failure_class
        assert restored.command == a.command
        assert restored.failure_count == a.failure_count


class TestEvents:
    def test_failure_analyzed_event_emitted(self) -> None:
        events = EventBus()
        _analyzer(events).analyze(command="pytest", exit_code=1, stderr="1 failed")
        types = [e.type.value for e in events.history]
        assert "failure_analyzed" in types
