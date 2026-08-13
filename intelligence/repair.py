"""Bounded repair coordination.

When verification fails, the :class:`RepairCoordinator`:

  1. preserves the original failure,
  2. analyzes it (via :class:`~intelligence.analyzer.FailureAnalyzer`),
  3. builds a bounded repair context (no raw log dump — secret-masked, trimmed),
  4. asks the existing agent to perform a repair step — through the *same*
     ``AgentSession.query`` safe path used by the Phase 5 step runner (there is
     NO second agent loop, NO second ToolRegistry, NO second permission system),
  5. re-runs verification (through the existing :class:`~tasks.verifier.Verifier`
     → ``Environment.exec`` → permission boundary),
  6. repeats only within configured, bounded limits,
  7. performs stuck detection and (on success) a regression check.

The Claude Agent SDK remains responsible for model reasoning. KINETIC remains
responsible for orchestration, state, permissions, verification and recovery.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from events import EventBus, EventType
from intelligence.analyzer import FailureAnalyzer, failure_signature
from intelligence.models import (
    FailureAnalysis,
    RepairAttempt,
    RepairOutcome,
    RepairState,
)
from intelligence.stuck import StuckDetector
from security import AuditLog
from tasks.policies import VerificationOutcome

if TYPE_CHECKING:
    from intelligence.diff import ChangeAnalyzer
    from intelligence.regression import RegressionChecker
    from tasks.verifier import VerificationResult, Verifier


@runtime_checkable
class RepairRunner(Protocol):
    """Performs one repair step through the SAME safe path as the step runner.

    Implementations wrap :class:`~agent.session.AgentSession.query`
    (which goes through the adapter → ``can_use_tool`` → registry → policy →
    environment). This protocol adds NO new execution path.
    """

    async def repair(self, *, prompt: str, session_id: str) -> dict[str, Any]:
        """Run a repair prompt; return ``{result_text, exit_code, errors, success}``."""
        ...


class RepairContextBuilder:
    """Constructs a bounded, secret-free repair context for the model.

    Only relevant information is included: the failure summary, relevant test
    failures with file/line, bounded command output, recent changed files, and
    previous repair attempts. Nothing unbounded; everything secret-masked.
    """

    def __init__(
        self,
        *,
        max_chars: int = 8000,
        max_test_failures: int = 8,
        max_changed_files: int = 20,
    ) -> None:
        self._max_chars = max_chars
        self._max_test_failures = max_test_failures
        self._max_changed_files = max_changed_files

    @classmethod
    def from_settings(cls, s: object) -> RepairContextBuilder:
        return cls(
            max_chars=getattr(s, "repair_context_max_chars", 8000),
            max_test_failures=getattr(s, "repair_context_max_test_failures", 8),
            max_changed_files=getattr(s, "repair_context_max_changed_files", 20),
        )

    def build(
        self,
        *,
        analysis: FailureAnalysis,
        task_request: str = "",
        plan_goal: str = "",
        step_id: str | None = None,
        changed_files: list[str] | None = None,
        previous_attempts: list[RepairAttempt] | None = None,
    ) -> str:
        """Return a bounded repair prompt string."""
        lines = [
            "## Repair context",
            f"Failure class: {analysis.failure_class.value}",
        ]
        if analysis.command:
            lines.append(f"Verification command: {analysis.command}")
        if analysis.exit_code is not None:
            lines.append(f"Exit code: {analysis.exit_code}")
        if task_request:
            lines.append(f"Task: {task_request}")
        if plan_goal:
            lines.append(f"Plan goal: {plan_goal}")
        if step_id:
            lines.append(f"Failed step: {step_id}")
        if analysis.transient:
            lines.append("This failure may be transient.")

        # Relevant test failures (bounded).
        if analysis.test_failures:
            lines.append("\nFailing tests:")
            for tf in analysis.test_failures[: self._max_test_failures]:
                loc = tf.file
                if tf.line is not None:
                    loc = f"{tf.file}:{tf.line}" if tf.file else f":{tf.line}"
                line = f"- {tf.name}"
                if loc:
                    line += f" ({loc})"
                if tf.message:
                    line += f": {tf.message}"
                lines.append(line)
            extra = len(analysis.test_failures) - self._max_test_failures
            if extra > 0:
                lines.append(f"…({extra} more failure(s) omitted)")
        elif analysis.failure_count:
            lines.append(f"\n{analysis.failure_count} failure(s) reported (no structured names extracted).")

        # Bounded stderr (already masked by the analyzer).
        if analysis.stderr_bounded:
            lines.append("\nstderr (bounded, masked):")
            lines.append(analysis.stderr_bounded)

        # Recent changed files (bounded).
        if changed_files:
            shown = changed_files[-self._max_changed_files:]
            lines.append("\nRecently changed files:")
            lines.extend(f"- {f}" for f in shown)

        # Previous repair attempts (so the model does not repeat itself).
        if previous_attempts:
            lines.append("\nPrevious repair attempts:")
            for a in previous_attempts[-4:]:
                sig = failure_signature(a.analysis) if a.analysis else "(no analysis)"
                lines.append(f"- attempt {a.attempt}: {'success' if a.success else 'failed'} ({sig})")
            lines.append("Do NOT repeat the same failing action. Try a different approach.")

        lines.append(
            "\nUse the available tools to fix the failing verification. "
            "Do not introduce unrelated changes."
        )
        text = "\n".join(lines)
        if len(text) > self._max_chars:
            text = text[: self._max_chars] + "\n…(truncated)"
        return text


class RepairCoordinator:
    """Orchestrates the bounded repair loop.

    Dependencies are injected; this coordinator owns no execution path of its
    own — it asks the :class:`RepairRunner` (the same safe path) to repair and
    the :class:`Verifier` (Environment.exec) to re-verify.
    """

    def __init__(
        self,
        *,
        runner: RepairRunner,
        analyzer: FailureAnalyzer,
        verifier: Verifier,
        context_builder: RepairContextBuilder,
        stuck_detector: StuckDetector,
        regression_checker: RegressionChecker | None = None,
        change_analyzer: ChangeAnalyzer | None = None,
        max_repair_attempts: int = 2,
        max_verification_attempts: int = 3,
        events: EventBus,
        audit: AuditLog | None = None,
        session_id: str = "intelligence",
    ) -> None:
        self._runner = runner
        self._analyzer = analyzer
        self._verifier = verifier
        self._context = context_builder
        self._stuck = stuck_detector
        self._regression = regression_checker
        self._change = change_analyzer
        self._max_repair = max(0, max_repair_attempts)
        self._max_verify = max(1, max_verification_attempts)
        self._events = events
        self._audit = audit
        self._session_id = session_id

    async def repair(
        self,
        *,
        failed_verification: VerificationResult,
        task_request: str = "",
        plan_goal: str = "",
        step_id: str | None = None,
        workspace: str = "",
        project_id: str = "",
        state: RepairState | None = None,
    ) -> RepairOutcome:
        """Run the bounded repair loop and return a :class:`RepairOutcome`.

        The original failure is preserved in the returned state's attempt
        history. Permission/cancellation/invalid-plan failures are NOT repaired
        (they are terminal/security failures) — the loop returns immediately
        with a bounded failure outcome.
        """
        state = state or RepairState()
        # Analyze the original failure.
        analysis = self._analyzer.analyze(
            command=failed_verification.command or "",
            exit_code=failed_verification.exit_code,
            stdout=failed_verification.stdout,
            stderr=failed_verification.stderr,
            workspace=workspace,
            project_id=project_id,
            verification_failed=True,
        )
        state.last_failure_signature = failure_signature(analysis)

        # Terminal/security failures are never repaired.
        if analysis.is_terminal or not analysis.retryable:
            attempt = RepairAttempt(attempt=state.repair_count + 1, analysis=analysis, success=False,
                                    error="terminal/non-retryable failure; repair not attempted")
            state.attempts.append(attempt)
            self._emit_failed(analysis, "terminal failure; repair skipped")
            return RepairOutcome(success=False, state=state, final_analysis=analysis,
                                 reason=analysis.failure_class.value)

        # Fetch changed files for context (best-effort, never fatal).
        changed_files: list[str] = []
        if self._change is not None:
            try:
                change = await self._change.analyze()
                changed_files = change.modified + change.added
            except Exception:  # noqa: BLE001
                changed_files = []

        attempt_no = 0
        while attempt_no < self._max_repair:
            attempt_no += 1
            state.total_recovery_attempts += 1

            # Stuck detection (before attempting another identical repair).
            if self._stuck.evaluate(state).stuck:
                self._emit_failed(analysis, "stuck: identical failure repeated")
                return RepairOutcome(success=False, state=state, final_analysis=analysis,
                                     reason="stuck: identical failure repeated")

            # Verification budget.
            if state.verification_attempts >= self._max_verify:
                self._emit_failed(analysis, "verification attempt budget exhausted")
                return RepairOutcome(success=False, state=state, final_analysis=analysis,
                                     reason="verification attempt budget exhausted")

            prompt = self._context.build(
                analysis=analysis,
                task_request=task_request,
                plan_goal=plan_goal,
                step_id=step_id,
                changed_files=changed_files,
                previous_attempts=state.attempts,
            )
            self._events.emit(
                EventType.REPAIR_STARTED, self._session_id,
                attempt=attempt_no, failure_class=analysis.failure_class.value,
            )
            self._audit_repair("repair_started", attempt=attempt_no, allowed=True,
                               reason=analysis.failure_class.value)

            try:
                result = await self._runner.repair(prompt=prompt, session_id=self._session_id)
            except Exception as exc:  # noqa: BLE001 - bounded failure, never fabricated success
                attempt = RepairAttempt(
                    attempt=attempt_no, analysis=analysis,
                    repair_prompt_bounded=prompt[:1000], success=False, error=str(exc),
                )
                state.attempts.append(attempt)
                self._emit_failed(analysis, f"repair runner error: {exc}")
                return RepairOutcome(success=False, state=state, final_analysis=analysis,
                                     reason=f"repair runner error: {exc}")

            repair_success = bool(result.get("success", False))
            attempt = RepairAttempt(
                attempt=attempt_no, analysis=analysis,
                repair_prompt_bounded=prompt[:1000], success=repair_success,
                error="" if repair_success else str(result.get("errors") or result.get("result_text") or "repair did not succeed"),
            )
            state.attempts.append(attempt)

            if not repair_success:
                self._events.emit(EventType.REPAIR_FAILED, self._session_id, attempt=attempt_no)
                self._audit_repair("repair_failed", attempt=attempt_no, allowed=False,
                                   reason=attempt.error)
                continue

            self._events.emit(EventType.REPAIR_COMPLETED, self._session_id, attempt=attempt_no)
            self._audit_repair("repair_completed", attempt=attempt_no, allowed=True,
                               reason="repair step succeeded")

            # Re-verify.
            state.verification_attempts += 1
            self._events.emit(EventType.VERIFICATION_RETRY, self._session_id,
                              attempt=state.verification_attempts)
            try:
                reverify = await self._verifier.verify()
            except Exception as exc:  # noqa: BLE001
                self._emit_failed(analysis, f"re-verification error: {exc}")
                return RepairOutcome(success=False, state=state, final_analysis=analysis,
                                     reason=f"re-verification error: {exc}")

            if reverify.outcome is VerificationOutcome.PASS:
                # Regression check: run broader verification and compare.
                if self._regression is not None:
                    try:
                        reg = await self._regression.check(before_passed=False)
                    except Exception as exc:  # noqa: BLE001
                        self._emit_failed(analysis, f"regression check error: {exc}")
                        return RepairOutcome(success=False, state=state, final_analysis=analysis,
                                             reason=f"regression check error: {exc}")
                    if reg.regressed:
                        state.regression_detected = True
                        self._emit_failed(analysis, "regression detected after repair")
                        return RepairOutcome(success=False, state=state, final_analysis=analysis,
                                             regression=True, reason="regression detected after repair")
                return RepairOutcome(success=True, state=state, final_analysis=analysis,
                                     reason="repair succeeded and verification passed")

            # Re-verification failed: re-analyze the new failure for the next loop.
            analysis = self._analyzer.analyze(
                command=reverify.command or "",
                exit_code=reverify.exit_code,
                stdout=reverify.stdout,
                stderr=reverify.stderr,
                workspace=workspace,
                project_id=project_id,
                verification_failed=True,
            )
            state.last_failure_signature = failure_signature(analysis)

        self._emit_failed(analysis, "repair attempt budget exhausted")
        return RepairOutcome(success=False, state=state, final_analysis=analysis,
                             reason="repair attempt budget exhausted")

    # --- audit / events ----------------------------------------------------

    def _emit_failed(self, analysis: FailureAnalysis, reason: str) -> None:
        self._events.emit(
            EventType.REPAIR_FAILED, self._session_id,
            failure_class=analysis.failure_class.value, reason=reason,
        )
        self._audit_repair("repair_failed", attempt=0, allowed=False, reason=reason)

    def _audit_repair(self, action: str, *, attempt: int, allowed: bool, reason: str) -> None:
        if self._audit is None:
            return
        self._audit.record(
            session_id=self._session_id, action=action, allowed=allowed,
            reason=reason, detail={"attempt": attempt},
        )


__all__ = [
    "RepairContextBuilder",
    "RepairCoordinator",
    "RepairRunner",
]
