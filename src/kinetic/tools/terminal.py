"""Terminal tool: execute shell commands with lifecycle control.

Runs commands as subprocesses, captures stdout/stderr, enforces a timeout,
supports cancellation, and returns the exit code. Designed for the controlled
environment; the permission policy decides whether execution is allowed at all.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from kinetic.errors import ToolError
from kinetic.tools.base import ToolDefinition, tool_result


@dataclass
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False


async def run_command(
    command: str,
    *,
    cwd: str | None = None,
    timeout: float = 120.0,
    env: dict[str, str] | None = None,
    cancellation: CancellationToken | None = None,
) -> CommandResult:
    """Execute a command with timeout + cancellation support."""
    import time

    start = time.monotonic()
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
    )

    cancelled = False
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        stdout_b, stderr_b = await proc.communicate()
        cancelled = True
    finally:
        if cancellation is not None and cancellation.cancelled:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            cancelled = True

    duration_ms = int((time.monotonic() - start) * 1000)
    exit_code = proc.returncode if proc.returncode is not None else -1
    if cancelled and exit_code == 0:
        exit_code = -1

    return CommandResult(
        exit_code=exit_code if not cancelled else -1,
        stdout=stdout_b.decode("utf-8", errors="replace"),
        stderr=stderr_b.decode("utf-8", errors="replace"),
        duration_ms=duration_ms,
        timed_out=cancelled,
    )


class CancellationToken:
    """Minimal cooperative cancellation flag for long-running commands."""

    def __init__(self) -> None:
        self._cancelled = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        self._cancelled = True


class TerminalTool:
    """A configurable terminal tool bound to a working directory + settings."""

    def __init__(
        self,
        *,
        cwd: str,
        default_timeout: float = 120.0,
        max_timeout: float = 1800.0,
        cancellation: CancellationToken | None = None,
    ) -> None:
        self._cwd = cwd
        self._default_timeout = default_timeout
        self._max_timeout = max_timeout
        self._cancellation = cancellation

    async def run(self, args: dict[str, Any]) -> dict[str, Any]:
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ToolError("terminal", "missing or empty 'command' argument")
        timeout = float(args.get("timeout", self._default_timeout))
        timeout = min(timeout, self._max_timeout)

        result = await run_command(
            command,
            cwd=self._cwd,
            timeout=timeout,
            cancellation=self._cancellation,
        )
        body = (
            f"$ {command}\n"
            f"[exit {result.exit_code}] ({result.duration_ms}ms)"
            + (" [TIMED OUT]" if result.timed_out else "")
            + "\n--- stdout ---\n"
            f"{result.stdout}"
            + ("\n--- stderr ---\n" + result.stderr if result.stderr else "")
        )
        return tool_result(body, is_error=(result.exit_code != 0 or result.timed_out))


TERMINAL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "description": "The shell command to execute."},
        "timeout": {
            "type": "number",
            "description": "Timeout in seconds (default 120).",
            "default": 120,
        },
    },
    "required": ["command"],
}


def terminal_tool(
    *, cwd: str, default_timeout: float, max_timeout: float
) -> ToolDefinition:
    from kinetic.security.policy import EXECUTE

    instance = TerminalTool(cwd=cwd, default_timeout=default_timeout, max_timeout=max_timeout)
    return ToolDefinition(
        name="run_command",
        description="Execute a shell command in the project workspace and return stdout/stderr and exit code.",
        input_schema=TERMINAL_INPUT_SCHEMA,
        permission=EXECUTE,
        func=instance.run,
    )
