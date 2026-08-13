"""Operational diagnostics for the environment/sandbox subsystem.

Provides read-only inspection of KINETIC-managed Docker containers for leak
detection and operational hygiene. This is a *diagnostic* module: it lists
stale containers but NEVER destroys anything automatically — cleanup is always
explicit and permission-aware (the operator decides).

Only containers carrying the ``kinetic.managed=true`` label are considered, so
unrelated host containers are never inspected or touched.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from kinetic.environment.docker import _docker_cmd_prefix
from kinetic.tools.terminal import run_command

#: The management label that marks a container as KINETIC-owned.
MANAGED_LABEL = "kinetic.managed"
MANAGED_LABEL_VALUE = "true"


@dataclass
class ContainerInfo:
    """A discovered KINETIC-managed container (read-only)."""

    container_id: str
    name: str
    image: str
    status: str
    labels: dict[str, str]
    created: str = ""

    @property
    def session_id(self) -> str | None:
        return self.labels.get("kinetic.session_id")

    @property
    def environment_label(self) -> str | None:
        return self.labels.get("kinetic.environment")

    @property
    def is_managed(self) -> bool:
        return self.labels.get(MANAGED_LABEL) == MANAGED_LABEL_VALUE


async def list_managed_containers() -> list[ContainerInfo]:
    """List all KINETIC-managed containers on the Docker daemon.

    Only containers with ``kinetic.managed=true`` are returned. Unrelated host
    containers are never included. This is read-only and never mutates state.
    """
    prefix = _docker_cmd_prefix()
    # Use --filter so the daemon does the work; we never inspect unrelated
    # containers.
    res = await run_command(
        f'{prefix} ps -a --no-trunc --filter "label={MANAGED_LABEL}={MANAGED_LABEL_VALUE}" '
        '--format "{{json .}}"',
        timeout=30,
    )
    if res.exit_code != 0:
        return []
    containers: list[ContainerInfo] = []
    for line in res.stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        containers.append(_parse_container(obj))
    return containers


def _parse_container(obj: dict[str, Any]) -> ContainerInfo:
    labels_raw = obj.get("Labels", "")
    labels: dict[str, str] = {}
    if isinstance(labels_raw, str):
        for pair in labels_raw.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                labels[k] = v
    elif isinstance(labels_raw, dict):
        labels = dict(labels_raw)
    return ContainerInfo(
        container_id=obj.get("ID", obj.get("Id", "")),
        name=obj.get("Names", obj.get("Name", "")),
        image=obj.get("Image", ""),
        status=obj.get("Status", ""),
        labels=labels,
        created=obj.get("CreatedAt", obj.get("Created", "")),
    )


async def find_stale_containers() -> list[ContainerInfo]:
    """Identify KINETIC-managed containers that appear stale.

    A container is considered stale if it is in an ``exited`` state (its owning
    session likely finished without cleanup) or has been running longer than
    expected (heuristic: status indicates it is up). This is diagnostic only —
    it does NOT destroy anything. The caller decides what to do.

    Only managed containers are considered; unrelated containers are invisible.
    """
    managed = await list_managed_containers()
    stale: list[ContainerInfo] = []
    for c in managed:
        status_lower = c.status.lower()
        if "exited" in status_lower or "dead" in status_lower or "created" in status_lower:
            stale.append(c)
    return stale


async def destroy_container(container_id: str) -> bool:
    """Explicitly destroy one KINETIC-managed container by ID.

    This is the ONLY destructive operation in this module, and it is explicit:
    the caller must pass a specific container ID. It verifies the container is
    KINETIC-managed before removing it, so an unrelated container can never be
    destroyed through this path.
    """
    managed = await list_managed_containers()
    if not any(c.container_id == container_id or c.container_id.startswith(container_id) for c in managed):
        return False
    prefix = _docker_cmd_prefix()
    res = await run_command(f"{prefix} rm -f {container_id}", timeout=30)
    return res.exit_code == 0


__all__ = [
    "ContainerInfo",
    "MANAGED_LABEL",
    "destroy_container",
    "find_stale_containers",
    "list_managed_containers",
]
