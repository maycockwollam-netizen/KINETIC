"""Tests for the test-output parsers (Phase 6).

Pure-function tests — no execution, no model. Cover pytest/npm/cargo/go/generic
extraction plus graceful fallback for unknown formats.
"""

from __future__ import annotations

from kinetic.intelligence.parsers import (
    analyze_test_output,
    parse_cargo,
    parse_generic,
    parse_go,
    parse_npm,
    parse_pytest,
)

PYTEST_OUTPUT = """\
============================= test session starts ==============================
collected 2 items

tests/test_app.py::test_add F
tests/test_app.py::test_sub .

=================================== FAILURES ===================================
______________________________ test_add ____________________________________
    assert 1 + 1 == 3
tests/test_app.py:12: AssertionError
=========================== short test summary info ============================
FAILED tests/test_app.py::test_add - assert 1 + 1 == 3
============================== 1 failed, 1 passed in 0.5s ===============================
"""

NPM_OUTPUT = """\
FAIL  src/app.test.js
  ✗ adds numbers (2 ms)
  ✕ subtracts numbers
Tests: 2 failed, 3 passed, 5 total
"""

CARGO_OUTPUT = """\
running 2 tests
test test_add ... ok
test test_sub ... FAILED

failures:

---- test_sub stdout ----
assertion failed

test result: FAILED. 1 passed; 1 failed; 0 ignored
"""

GO_OUTPUT = """\
--- FAIL: TestAdd (0.00s)
    main_test.go:10: assertion failed
FAIL\texample.com/pkg\t0.5s
"""


class TestPytestParser:
    def test_extracts_failing_tests(self) -> None:
        result = parse_pytest(PYTEST_OUTPUT)
        assert result.runner == "pytest"
        assert result.failure_count == 1
        assert len(result.failures) == 1
        f = result.failures[0]
        assert f.name == "test_add"
        assert "test_app.py" in f.file
        assert f.line == 12

    def test_no_failures_returns_empty(self) -> None:
        result = parse_pytest("1 passed in 0.1s")
        assert result.failure_count is None
        assert result.failures == []

    def test_generic_fallback_when_failed_but_no_structured(self) -> None:
        result = parse_pytest("2 failed in 0.1s\nsome error")
        assert result.failure_count == 2
        assert len(result.failures) >= 1


class TestNpmParser:
    def test_extracts_failing_tests(self) -> None:
        result = parse_npm(NPM_OUTPUT)
        assert result.runner == "npm"
        assert result.failure_count == 2
        names = [f.name for f in result.failures]
        assert "adds numbers" in names
        assert "subtracts numbers" in names
        # File attached from FAIL line.
        assert any(f.file == "src/app.test.js" for f in result.failures)


class TestCargoParser:
    def test_extracts_failing_tests(self) -> None:
        result = parse_cargo(CARGO_OUTPUT)
        assert result.runner == "cargo"
        assert result.failure_count == 1
        assert result.failures[0].name == "test_sub"


class TestGoParser:
    def test_extracts_failing_tests(self) -> None:
        result = parse_go(GO_OUTPUT)
        assert result.runner == "go"
        assert result.failures[0].name == "TestAdd"
        assert result.failures[0].file == "example.com/pkg"


class TestGenericParser:
    def test_generic_records_failure(self) -> None:
        result = parse_generic("error: something broke\nexit code 1", exit_code=1)
        assert result.runner == "generic"
        assert len(result.failures) == 1
        assert "1" in result.failures[0].name

    def test_generic_empty_output(self) -> None:
        result = parse_generic("", exit_code=1)
        assert result.failures == []


class TestDispatch:
    def test_dispatch_pytest(self) -> None:
        result = analyze_test_output(PYTEST_OUTPUT, command="uv run pytest -q")
        assert result.runner == "pytest"

    def test_dispatch_npm(self) -> None:
        result = analyze_test_output(NPM_OUTPUT, command="npm test")
        assert result.runner == "npm"

    def test_dispatch_cargo(self) -> None:
        result = analyze_test_output(CARGO_OUTPUT, command="cargo test")
        assert result.runner == "cargo"

    def test_dispatch_go(self) -> None:
        result = analyze_test_output(GO_OUTPUT, command="go test ./...")
        assert result.runner == "go"

    def test_dispatch_generic_for_unknown(self) -> None:
        result = analyze_test_output("weird runner output", command="custom-runner")
        assert result.runner == "generic"
