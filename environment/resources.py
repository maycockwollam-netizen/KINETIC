"""Resource limits for sandboxed environments.

Limits are enforced by the runtime layer where supported. If a runtime cannot
enforce a requested limit it must raise rather than silently ignoring it —
``ResourceLimits.unsupported_for`` helps runtimes report this honestly.
"""

from __future__ import annotations

from dataclasses import dataclass

# CPU quota: number of whole CPUs (1.0 = one full core). ``None`` = no limit.
CPU_SHARE = float
# Memory ceiling in bytes. ``None`` = no limit.
MEM_BYTES = int


@dataclass(frozen=True)
class ResourceLimits:
    """Configurable resource ceilings for an environment.

    A ``None`` (or zero) field means "no limit requested". A runtime that
    cannot enforce a *requested* (non-None) limit must fail closed.
    """

    cpu: float | None = None
    memory_bytes: int | None = None
    process_count: int | None = None
    execution_timeout: float | None = None
    disk_bytes: int | None = None

    def requested_fields(self) -> list[str]:
        """Names of limits that were explicitly requested (non-None, non-zero)."""
        out: list[str] = []
        if self.cpu:
            out.append("cpu")
        if self.memory_bytes:
            out.append("memory_bytes")
        if self.process_count:
            out.append("process_count")
        if self.execution_timeout:
            out.append("execution_timeout")
        if self.disk_bytes:
            out.append("disk_bytes")
        return out

    @staticmethod
    def default() -> ResourceLimits:
        """Safe defaults: a bounded execution timeout only, no other ceiling."""
        return ResourceLimits(execution_timeout=600.0)
