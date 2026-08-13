"""Centralized structured logging for KINETIC operational diagnostics.

This is distinct from the :class:`~kinetic.security.audit.AuditLog`:

* **logging** — operational diagnostics for engineers (what happened, when,
  in which session/task/workspace). Structured JSON records so logs can be
  searched, filtered, and correlated.
* **audit** — security/accountability records (permission decisions, tool
  invocations). Append-only, human-readable JSONL, never duplicated into
  normal logs.

Security: the log layer redacts credential-like values from every record's
message and data fields before emission, so secrets never appear in logs even
if a caller passes them inadvertently. The detector is the same one used by
the memory subsystem for consistency.

The layer is a thin wrapper around the stdlib ``logging`` package: a custom
``JsonFormatter`` plus a :func:`configure` entry point that installs it. No
heavyweight dependency.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from kinetic.memory.metadata import DEFAULT_SECRET_DETECTOR

#: The logger name everything in KINETIC logs under.
LOGGER_NAME = "kinetic"

#: Default level when :func:`configure` is called without one.
DEFAULT_LEVEL = logging.INFO

# Module-level holder for the correlation context (session/task/workspace).
# This is set per-run via :func:`bind_context` and read by the formatter so
# every log record carries the current identifiers without callers repeating
# them on every call.
_context: dict[str, str] = {}


def bind_context(
    *,
    session_id: str | None = None,
    task_id: str | None = None,
    workspace: str | None = None,
    environment_id: str | None = None,
) -> None:
    """Set correlation identifiers for subsequent log records.

    Identifiers that are ``None`` are left unchanged (not cleared), so a caller
    can progressively enrich the context. Use :func:`clear_context` to reset.
    """
    if session_id is not None:
        _context["session_id"] = session_id
    if task_id is not None:
        _context["task_id"] = task_id
    if workspace is not None:
        _context["workspace"] = workspace
    if environment_id is not None:
        _context["environment_id"] = environment_id


def clear_context() -> None:
    """Remove all correlation identifiers."""
    _context.clear()


def get_context() -> dict[str, str]:
    """Return a copy of the current correlation context."""
    return dict(_context)


class _SecretRedactingJsonFormatter(logging.Formatter):
    """Emit each log record as a JSON object with correlation IDs + redaction.

    Fields: ``ts``, ``level``, ``logger``, ``message``, plus any ``extra``
    keys the caller attached, plus the bound correlation context. Credential-
    like values in the message and in string-valued extras are masked.
    """

    _REDACTED = "<redacted>"

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=UTC).isoformat()
        payload: dict[str, Any] = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "message": self._redact(record.getMessage()),
        }
        # Merge correlation context.
        payload.update(_context)
        # Merge caller-supplied extras (skip stdlib's own attributes).
        std_attrs = set(vars(logging.LogRecord(
            name="", level=0, pathname="", lineno=0, msg="", args=None, exc_info=None
        )).keys())
        std_attrs |= {"message", "asctime"}
        for key, value in record.__dict__.items():
            if key not in std_attrs and not key.startswith("_"):
                payload[key] = self._redact(value)
        if record.exc_info:
            payload["exc_info"] = self._format_exception(record.exc_info)
        return json.dumps(payload, default=str)

    def _redact(self, value: Any) -> Any:
        """Mask credential-like substrings in strings; recurse into dicts/lists."""
        if isinstance(value, str):
            return self._redact_string(value)
        if isinstance(value, dict):
            return {k: self._redact(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._redact(v) for v in value]
        return value

    def _redact_string(self, text: str) -> str:
        result = text
        for match in DEFAULT_SECRET_DETECTOR.detect(text):
            original = match.original
            if original and original in result:
                result = result.replace(original, self._REDACTED)
        return result

    @staticmethod
    def _format_exception(exc_info: Any) -> str:
        import traceback

        return "".join(traceback.format_exception(*exc_info))


def configure(
    *,
    level: int | str = DEFAULT_LEVEL,
    stream: Any = None,
    force: bool = True,
) -> logging.Logger:
    """Install the JSON structured-logging formatter on the KINETIC logger.

    Idempotent: calling again reconfigures cleanly (old handlers removed). By
    default writes to ``stderr`` so stdout stays free for tool/agent output.
    """
    logger = logging.getLogger(LOGGER_NAME)
    if force:
        for h in list(logger.handlers):
            logger.removeHandler(h)
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(_SecretRedactingJsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a child logger under the KINETIC namespace."""
    if name is None or name == LOGGER_NAME:
        return logging.getLogger(LOGGER_NAME)
    if name.startswith(LOGGER_NAME + "."):
        return logging.getLogger(name)
    return logging.getLogger(f"{LOGGER_NAME}.{name}")


__all__ = [
    "LOGGER_NAME",
    "bind_context",
    "clear_context",
    "configure",
    "get_context",
    "get_logger",
]
