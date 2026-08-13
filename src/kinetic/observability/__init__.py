"""Observability: structured logging + metrics for operational diagnostics.

This package is distinct from the security :class:`~kinetic.security.audit.AuditLog`.
Logging and metrics here are operational diagnostics; audit records remain the
authoritative security/accountability trail and are never duplicated into logs.
"""

from __future__ import annotations

from kinetic.observability.logging import (
    bind_context,
    clear_context,
    configure,
    get_context,
    get_logger,
)
from kinetic.observability.metrics import (
    MetricsCollector,
)

__all__ = [
    "MetricsCollector",
    "bind_context",
    "clear_context",
    "configure",
    "get_context",
    "get_logger",
]
