"""Phase 7 — structured logging tests.

Verifies the JSON structured-logging layer: consistent format, correlation IDs,
levels, timestamps, and that known secret fixtures NEVER appear in log output.
"""

from __future__ import annotations

import io
import json
import logging

import pytest

from observability.logging import (
    bind_context,
    clear_context,
    configure,
    get_logger,
)


@pytest.fixture
def log_stream() -> io.StringIO:
    stream = io.StringIO()
    configure(level=logging.DEBUG, stream=stream, force=True)
    clear_context()
    return stream


def _read_records(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


class TestStructuredFormat:
    def test_json_format(self, log_stream: io.StringIO) -> None:
        logger = get_logger("test")
        logger.info("hello world")
        records = _read_records(log_stream)
        assert len(records) == 1
        assert records[0]["message"] == "hello world"
        assert records[0]["level"] == "INFO"
        assert records[0]["logger"] == "kinetic.test"
        assert "ts" in records[0]

    def test_correlation_context(self, log_stream: io.StringIO) -> None:
        bind_context(session_id="sess-123", task_id="task-456", workspace="/tmp/ws")
        logger = get_logger()
        logger.info("with context")
        records = _read_records(log_stream)
        assert records[0]["session_id"] == "sess-123"
        assert records[0]["task_id"] == "task-456"
        assert records[0]["workspace"] == "/tmp/ws"
        clear_context()

    def test_clear_context(self, log_stream: io.StringIO) -> None:
        bind_context(session_id="s1")
        clear_context()
        get_logger().info("no context")
        records = _read_records(log_stream)
        assert "session_id" not in records[0]

    def test_extra_fields(self, log_stream: io.StringIO) -> None:
        logger = get_logger("test")
        logger.info("msg", extra={"tool": "run_command", "exit_code": 0})
        records = _read_records(log_stream)
        assert records[0]["tool"] == "run_command"
        assert records[0]["exit_code"] == 0

    def test_levels(self, log_stream: io.StringIO) -> None:
        logger = get_logger("test")
        logger.warning("warn")
        logger.error("err")
        records = _read_records(log_stream)
        assert records[0]["level"] == "WARNING"
        assert records[1]["level"] == "ERROR"

    def test_exception_info(self, log_stream: io.StringIO) -> None:
        logger = get_logger("test")
        try:
            raise ValueError("boom")
        except ValueError:
            logger.exception("caught")
        records = _read_records(log_stream)
        assert "exc_info" in records[0]
        assert "ValueError" in records[0]["exc_info"]


class TestSecretRedaction:
    """Known secret fixtures must NEVER appear in log output."""

    SECRET_FIXTURES = [
        "api_key=sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "ANTHROPIC_API_KEY=sk-ant-api03-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
        "password=supersecret12345678",
        "token=ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "AKIAABCDEFGHIJKLMNOP",
        "xoxb-1234567890-abcdefghij",
        "-----BEGIN RSA PRIVATE KEY-----\nfakekey\n-----END RSA PRIVATE KEY-----",
    ]

    @pytest.mark.parametrize("secret", SECRET_FIXTURES)
    def test_secret_not_in_message(self, log_stream: io.StringIO, secret: str) -> None:
        get_logger("test").info(f"processing: {secret}")
        records = _read_records(log_stream)
        raw = log_stream.getvalue()
        # The raw secret must not appear anywhere in the log output.
        assert secret not in raw
        assert "<redacted>" in records[0]["message"]

    def test_secret_in_extra_redacted(self, log_stream: io.StringIO) -> None:
        get_logger("test").info(
            "msg", extra={"credential": "api_key=sk-ant-api03-CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"}
        )
        raw = log_stream.getvalue()
        assert "sk-ant-api03-CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC" not in raw

    def test_secret_in_nested_dict(self, log_stream: io.StringIO) -> None:
        get_logger("test").info(
            "msg", extra={"data": {"token": "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"}}
        )
        raw = log_stream.getvalue()
        assert "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" not in raw
