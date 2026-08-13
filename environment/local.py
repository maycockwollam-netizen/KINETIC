"""Local (subprocess) runtime.

A development runtime that runs processes as host subprocesses bound to the
workspace directory. It enforces:

  * workspace-bound working directory (processes run inside the workspace root)
  * environment-variable filtering (host env is never forwarded wholesale)
  * timeout + cancellation
  * exit-code capture

It does **not** provide filesystem or network isolation — a subprocess can
still touch host paths via absolute paths and reach the network. Therefore
``LocalRuntime`` is honest about what it cannot do:

  * ``network == DENY`` or ``RESTRICTED`` is NOT enforceable locally and raises
    :class:`~errors.SandboxError` at create time (fail closed) unless
    the config explicitly opts into a non-isolating dev mode
    (``sandbox_mode=False``), in which case network policy is treated as ALLOW
    with a recorded warning — never silently as if isolated.
  * hard resource limits (CPU/memory/process count/disk) are NOT enforceable
    locally; requesting them with ``sandbox_mode=True`` raises.

This runtime is the bridge that keeps the existing host-based ``run_command``
behavior working while routing it through the environment abstraction.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from environment.config import EnvironmentConfig
from environment.network import NetworkPolicy
from environment.process import ProcessResult, ProcessSpec, ProcessState
from environment.runtime import EnvironmentRuntime, RuntimeStatus
from errors import SandboxError
from tools.terminal import CancellationToken, run_command


class LocalRuntime(EnvironmentRuntime):
    """Host-subprocess runtime; isolates what it can, fails closed on the rest."""

    runtime_type = "local"

    def __init__(self, workspace: Path, config: EnvironmentConfig) -> None:
        super().__init__(workspace, config)
        self._created = False
        self._started = False
        self._stopped = False
        self._network_warning: str | None = None

    async def create(self) -> None:
        self._check_network_policy()
        self._check_resource_limits()
        self._workspace.mkdir(parents=True, exist_ok=True)
        self._created = True

    async def start(self) -> None:
        self._require_created()
        self._started = True

    async def exec(self, spec: ProcessSpec, *, cancellation: CancellationToken | None = None) -> ProcessResult:
        self._require_started()
        cwd = self._resolve_cwd(spec.cwd)
        # Build the process environment from the filtered host env, then layer
        # the spec's explicit env on top. We NEVER pass env=None, which would
        # cause the subprocess to inherit the full host environment wholesale.
        env = self._build_process_env(spec.env)
        result = await run_command(
            spec.command,
            cwd=str(cwd),
            timeout=spec.timeout,
            env=env,
            cancellation=cancellation,
        )
        state = ProcessState.COMPLETED
        if result.timed_out:
            state = ProcessState.TIMED_OUT
        return ProcessResult(
            spec=spec,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_ms=result.duration_ms,
            state=state,
        )

    async def stop(self) -> None:
        self._stopped = True

    async def destroy(self) -> None:
        self._created = False
        self._started = False

    async def inspect(self) -> RuntimeStatus:
        return RuntimeStatus(
            runtime_type=self.runtime_type,
            ready=self._started and not self._stopped,
            detail={
                "workspace": str(self._workspace),
                "network": self._config.network.value,
                "sandbox_mode": self._config.sandbox_mode,
                "network_warning": self._network_warning,
            },
        )

    @staticmethod
    async def probe() -> bool:
        # The local runtime is always available — it only needs a filesystem.
        return True

    # --- enforcement -------------------------------------------------------

    def _check_network_policy(self) -> None:
        """Fail closed if true network isolation is requested but impossible."""
        net = self._config.network
        if net is NetworkPolicy.ALLOW:
            return
        # DENY / RESTRICTED cannot be enforced by a plain subprocess.
        if self._config.sandbox_mode:
            raise SandboxError(
                f"local runtime cannot enforce network policy {net.value!r}; "
                "use the docker runtime for network isolation"
            )
        # Dev mode: record that we are NOT isolating and proceed honestly.
        self._network_warning = (
            f"network policy {net.value!r} requested but local runtime does not "
            "isolate network; running without network isolation"
        )

    def _check_resource_limits(self) -> None:
        """Hard limits cannot be enforced locally; fail closed if requested."""
        if not self._config.sandbox_mode:
            return
        unsupported = [
            f for f in ("cpu", "memory_bytes", "process_count", "disk_bytes")
            if getattr(self._config.resources, f)
        ]
        if unsupported:
            raise SandboxError(
                f"local runtime cannot enforce resource limits {unsupported}; "
                "use the docker runtime"
            )

    # --- helpers -----------------------------------------------------------

    def _build_process_env(self, spec_env: dict[str, str] | None) -> dict[str, str]:
        """Construct the subprocess environment from policy + spec overrides.

        The host environment is filtered through the configured
        :class:`EnvironmentVariablePolicy` (which drops denied/secret-named vars
        and redacts secret-shaped values). The spec's explicit ``env`` is then
        layered on top so callers can override individual variables. The result
        is always a concrete dict — never ``None`` — so the subprocess never
        silently inherits the full host environment.
        """
        import os

        base = self._config.env_vars.filter(dict(os.environ))
        if spec_env:
            base.update(spec_env)
        return base

    def _resolve_cwd(self, cwd: str) -> Path:
        """Resolve a spec cwd against the workspace, rejecting escapes."""
        from paths import safe_resolve

        if not cwd or cwd == ".":
            return self._workspace
        return safe_resolve(self._workspace, cwd)

    def _require_created(self) -> None:
        if not self._created:
            raise SandboxError("local runtime not created")

    def _require_started(self) -> None:
        if not self._started:
            raise SandboxError("local runtime not started")

    def detail(self) -> dict[str, Any]:
        return {
            "network_warning": self._network_warning,
            "sandbox_mode": self._config.sandbox_mode,
        }
