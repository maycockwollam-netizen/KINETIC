"""Structured observation of execution results.

Observations are bounded: stdout/stderr are truncated to configured limits, and
secret-shaped content is masked before persistence/audit. Observations never
store unlimited model output or raw conversation turns — only a bounded summary
of what happened during a step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from kinetic.memory.metadata import SecretDetector


@dataclass
class Observation:
    """A bounded, secret-filtered snapshot of one step's outcome."""

    step_id: str
    exit_status: str = "unknown"
    exit_code: int | None = None
    stdout_summary: str = ""
    stderr_summary: str = ""
    changed_files: list[str] = field(default_factory=list)
    test_results: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    duration_ms: int = 0
    verification: str = "unknown"
    tool_calls: list[str] = field(default_factory=list)
    success: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "exit_status": self.exit_status,
            "exit_code": self.exit_code,
            "stdout_summary": self.stdout_summary,
            "stderr_summary": self.stderr_summary,
            "changed_files": list(self.changed_files),
            "test_results": dict(self.test_results),
            "errors": list(self.errors),
            "duration_ms": self.duration_ms,
            "verification": self.verification,
            "tool_calls": list(self.tool_calls),
            "success": self.success,
        }


class Observer:
    """Builds bounded, secret-filtered observations from raw execution data.

    Configurable output size limits prevent unbounded memory growth; the secret
    detector masks credential-like content so raw secrets never land in
    persisted observations or audit logs.
    """

    def __init__(
        self,
        *,
        max_stdout_chars: int = 4000,
        max_stderr_chars: int = 2000,
        secret_detector: SecretDetector | None = None,
    ) -> None:
        self._max_out = max_stdout_chars
        self._max_err = max_stderr_chars
        self._secrets = secret_detector or SecretDetector()

    @staticmethod
    def from_settings(s: object) -> Observer:
        return Observer(
            max_stdout_chars=getattr(s, "observation_max_stdout_chars", 4000),
            max_stderr_chars=getattr(s, "observation_max_stderr_chars", 2000),
        )

    def observe(
        self,
        *,
        step_id: str,
        result_text: str | None = None,
        exit_code: int | None = None,
        stdout: str = "",
        stderr: str = "",
        changed_files: list[str] | None = None,
        test_results: dict[str, Any] | None = None,
        errors: list[str] | None = None,
        duration_ms: int = 0,
        verification: str = "unknown",
        tool_calls: list[str] | None = None,
        success: bool | None = None,
    ) -> Observation:
        out = self._truncate(self._mask(stdout), self._max_out)
        err = self._truncate(self._mask(stderr), self._max_err)
        if success is None:
            success = exit_code == 0 and not errors
        return Observation(
            step_id=step_id,
            exit_status=("completed" if success else "failed"),
            exit_code=exit_code,
            stdout_summary=out,
            stderr_summary=err,
            changed_files=list(changed_files or []),
            test_results=dict(test_results or {}),
            errors=[self._mask(e) for e in (errors or [])],
            duration_ms=duration_ms,
            verification=verification,
            tool_calls=list(tool_calls or []),
            success=success,
        )

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        if limit and len(text) > limit:
            return text[:limit] + "\n…(truncated)"
        return text

    def _mask(self, text: str) -> str:
        """Replace secret-shaped substrings with a masked placeholder."""
        if not text:
            return text
        masked = text
        for m in self._secrets.detect(text):
            target = m.original or m.snippet
            if target:
                masked = masked.replace(target, "<secret-hidden>")
        return masked


def summarize(result_text: str | None, *, limit: int = 800) -> str:
    """A short, bounded summary of a model result (no raw chain-of-thought)."""
    if not result_text:
        return ""
    text = result_text.strip()
    if len(text) > limit:
        return text[:limit] + "\n…(truncated)"
    return text
