"""Git tools.

All Git operations run inside the managed workspace, pass through the
PermissionPolicy, are audited, and support timeout/cancellation. Read-only
operations (status, diff, log, branch, show) require ``GIT_READ``; mutations
(checkout, commit) require ``GIT_WRITE`` (off by default).

Rules:
  * never push to remotes
  * never modify Git config
  * never auto-commit (only when the agent explicitly calls the commit tool)
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from errors import GitError, PermissionDeniedError
from events import EventBus, EventType
from security import AuditLog, PermissionPolicy
from security.policy import GIT_READ, GIT_WRITE, ToolPermission
from tools.base import ToolDefinition, tool_result
from tools.terminal import CancellationToken, run_command


class GitTools:
    """Workspace-scoped Git operations."""

    def __init__(
        self,
        *,
        workspace: Path,
        policy: PermissionPolicy,
        audit: AuditLog,
        events: EventBus | None = None,
        session_id: str = "git",
        default_timeout: float = 60.0,
        max_timeout: float = 600.0,
    ) -> None:
        self._workspace = workspace.resolve()
        self._policy = policy
        self._audit = audit
        self._events = events
        self._session_id = session_id
        self._default_timeout = default_timeout
        self._max_timeout = max_timeout
        self._cancel = CancellationToken()

    # --- shared runner ------------------------------------------------------

    async def _run(
        self,
        operation: str,
        args: list[str],
        permission: ToolPermission,
        *,
        timeout: float | None = None,
    ) -> str:
        # Inject a local fallback identity for commits so they don't fail when no
        # global git identity is configured. This uses -c flags (per-invocation)
        # and never modifies global/repo config.
        git_args = args
        if operation in {"git_commit"}:
            git_args = ["-c", "user.name=KINETIC Agent", "-c", "user.email=agent@kinetic.local", *args]
        command = "git " + " ".join(shlex.quote(a) for a in git_args)
        # Permission gate before execution.
        try:
            self._policy.require(operation, permission, {"command": command, "args": args})
        except PermissionDeniedError as exc:
            self._audit.record(
                session_id=self._session_id,
                action="git",
                tool=operation,
                allowed=False,
                reason=exc.reason,
            )
            raise

        self._emit(
            EventType.GIT_COMMAND_STARTED,
            operation=operation,
            command=command,
        )
        self._audit.record(
            session_id=self._session_id,
            action="git",
            tool=operation,
            allowed=True,
            detail={"command": command},
        )
        result = await run_command(
            command,
            cwd=str(self._workspace),
            timeout=timeout or self._default_timeout,
            cancellation=self._cancel,
        )
        self._emit(
            EventType.GIT_COMMAND_FINISHED,
            operation=operation,
            exit_code=result.exit_code,
            timed_out=result.timed_out,
        )
        if result.exit_code != 0:
            raise GitError(
                operation,
                f"git failed: {result.stderr.strip() or result.stdout.strip() or 'unknown error'}",
                exit_code=result.exit_code,
            )
        return result.stdout

    def _emit(self, event_type: EventType, **data: object) -> None:
        if self._events is not None:
            self._events.emit(event_type, self._session_id, **data)

    # --- read-only operations ----------------------------------------------

    async def status(self, args: dict[str, Any]) -> dict[str, Any]:
        out = await self._run("git_status", ["status", "--porcelain=v1", "-b"], GIT_READ)
        return tool_result(out)

    async def diff(self, args: dict[str, Any]) -> dict[str, Any]:
        target = args.get("ref") or args.get("path")
        cmd = ["diff"]
        staged = bool(args.get("staged", False))
        if staged:
            cmd.append("--staged")
        if target:
            cmd.append(str(target))
        out = await self._run("git_diff", cmd, GIT_READ)
        return tool_result(out or "(no changes)")

    async def log(self, args: dict[str, Any]) -> dict[str, Any]:
        limit = int(args.get("limit", 20))
        limit = max(1, min(limit, 200))
        out = await self._run("git_log", ["log", f"-{limit}", "--oneline", "--decorate"], GIT_READ)
        return tool_result(out or "(no commits)")

    async def branch(self, args: dict[str, Any]) -> dict[str, Any]:
        out = await self._run("git_branch", ["branch", "--list", "-vv"], GIT_READ)
        return tool_result(out or "(no branches)")

    async def show(self, args: dict[str, Any]) -> dict[str, Any]:
        ref = args.get("ref")
        if not isinstance(ref, str) or not ref.strip():
            raise GitError("git_show", "requires 'ref' argument")
        out = await self._run("git_show", ["show", "--stat", ref], GIT_READ)
        return tool_result(out)

    # --- mutating operations (require GIT_WRITE, off by default) -----------

    async def checkout(self, args: dict[str, Any]) -> dict[str, Any]:
        ref = args.get("ref")
        if not isinstance(ref, str) or not ref.strip():
            raise GitError("git_checkout", "requires 'ref' argument")
        out = await self._run("git_checkout", ["checkout", ref], GIT_WRITE)
        return tool_result(out or f"switched to {ref}")

    async def commit(self, args: dict[str, Any]) -> dict[str, Any]:
        message = args.get("message")
        if not isinstance(message, str) or not message.strip():
            raise GitError("git_commit", "requires non-empty 'message'")
        add_all = bool(args.get("add_all", True))
        cmd_prefix = ["add", "-A"] if add_all else []
        if cmd_prefix:
            await self._run("git_add", cmd_prefix, GIT_WRITE)
        out = await self._run("git_commit", ["commit", "-m", message], GIT_WRITE)
        return tool_result(out)

    # --- cancellation -------------------------------------------------------

    def cancel(self) -> None:
        self._cancel.cancel()


# --- schemas + registry -------------------------------------------------------

_STATUS_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}

_DIFF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ref": {"type": "string", "description": "A ref or path to diff."},
        "staged": {"type": "boolean", "default": False},
    },
}

_LOG_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"limit": {"type": "integer", "default": 20}},
}

_BRANCH_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}

_SHOW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"ref": {"type": "string"}},
    "required": ["ref"],
}

_CHECKOUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"ref": {"type": "string"}},
    "required": ["ref"],
}

_COMMIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "message": {"type": "string"},
        "add_all": {"type": "boolean", "default": True},
    },
    "required": ["message"],
}


def git_tools(
    *,
    workspace: Path,
    policy: PermissionPolicy,
    audit: AuditLog,
    events: EventBus | None = None,
    session_id: str = "git",
    default_timeout: float = 60.0,
    max_timeout: float = 600.0,
) -> list[ToolDefinition]:
    """Build the Git tool set scoped to ``workspace``."""
    g = GitTools(
        workspace=workspace,
        policy=policy,
        audit=audit,
        events=events,
        session_id=session_id,
        default_timeout=default_timeout,
        max_timeout=max_timeout,
    )
    return [
        ToolDefinition("git_status", "Show working-tree status (porcelain).", _STATUS_SCHEMA, GIT_READ, g.status),
        ToolDefinition("git_diff", "Show changes (working tree or staged).", _DIFF_SCHEMA, GIT_READ, g.diff),
        ToolDefinition("git_log", "Show commit log (oneline).", _LOG_SCHEMA, GIT_READ, g.log),
        ToolDefinition("git_branch", "List local branches.", _BRANCH_SCHEMA, GIT_READ, g.branch),
        ToolDefinition("git_show", "Show a commit/object.", _SHOW_SCHEMA, GIT_READ, g.show),
        ToolDefinition("git_checkout", "Checkout a branch/ref (requires git_write).", _CHECKOUT_SCHEMA, GIT_WRITE, g.checkout),
        ToolDefinition("git_commit", "Stage and commit changes (requires git_write).", _COMMIT_SCHEMA, GIT_WRITE, g.commit),
    ]
