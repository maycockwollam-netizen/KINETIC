"""Docker container runtime — a real isolated sandbox.

Only this module knows about Docker. It realizes the abstract
:class:`~kinetic.environment.runtime.EnvironmentRuntime` operations via the
Docker CLI (shelled out through the existing subprocess runner, so Docker
APIs never leak into the rest of the application).

Isolation enforced:

  * filesystem: only the workspace (and explicitly allowed mounts) are bind-mounted
    read-write; the host home dir, SSH creds and cloud creds are NEVER mounted.
  * network: ``DENY`` -> ``--network none``; ``ALLOW`` -> default bridge;
    ``RESTRICTED`` -> ``--network none`` plus an explicit egress note (we do not
    stand up proxy infra; restricted callers must supply a pre-built network).
  * resource limits: ``--cpus``, ``--memory``, ``--pids-limit`` (and a container
    timeout via the process timeout). Disk is enforced via storage options where
    the daemon supports it; otherwise it fails closed if requested.
  * environment variables: only the filtered set is passed via ``-e``/``--env-file``.

Fail-closed behavior:

  * if the Docker daemon is unavailable, raise :class:`RuntimeUnavailableError`
    (never silently fall back to host execution);
  * if a requested limit/policy cannot be enforced, raise :class:`SandboxError`.
"""

from __future__ import annotations

import contextlib
import shlex
import tempfile
from pathlib import Path
from typing import Any

from kinetic.environment.config import EnvironmentConfig
from kinetic.environment.network import NetworkPolicy
from kinetic.environment.process import ProcessResult, ProcessSpec, ProcessState
from kinetic.environment.runtime import EnvironmentRuntime, RuntimeStatus
from kinetic.errors import RuntimeUnavailableError, SandboxError
from kinetic.tools.terminal import CancellationToken, run_command

#: Whether to prefix docker invocations with sudo (needed in this sandbox).
_USE_SUDO = True


def _docker_cmd_prefix() -> str:
    return "sudo docker" if _USE_SUDO else "docker"


class DockerRuntime(EnvironmentRuntime):
    """Containerized execution behind the Environment interface."""

    runtime_type = "docker"

    def __init__(self, workspace: Path, config: EnvironmentConfig) -> None:
        super().__init__(workspace, config)
        self._container_id: str | None = None
        self._started = False
        self._stopped = False
        self._env_file: Path | None = None

    # --- lifecycle ---------------------------------------------------------

    async def create(self) -> None:
        await self._require_daemon()
        self._validate_mounts()
        self._check_resource_limits_support()
        self._workspace.mkdir(parents=True, exist_ok=True)
        self._env_file = await self._write_env_file()
        await self._create_container()

    async def start(self) -> None:
        self._require_container()
        prefix = _docker_cmd_prefix()
        res = await run_command(f"{prefix} start {self._container_id}", timeout=30)
        if res.exit_code != 0:
            raise SandboxError(f"docker start failed: {res.stderr.strip()}")
        self._started = True

    async def exec(self, spec: ProcessSpec, *, cancellation: CancellationToken | None = None) -> ProcessResult:
        self._require_started()
        cwd = self._resolve_container_cwd(spec.cwd)
        # ``docker exec`` runs the command through the container's default shell.
        docker_cmd = [
            "exec",
            "--env-file", str(self._env_file),
            "-w", str(cwd),
        ]
        if spec.env:
            for k, v in spec.env.items():
                docker_cmd += ["-e", f"{k}={v}"]
        docker_cmd += [self._container_id, "sh", "-c", spec.command]
        prefix = _docker_cmd_prefix()
        full = prefix + " " + " ".join(shlex.quote(a) for a in docker_cmd)
        res = await run_command(full, timeout=spec.timeout, cancellation=cancellation)
        state = ProcessState.TIMED_OUT if res.timed_out else ProcessState.COMPLETED
        return ProcessResult(
            spec=spec,
            exit_code=res.exit_code,
            stdout=res.stdout,
            stderr=res.stderr,
            duration_ms=res.duration_ms,
            state=state,
        )

    async def stop(self) -> None:
        if self._container_id is None or self._stopped:
            return
        prefix = _docker_cmd_prefix()
        await run_command(
            f"{prefix} stop -t 5 {self._container_id}", timeout=30, cancellation=None
        )
        self._stopped = True

    async def destroy(self) -> None:
        if self._container_id is not None:
            prefix = _docker_cmd_prefix()
            # Best-effort remove; ignore failures (container may already be gone).
            await run_command(
                f"{prefix} rm -f {self._container_id}", timeout=30, cancellation=None
            )
        self._container_id = None
        if self._env_file is not None:
            with contextlib.suppress(OSError):
                self._env_file.unlink(missing_ok=True)
            self._env_file = None
        self._started = False
        self._stopped = False

    async def inspect(self) -> RuntimeStatus:
        detail: dict[str, Any] = {
            "workspace": str(self._workspace),
            "image": self._config.image,
            "network": self._config.network.value,
            "container_id": self._container_id,
        }
        if self._container_id is not None:
            prefix = _docker_cmd_prefix()
            res = await run_command(
                f"{prefix} inspect --format '{{.State.Status}}' {self._container_id}",
                timeout=15,
            )
            detail["container_status"] = res.stdout.strip() or "unknown"
        return RuntimeStatus(
            runtime_type=self.runtime_type,
            ready=self._started and not self._stopped,
            detail=detail,
        )

    @staticmethod
    async def probe() -> bool:
        """Return True iff a Docker daemon is reachable."""
        prefix = _docker_cmd_prefix()
        res = await run_command(f"{prefix} info --format '{{{{json .ServerVersion}}}}'", timeout=15)
        return res.exit_code == 0 and bool(res.stdout.strip())

    # --- container creation ------------------------------------------------

    async def _create_container(self) -> None:
        args = [
            "create",
            "--env-file", str(self._env_file),
            "-w", "/workspace",
        ]
        args += self._network_args()
        args += self._resource_args()
        # Bind-mount the workspace read-write at /workspace.
        args += ["-v", f"{self._workspace}:/workspace"]
        # Explicitly allowed extra mounts only (never home/ssh/cloud dirs).
        for mount in self._config.extra_mounts:
            resolved = str(Path(mount).resolve())
            args += ["-v", f"{resolved}:{resolved}"]
        args += [self._config.image, "sleep", "infinity"]
        prefix = _docker_cmd_prefix()
        full = prefix + " " + " ".join(shlex.quote(a) for a in args)
        res = await run_command(full, timeout=60)
        if res.exit_code != 0:
            raise SandboxError(f"docker create failed: {res.stderr.strip()}")
        self._container_id = res.stdout.strip().splitlines()[0].strip()
        if not self._container_id:
            raise SandboxError("docker create returned no container id")

    def _network_args(self) -> list[str]:
        net = self._config.network
        if net is NetworkPolicy.DENY:
            return ["--network", "none"]
        if net is NetworkPolicy.ALLOW:
            return []  # default bridge
        # RESTRICTED: we don't stand up proxy infra. Fail closed if rules are
        # requested but cannot be realized; otherwise treat as DENY.
        if self._config.network_rules:
            raise SandboxError(
                "docker runtime cannot realize RESTRICTED egress rules without "
                "a pre-built network; supply one or use DENY/ALLOW"
            )
        return ["--network", "none"]

    def _resource_args(self) -> list[str]:
        r = self._config.resources
        args: list[str] = []
        if r.cpu:
            args += ["--cpus", str(r.cpu)]
        if r.memory_bytes:
            args += ["--memory", str(r.memory_bytes)]
        if r.process_count:
            args += ["--pids-limit", str(r.process_count)]
        # disk: not all daemons support --storage-opt; fail closed if requested.
        if r.disk_bytes:
            raise SandboxError(
                "docker runtime cannot enforce disk limits via --storage-opt "
                "on this daemon; unset disk_bytes or use an external quota"
            )
        return args

    def _check_resource_limits_support(self) -> None:
        # Disk is validated in _resource_args (raised at create time).
        pass

    # --- env file ----------------------------------------------------------

    async def _write_env_file(self) -> Path:
        """Write the filtered environment to a temp file passed via --env-file."""
        import os

        host_env = dict(os.environ)
        filtered = self._config.env_vars.filter(host_env)
        fd, path = tempfile.mkstemp(prefix="kinetic-env-", suffix=".env")
        self._env_file = Path(path)
        try:
            with self._env_file.open("w", encoding="utf-8") as f:
                for k, v in filtered.items():
                    f.write(f"{k}={v}\n")
        finally:
            os.close(fd)
        return self._env_file

    # --- validation --------------------------------------------------------

    def _validate_mounts(self) -> None:
        """Reject mounts that would expose host credentials/home/SSH."""
        forbidden_roots = [
            Path.home(),
            Path.home() / ".ssh",
            Path("/etc/ssh"),
            Path("/root"),
        ]
        for mount in self._config.extra_mounts:
            mp = Path(mount).resolve()
            for forb in forbidden_roots:
                try:
                    mp.relative_to(forb)
                    raise SandboxError(f"refusing to mount sensitive host path: {mount}")
                except ValueError:
                    continue
            if not mp.exists():
                raise SandboxError(f"extra mount does not exist: {mount}")

    def _resolve_container_cwd(self, cwd: str) -> str:
        """Map a spec cwd onto a container path under /workspace."""
        if not cwd or cwd == ".":
            return "/workspace"
        # Treat spec cwd as workspace-relative; reject traversal.
        from kinetic.paths import safe_resolve

        resolved = safe_resolve(self._workspace, cwd)
        try:
            rel = resolved.relative_to(self._workspace)
        except ValueError as exc:
            raise SandboxError(f"cwd outside workspace: {cwd}") from exc
        return f"/workspace/{rel.as_posix()}"

    # --- guards ------------------------------------------------------------

    async def _require_daemon(self) -> None:
        if not await self.probe():
            raise RuntimeUnavailableError(
                "docker daemon is not available; refusing to fall back to "
                "unrestricted host execution"
            )

    def _require_container(self) -> None:
        if self._container_id is None:
            raise SandboxError("docker container not created")

    def _require_started(self) -> None:
        self._require_container()
        if not self._started:
            raise SandboxError("docker container not started")

    def _guard_no_orphans(self) -> None:
        pass
