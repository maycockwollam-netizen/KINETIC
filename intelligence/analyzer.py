"""Structured failure analysis.

The :class:`FailureAnalyzer` turns a failed verification/step result into a
structured, bounded, secret-masked :class:`FailureAnalysis`. It combines the
Phase 5 :func:`~tasks.policies.classify_failure` heuristic with the
Phase 6 test-output parsers, and always produces bounded output — it never
passes unbounded logs or raw secrets onward to persistence, audit, or the
model.
"""

from __future__ import annotations

from typing import Any

from events import EventBus, EventType
from intelligence.models import FailureAnalysis, TestFailureInfo
from intelligence.parsers import analyze_test_output
from memory.metadata import SecretDetector
from security import AuditLog
from tasks.policies import FailureClass, classify_failure


class FailureAnalyzer:
    """Analyzes a failure into a structured, bounded, secret-safe record.

    All captured stdout/stderr is truncated to configured limits and run
    through the secret detector so credential-like content is masked before the
    analysis is persisted, audited, or exposed to the model.
    """

    def __init__(
        self,
        *,
        max_stdout_chars: int = 4000,
        max_stderr_chars: int = 2000,
        secret_detector: SecretDetector | None = None,
        events: EventBus | None = None,
        audit: AuditLog | None = None,
        session_id: str = "intelligence",
    ) -> None:
        self._max_out = max_stdout_chars
        self._max_err = max_stderr_chars
        self._secrets = secret_detector or SecretDetector()
        self._events = events
        self._audit = audit
        self._session_id = session_id

    @classmethod
    def from_settings(cls, s: object, *, events: EventBus | None = None,
                      audit: AuditLog | None = None, session_id: str = "intelligence") -> FailureAnalyzer:
        return cls(
            max_stdout_chars=getattr(s, "observation_max_stdout_chars", 4000),
            max_stderr_chars=getattr(s, "observation_max_stderr_chars", 2000),
            events=events,
            audit=audit,
            session_id=session_id,
        )

    def analyze(
        self,
        *,
        command: str = "",
        tool: str = "",
        exit_code: int | None = None,
        stdout: str = "",
        stderr: str = "",
        workspace: str = "",
        project_id: str = "",
        error_message: str = "",
        verification_failed: bool = False,
        is_build: bool = False,
        override_class: FailureClass | None = None,
    ) -> FailureAnalysis:
        """Produce a bounded, secret-masked :class:`FailureAnalysis`."""
        out = self._truncate(self._mask(stdout), self._max_out)
        err = self._truncate(self._mask(stderr), self._max_err)
        masked_msg = self._mask(error_message) if error_message else err

        failure_class = override_class or classify_failure(
            exit_code=exit_code,
            stderr=err,
            error_message=masked_msg,
            verification_failed=verification_failed,
            is_build=is_build,
        )
        failure_class = self._refine_class(failure_class, command, out, err, masked_msg)

        # Extract structured test failures from the combined output.
        combined = out + "\n" + err
        parsed = analyze_test_output(combined, command=command, exit_code=exit_code)

        transient = self._is_transient(failure_class, parsed.failure_count)
        retryable = self._is_retryable(failure_class, transient)

        analysis = FailureAnalysis(
            failure_class=failure_class,
            command=command,
            tool=tool,
            exit_code=exit_code,
            stdout_bounded=out,
            stderr_bounded=err,
            workspace=workspace,
            project_id=project_id,
            transient=transient,
            retryable=retryable,
            test_failures=parsed.failures,
            failure_count=parsed.failure_count,
            diagnostic={
                "runner": parsed.runner,
                "extracted_failures": len(parsed.failures),
            },
        )
        self._emit(analysis)
        return analysis

    # --- classification refinement -----------------------------------------

    @staticmethod
    def _refine_class(
        cls: FailureClass, command: str, stdout: str, stderr: str, message: str
    ) -> FailureClass:
        """Refine the heuristic class using command/output signals."""
        combined = f"{stdout}\n{stderr}\n{message}".lower()
        cmd = (command or "").lower()
        # Cancellation / timeout / permission are authoritative when present.
        if cls in (FailureClass.CANCELLATION, FailureClass.TIMEOUT, FailureClass.PERMISSION_DENIED):
            return cls
        if "cancelled" in combined or "aborted" in combined:
            return FailureClass.CANCELLATION
        if "timed out" in combined or "timeout" in combined:
            return FailureClass.TIMEOUT
        if "permission denied" in combined or "is disabled" in combined:
            return FailureClass.PERMISSION_DENIED
        # Lint / type-check commands.
        if any(k in cmd for k in ("ruff", "flake8", "pylint", "mypy", "eslint", "tsc", "type-check")):
            return FailureClass.LINT_FAILURE
        if any(k in combined for k in ("lint", "type error", "type-check", "mypy error")) and cls is FailureClass.UNKNOWN:
            return FailureClass.LINT_FAILURE
        # Dependency install commands.
        if any(k in cmd for k in ("pip install", "uv add", "uv sync", "npm install", "yarn add", "cargo add")):
            return FailureClass.DEPENDENCY_FAILURE
        if "could not find a version" in combined or "no matching distribution" in combined:
            return FailureClass.DEPENDENCY_FAILURE
        # Build commands.
        if any(k in cmd for k in ("cargo build", "make", "gcc", "tsc --build", "npm run build")) and cls in (
            FailureClass.UNKNOWN,
            FailureClass.TOOL_FAILURE,
        ):
            return FailureClass.BUILD_FAILURE
        # Environment / sandbox signals.
        if any(k in combined for k in ("sandbox", "runtime unavailable", "container", "docker")):
            return FailureClass.ENVIRONMENT_FAILURE
        # Verification inconclusive (no command / cannot run).
        if "inconclusive" in combined or "no verification command" in combined:
            return FailureClass.VERIFICATION_INCONCLUSIVE
        # Test-runner commands produce test failures.
        if any(k in cmd for k in ("pytest", "test", "cargo test", "go test", "vitest", "jest", "mocha")) and cls in (
            FailureClass.UNKNOWN,
            FailureClass.TOOL_FAILURE,
        ):
            return FailureClass.TEST_FAILURE
        return cls

    @staticmethod
    def _is_transient(cls: FailureClass, failure_count: int | None) -> bool:
        """Heuristic: environment/timeout failures may be transient."""
        return cls in (FailureClass.ENVIRONMENT_FAILURE, FailureClass.TIMEOUT)

    @staticmethod
    def _is_retryable(cls: FailureClass, transient: bool) -> bool:
        """A failure is retryable unless it is a terminal/security/cancel class."""
        return cls not in (
            FailureClass.PERMISSION_DENIED,
            FailureClass.CANCELLATION,
            FailureClass.INVALID_PLAN,
            FailureClass.VERIFICATION_INCONCLUSIVE,
        )

    # --- bounding + masking ------------------------------------------------

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        if limit and len(text) > limit:
            return text[:limit] + "\n…(truncated)"
        return text

    def _mask(self, text: str) -> str:
        if not text:
            return text
        masked = text
        for m in self._secrets.detect(text):
            target = m.original or m.snippet
            if target:
                masked = masked.replace(target, "<secret-hidden>")
        return masked

    # --- events + audit ----------------------------------------------------

    def _emit(self, analysis: FailureAnalysis) -> None:
        if self._events is not None:
            self._events.emit(
                EventType.FAILURE_ANALYZED,
                self._session_id,
                failure_class=analysis.failure_class.value,
                command=analysis.command,
                exit_code=analysis.exit_code,
                failure_count=analysis.failure_count,
                transient=analysis.transient,
                retryable=analysis.retryable,
            )
        if self._audit is not None:
            self._audit.record(
                session_id=self._session_id,
                action="failure_analyzed",
                allowed=True,
                reason=analysis.failure_class.value,
                detail={
                    "command": analysis.command,
                    "exit_code": analysis.exit_code,
                    "failure_count": analysis.failure_count,
                    # Never log raw stdout/stderr; only the class + counts.
                },
            )


def failure_signature(analysis: FailureAnalysis) -> str:
    """A stable, bounded signature for stuck detection.

    Two failures with the same signature are considered "the same failure" for
    stuck-detection purposes. The signature deliberately excludes volatile
    output (stdout/stderr text) — only the class, command, failing tests, and
    exit code are used so a genuine repeat is detected while incidental log
    noise is ignored.
    """
    test_names = ",".join(sorted({f.name for f in analysis.test_failures}))
    return f"{analysis.failure_class.value}|{analysis.command}|{analysis.exit_code}|{test_names}"


def analysis_from_dict(data: dict[str, Any]) -> FailureAnalysis:
    """Rebuild a :class:`FailureAnalysis` from a checkpoint dict."""
    tfs = [
        TestFailureInfo(**t) if isinstance(t, dict) else t  # type: ignore[arg-type]
        for t in (data.get("test_failures") or [])
    ]
    return FailureAnalysis(
        failure_class=FailureClass(data.get("failure_class", "unknown")),
        command=data.get("command", ""),
        tool=data.get("tool", ""),
        exit_code=data.get("exit_code"),
        stdout_bounded=data.get("stdout_bounded", ""),
        stderr_bounded=data.get("stderr_bounded", ""),
        workspace=data.get("workspace", ""),
        project_id=data.get("project_id", ""),
        transient=data.get("transient", False),
        retryable=data.get("retryable", True),
        test_failures=tfs,
        failure_count=data.get("failure_count"),
        diagnostic=data.get("diagnostic", {}),
        timestamp=data.get("timestamp", ""),
    )
