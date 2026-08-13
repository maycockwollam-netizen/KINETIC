"""The Environment abstraction: a controlled execution context.

An :class:`Environment` owns a :class:`~environment.workspace.Workspace`,
a :class:`~environment.runtime.EnvironmentRuntime`, and an
:class:`~environment.config.EnvironmentConfig`. It drives the runtime
through a strict state machine, emits events, writes audit records, and
exposes a single :meth:`exec` entry point for all process execution.

The agent layer never talks to a runtime directly — it goes through the
Environment. This keeps the security boundary (state checks + permission gate +
audit + events) in one place.

Lifecycle::

    env = Environment.create(workspace, config, ...)
    await env.start()                 # CREATING -> READY -> RUNNING
    result = await env.exec(spec)    # runs inside the runtime
    await env.stop()                  # -> STOPPING -> STOPPED
    await env.destroy()               # -> DESTROYED (releases runtime resources)
"""

from __future__ import annotations

from asyncio import CancelledError
from pathlib import Path
from typing import TYPE_CHECKING, Any

from environment.config import RUNTIME_DOCKER, RUNTIME_LOCAL, EnvironmentConfig
from environment.process import ProcessResult, ProcessSpec, ProcessState
from environment.runtime import EnvironmentRuntime, RuntimeStatus
from environment.states import EnvironmentState, require_transition, transition_allowed
from errors import EnvironmentStateError, SandboxError
from events import EventBus, EventType
from security import AuditLog, PermissionPolicy
from tools.terminal import CancellationToken

if TYPE_CHECKING:
    from observability import MetricsCollector


def _select_runtime(config: EnvironmentConfig) -> type[EnvironmentRuntime]:
    """Pick the runtime class for a config type. Imports are lazy to avoid
    importing Docker unless actually needed (keeps the local path dependency-free)."""
    if config.runtime_type == RUNTIME_LOCAL:
        from environment.local import LocalRuntime

        return LocalRuntime
    if config.runtime_type == RUNTIME_DOCKER:
        from environment.docker import DockerRuntime

        return DockerRuntime
    raise SandboxError(f"unknown runtime type: {config.runtime_type!r}")


class Environment:
    """A controlled execution environment wrapping a workspace + runtime."""

    def __init__(
        self,
        workspace: Path,
        config: EnvironmentConfig,
        *,
        runtime: EnvironmentRuntime | None = None,
        policy: PermissionPolicy | None = None,
        audit: AuditLog | None = None,
        events: EventBus | None = None,
        session_id: str = "environment",
        metrics: MetricsCollector | None = None,
    ) -> None:
        self._workspace = workspace.resolve()
        self._config = config
        self._policy = policy
        self._audit = audit
        self._events = events
        self._session_id = session_id
        self._metrics = metrics
        self._state = EnvironmentState.CREATING
        self._runtime = runtime if runtime is not None else _select_runtime(config)(self._workspace, config)
        self._runtime.session_id = session_id  # tag runtime for label correlation
        self._emit(EventType.ENVIRONMENT_CREATED, workspace=str(self._workspace),
                   runtime=config.runtime_type, network=config.network.value)
        if self._metrics is not None:
            from observability.metrics import METRIC_ENV_CREATED

            self._metrics.inc(METRIC_ENV_CREATED)

    # --- construction ------------------------------------------------------

    @classmethod
    def create(
        cls,
        workspace: Path,
        config: EnvironmentConfig,
        *,
        policy: PermissionPolicy | None = None,
        audit: AuditLog | None = None,
        events: EventBus | None = None,
        session_id: str = "environment",
        metrics: MetricsCollector | None = None,
    ) -> Environment:
        """Construct an Environment in the CREATING state and provision it.

        Provisioning (runtime.create) is synchronous-ish here; if it fails the
        environment transitions to FAILED and the error propagates.
        """
        env = cls(workspace, config, policy=policy, audit=audit, events=events,
                  session_id=session_id, metrics=metrics)
        return env

    async def provision(self) -> None:
        """Run the runtime's create + start. Moves CREATING -> READY -> RUNNING."""
        await self._transition_to(EnvironmentState.READY, op=self._runtime.create,
                                   started_event=EventType.ENVIRONMENT_STARTED)
        await self.start()

    # --- public API --------------------------------------------------------

    @property
    def workspace(self) -> Path:
        return self._workspace

    @property
    def config(self) -> EnvironmentConfig:
        return self._config

    @property
    def state(self) -> EnvironmentState:
        return self._state

    @property
    def runtime(self) -> EnvironmentRuntime:
        return self._runtime

    def is_running(self) -> bool:
        return self._state is EnvironmentState.RUNNING

    async def start(self) -> None:
        """READY -> RUNNING."""
        if self._state is EnvironmentState.READY:
            await self._do_transition(EnvironmentState.RUNNING, self._runtime.start)
            self._emit(EventType.ENVIRONMENT_STARTED, runtime=self._config.runtime_type)
            return
        # Allow re-start from STOPPED.
        if self._state is EnvironmentState.STOPPED:
            await self._do_transition(EnvironmentState.READY, self._runtime.create)
            await self._do_transition(EnvironmentState.RUNNING, self._runtime.start)
            self._emit(EventType.ENVIRONMENT_STARTED, runtime=self._config.runtime_type)
            return
        raise EnvironmentStateError(
            f"cannot start environment in state {self._state.value}"
        )

    async def exec(self, spec: ProcessSpec, *, cancellation: CancellationToken | None = None) -> ProcessResult:
        """Execute a process spec inside the environment.

        Requires RUNNING state. The permission policy (if configured) is
        consulted for the ENVIRONMENT_EXEC capability *here* — not only at the
        tool gate — so direct callers cannot bypass the security boundary.
        All outcomes are audited and emitted as events, including denials,
        timeouts and cancellation.
        """
        if not self.is_running():
            raise EnvironmentStateError(
                f"cannot exec in environment state {self._state.value}"
            )
        self._enforce_exec_permission(spec)
        self._audit_exec(spec)
        self._emit(EventType.PROCESS_STARTED, command=spec.command, cwd=spec.cwd)
        try:
            result = await self._runtime.exec(spec, cancellation=cancellation)
        except CancelledError:
            self._emit(EventType.PROCESS_CANCELLED, command=spec.command)
            raise
        if result.state is ProcessState.TIMED_OUT:
            self._emit(EventType.PROCESS_TIMEOUT, command=spec.command,
                       exit_code=result.exit_code, duration_ms=result.duration_ms)
        else:
            self._emit(EventType.PROCESS_FINISHED, command=spec.command,
                       exit_code=result.exit_code, duration_ms=result.duration_ms,
                       state=result.state.value)
        return result

    def _enforce_exec_permission(self, spec: ProcessSpec) -> None:
        """Enforce the ENVIRONMENT_EXEC capability at the environment boundary.

        This is the defense-in-depth gate that closes the direct-call bypass:
        even if a caller invokes ``Environment.exec`` without going through the
        adapter's ``can_use_tool`` hook, the policy is still consulted. A denial
        is audited and emitted as a PERMISSION_DENIED event before raising.
        """
        if self._policy is None:
            return
        from security.policy import ENVIRONMENT_EXEC

        decision = self._policy.evaluate("run_command", ENVIRONMENT_EXEC,
                                         {"command": spec.command, "cwd": spec.cwd})
        if not decision.allowed:
            self._emit(EventType.PERMISSION_DENIED, tool="run_command",
                       reason=decision.reason, command=spec.command)
            if self._audit is not None:
                self._audit.record(
                    session_id=self._session_id, action="permission",
                    tool="run_command", allowed=False, reason=decision.reason,
                    detail={"command": spec.command, "cwd": spec.cwd},
                )
            if self._metrics is not None:
                from observability.metrics import METRIC_PERMISSION_DENIALS

                self._metrics.inc(METRIC_PERMISSION_DENIALS)
            from errors import PermissionDeniedError

            raise PermissionDeniedError("run_command", decision.reason)

    async def stop(self) -> None:
        """RUNNING -> STOPPING -> STOPPED."""
        if self._state is not EnvironmentState.RUNNING:
            return
        await self._do_transition(EnvironmentState.STOPPING, self._runtime.stop)
        await self._do_transition(EnvironmentState.STOPPED, lambda: _noop())
        self._emit(EventType.ENVIRONMENT_STOPPED, runtime=self._config.runtime_type)

    async def destroy(self) -> None:
        """Release all runtime resources. Terminal state: DESTROYED."""
        if self._state is EnvironmentState.DESTROYED:
            return
        # Allow destroy from any non-destroyed state for cleanup safety.
        try:
            if self._state in (EnvironmentState.RUNNING, EnvironmentState.STOPPING):
                await self._runtime.stop()
            await self._runtime.destroy()
        except Exception as exc:  # noqa: BLE001 - record then continue to mark destroyed
            self._fail(str(exc))
        self._state = EnvironmentState.DESTROYED
        self._emit(EventType.ENVIRONMENT_DESTROYED, runtime=self._config.runtime_type)
        if self._metrics is not None:
            from observability.metrics import METRIC_ENV_DESTROYED

            self._metrics.inc(METRIC_ENV_DESTROYED)

    async def inspect(self) -> RuntimeStatus:
        return await self._runtime.inspect()

    # --- internals ---------------------------------------------------------

    async def _transition_to(
        self,
        target: EnvironmentState,
        *,
        op: Any,
        started_event: EventType | None = None,
    ) -> None:
        require_transition(self._state, target)
        try:
            await op()
        except Exception as exc:  # noqa: BLE001
            self._fail(str(exc))
            raise
        self._state = target

    async def _do_transition(self, target: EnvironmentState, op: Any) -> None:
        require_transition(self._state, target)
        try:
            result = op()
            if hasattr(result, "__await__"):
                await result
        except Exception as exc:  # noqa: BLE001
            self._fail(str(exc))
            raise
        self._state = target

    def _fail(self, reason: str) -> None:
        if transition_allowed(self._state, EnvironmentState.FAILED):
            self._state = EnvironmentState.FAILED
        self._emit(EventType.ENVIRONMENT_FAILED, reason=reason, runtime=self._config.runtime_type)
        if self._audit is not None:
            self._audit.record(
                session_id=self._session_id, action="environment_failed",
                allowed=False, reason=reason,
                detail={"runtime": self._config.runtime_type},
            )

    def _audit_exec(self, spec: ProcessSpec) -> None:
        if self._audit is None:
            return
        self._audit.record(
            session_id=self._session_id, action="process_exec",
            allowed=True, detail={"command": spec.command, "cwd": spec.cwd},
        )

    def _emit(self, event_type: EventType, **data: object) -> None:
        if self._events is not None:
            self._events.emit(event_type, self._session_id, **data)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"Environment(state={self._state.value}, runtime={self._config.runtime_type})"


async def _noop() -> None:
    return None
