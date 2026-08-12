"""Permission policy: decides whether a tool call may proceed.

This is the runtime security boundary. It is called from the SDK's
`can_use_tool` hook, so the decision is enforced *before* the tool runs,
regardless of what the model was told in the prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from kinetic.errors import PermissionDeniedError
from kinetic.security.policy import Capability, ToolPermission


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str

    @staticmethod
    def allow() -> Decision:
        return Decision(True, "allowed")

    @staticmethod
    def deny(reason: str) -> Decision:
        return Decision(False, reason)


class PermissionPolicy:
    """Decides tool-call permission based on capabilities + global settings.

    Future phases will extend this with per-tool argument inspection (path
    traversal checks, command allowlists, network gating). The boundary stays
    here, not in the agent prompt.
    """

    def __init__(
        self,
        *,
        writable_roots: list[Path] | None = None,
        allow_network: bool = False,
        allow_execute: bool = True,
    ) -> None:
        self._writable_roots = [r.resolve() for r in (writable_roots or [])]
        self._allow_network = allow_network
        self._allow_execute = allow_execute

    def evaluate(self, tool_name: str, permission: ToolPermission, tool_input: dict) -> Decision:
        caps = permission.capabilities

        if Capability.NETWORK in caps and not self._allow_network:
            return Decision.deny("network access is disabled")

        if Capability.EXECUTE in caps and not self._allow_execute:
            return Decision.deny("command execution is disabled")

        if Capability.WRITE_FS in caps:
            target = tool_input.get("path") or tool_input.get("file_path")
            if target is not None and self._writable_roots and not self._is_within_writable(
                Path(str(target))
            ):
                return Decision.deny(f"path outside writable roots: {target}")

        return Decision.allow()

    def require(self, tool_name: str, permission: ToolPermission, tool_input: dict) -> None:
        """Raise PermissionDeniedError if the call is not allowed."""
        decision = self.evaluate(tool_name, permission, tool_input)
        if not decision.allowed:
            raise PermissionDeniedError(tool_name, decision.reason)

    def _is_within_writable(self, path: Path) -> bool:
        # Relative paths are resolved against each writable root (the workspace),
        # since the agent never sees an absolute path outside its root.
        if not path.is_absolute():
            for root in self._writable_roots:
                resolved = (root / path).resolve()
                try:
                    resolved.relative_to(root)
                    return True
                except ValueError:
                    continue
            return False
        try:
            resolved = path.expanduser().resolve()
        except (OSError, RuntimeError):
            return False
        for root in self._writable_roots:
            try:
                resolved.relative_to(root)
                return True
            except ValueError:
                continue
        return False
