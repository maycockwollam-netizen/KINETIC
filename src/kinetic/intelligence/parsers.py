"""Test/build output parsers.

Pure, regex-based parsers over bounded command output. They extract structured
:class:`~kinetic.intelligence.models.TestFailureInfo` from common test runners
where practical, and gracefully fall back to generic failure information for
unknown formats.

These functions never execute anything — they only analyze text. They are
deliberately tolerant: a partial match is better than crashing, and an
unrecognized format yields an empty list (the caller falls back to generic
failure info).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from kinetic.intelligence.models import TestFailureInfo


@dataclass
class ParsedTestOutput:
    """Structured result of parsing test runner output."""

    failures: list[TestFailureInfo]
    failure_count: int | None
    runner: str  # detected runner name, or "generic"


def parse_pytest(output: str) -> ParsedTestOutput:
    """Parse pytest output.

    Recognizes the "short test summary info" section (``FAILED ...``) and the
    trailing ``X failed, Y passed`` summary line. Best-effort file/line
    extraction from assertion lines.
    """
    failures: list[TestFailureInfo] = []
    seen: set[str] = set()

    # "FAILED tests/test_x.py::test_name - AssertionError: ..."
    failed_re = re.compile(r"^FAILED\s+(\S+?)(?:::([^\s]+))?\s*(?:-\s*(.*))?$", re.MULTILINE)
    for m in failed_re.finditer(output):
        path = m.group(1)
        name = m.group(2) or path
        message = (m.group(3) or "").strip()
        loc = _extract_file_line(message)
        file_path, line = (loc if loc else (None, None))
        if file_path is None:
            loc2 = _find_assertion_location(output, path, name)
            if loc2:
                file_path, line = loc2
        key = f"{path}::{name}"
        if key in seen:
            continue
        seen.add(key)
        failures.append(TestFailureInfo(name=name, file=file_path or path, line=line, message=message))

    failure_count = _pytest_failure_count(output)

    # If no structured failures found but output indicates failure, record one
    # generic entry so the caller knows parsing ran.
    if not failures and failure_count and failure_count > 0:
        failures.append(TestFailureInfo(name="(pytest failure)", message=_first_error_line(output)))

    return ParsedTestOutput(failures=failures, failure_count=failure_count, runner="pytest")


def _pytest_failure_count(output: str) -> int | None:
    m = re.search(r"(\d+)\s+failed", output)
    if m:
        return int(m.group(1))
    return None


def parse_npm(output: str) -> ParsedTestOutput:
    """Parse ``npm test`` / jest-style output.

    Recognizes jest's "✗ test name" and "FAIL  path/to/file" lines plus the
    "Tests: X failed, Y passed" summary.
    """
    failures: list[TestFailureInfo] = []
    seen: set[str] = set()

    # jest failing test: "  ✗ test name" or "✕ test name"
    for m in re.finditer(r"^[ \t]*[✗✕]\s+(.+)$", output, re.MULTILINE):
        name = m.group(1).strip()
        # Strip trailing jest timing annotations like "(2 ms)".
        name = re.sub(r"\s*\([\d.]+\s*\w+\)\s*$", "", name)
        if name in seen:
            continue
        seen.add(name)
        failures.append(TestFailureInfo(name=name))

    # "FAIL  src/file.test.js"
    fail_files: list[str] = re.findall(r"^FAIL\s+(\S+)", output, re.MULTILINE)

    failure_count = _npm_failure_count(output)

    # Attach files to failures where possible.
    if fail_files and failures:
        for i, f in enumerate(failures):
            if not f.file:
                f_file = fail_files[min(i, len(fail_files) - 1)]
                object.__setattr__(f, "file", f_file)  # noqa: PLY020 - frozen dataclass field set

    if not failures and failure_count and failure_count > 0:
        failures.append(TestFailureInfo(name="(npm test failure)", message=_first_error_line(output)))

    return ParsedTestOutput(failures=failures, failure_count=failure_count, runner="npm")


def _npm_failure_count(output: str) -> int | None:
    m = re.search(r"(\d+)\s+fail(?:ing|ed)", output, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


def parse_cargo(output: str) -> ParsedTestOutput:
    """Parse ``cargo test`` output.

    Recognizes "test foo ... FAILED" lines and the "test result: FAILED.
    X passed; Y failed;" summary.
    """
    failures: list[TestFailureInfo] = []
    seen: set[str] = set()

    for m in re.finditer(r"^test\s+(\S+)\s+\.\.\.\s+FAILED", output, re.MULTILINE):
        name = m.group(1)
        if name in seen:
            continue
        seen.add(name)
        failures.append(TestFailureInfo(name=name))

    failure_count = _cargo_failure_count(output)

    if not failures and "FAILED" in output:
        failures.append(TestFailureInfo(name="(cargo test failure)", message=_first_error_line(output)))

    return ParsedTestOutput(failures=failures, failure_count=failure_count, runner="cargo")


def _cargo_failure_count(output: str) -> int | None:
    m = re.search(r"test result:\s*FAILED\.\s*\d+\s+passed;\s*(\d+)\s+failed", output)
    if m:
        return int(m.group(1))
    return None


def parse_go(output: str) -> int | None:
    """Parse ``go test`` output.

    Recognizes "--- FAIL: TestName (0.00s)" and "FAIL\\tpackage" lines.
    """
    failures: list[TestFailureInfo] = []
    seen: set[str] = set()

    for m in re.finditer(r"---\s+FAIL:\s+(\S+)", output, re.MULTILINE):
        name = m.group(1)
        if name in seen:
            continue
        seen.add(name)
        failures.append(TestFailureInfo(name=name))

    fail_pkgs = re.findall(r"^FAIL\s+(\S+)", output, re.MULTILINE)
    if fail_pkgs and failures:
        pkg = fail_pkgs[0]
        for f in failures:
            object.__setattr__(f, "file", pkg)  # noqa: PLY020 - frozen dataclass field set

    failure_count = None
    if failures:
        failure_count = len(failures)

    if not failures and "FAIL" in output:
        failures.append(TestFailureInfo(name="(go test failure)", message=_first_error_line(output)))

    return ParsedTestOutput(failures=failures, failure_count=failure_count, runner="go")


def parse_generic(output: str, *, exit_code: int | None = None) -> ParsedTestOutput:
    """Fallback parser for unrecognized command failures.

    Extracts nothing structured; records a single generic failure so the caller
    has a bounded representation. Never raises.
    """
    message = _first_error_line(output)
    name = f"command exited {exit_code}" if exit_code is not None else "command failed"
    failures = [TestFailureInfo(name=name, message=message)] if message else []
    return ParsedTestOutput(failures=failures, failure_count=None, runner="generic")


def analyze_test_output(
    output: str, *, command: str = "", exit_code: int | None = None
) -> ParsedTestOutput:
    """Dispatch to the appropriate parser based on command/output heuristics.

    Falls back to :func:`parse_generic` for unknown formats. Never raises.
    """
    cmd = (command or "").lower()
    text = output or ""
    if "pytest" in cmd or "pytest" in text.lower()[:200]:
        return parse_pytest(text)
    if "npm test" in cmd or "jest" in cmd or "vitest" in cmd or "mocha" in cmd:
        return parse_npm(text)
    if "cargo test" in cmd or "cargo " in cmd:
        return parse_cargo(text)
    if "go test" in cmd:
        return parse_go(text)
    return parse_generic(text, exit_code=exit_code)


# --- helpers --------------------------------------------------------------


def _extract_file_line(message: str) -> tuple[str, int | None] | None:
    """Extract a file path and optional line number from an error message."""
    # "path/to/file.py:23: AssertionError"
    m = re.search(r"([^\s:]+\.\w+):(\d+)(?::\d+)?:", message)
    if m:
        return m.group(1), int(m.group(2))
    m = re.search(r"([^\s:]+\.\w+):(\d+)", message)
    if m:
        return m.group(1), int(m.group(2))
    return None


def _find_assertion_location(output: str, path: str, name: str) -> tuple[str, int | None] | None:
    """Search the output for an assertion line like ``file.py:NN:``.

    Pytest prints the assertion location on a separate line under the failure
    header. We look for any ``path:line:`` occurrence in the output.
    """
    file_part = path.split("::", 1)[0]
    m = re.search(re.escape(file_part) + r":(\d+):", output)
    if m:
        return file_part, int(m.group(1))
    return None


def _first_error_line(output: str, *, limit: int = 500) -> str:
    """Return the first non-empty, error-looking line, bounded."""
    if not output:
        return ""
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        low = stripped.lower()
        if any(k in low for k in ("error", "fail", "exception", "traceback", "panic")):
            return stripped[:limit]
    # Fall back to first non-empty line.
    for line in output.splitlines():
        if line.strip():
            return line.strip()[:limit]
    return ""
