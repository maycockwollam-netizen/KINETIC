"""Tool permission metadata.

Each tool declares what it can do (read filesystem, write filesystem, execute
commands, network, ...). The permission policy uses this metadata — *not* the
agent's prompt — to decide whether a call is allowed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Flag, auto


class Capability(Flag):
    """What a tool is allowed to touch. Composable via bitwise OR."""

    NONE = 0
    READ_FS = auto()
    WRITE_FS = auto()
    EXECUTE = auto()
    NETWORK = auto()
    GIT = auto()
    BROWSER = auto()
    # Phase 2 fine-grained capabilities
    GIT_READ = auto()
    GIT_WRITE = auto()
    DEPENDENCY_READ = auto()
    DEPENDENCY_INSTALL = auto()
    WORKSPACE_READ = auto()
    WORKSPACE_WRITE = auto()


@dataclass(frozen=True)
class ToolPermission:
    """Permission metadata for a single tool."""

    capabilities: Capability
    #: Human-readable description used in audit records.
    description: str = ""
    #: Whether this tool is considered destructive (e.g. overwrites files).
    destructive: bool = False
    #: Extra free-form constraints enforced by the tool itself (e.g. max timeout).
    constraints: dict[str, object] = field(default_factory=dict)


#: Read-only inspection: no side effects.
READ_ONLY = ToolPermission(
    capabilities=Capability.READ_FS,
    description="Read-only filesystem inspection.",
    destructive=False,
)

#: File writes within the workspace root.
FILE_WRITE = ToolPermission(
    capabilities=Capability.WRITE_FS | Capability.READ_FS,
    description="Read and write files within the workspace.",
    destructive=True,
)

#: Command execution (potentially arbitrary side effects).
EXECUTE = ToolPermission(
    capabilities=Capability.EXECUTE,
    description="Execute shell commands in the controlled environment.",
    destructive=True,
    constraints={"requires_timeout": True},
)

#: Network access (off by default).
NETWORK = ToolPermission(
    capabilities=Capability.NETWORK,
    description="Network access (off by default; must be explicitly enabled).",
    destructive=False,
)

#: Read-only Git inspection (status, diff, log, branch, show).
GIT_READ = ToolPermission(
    capabilities=Capability.GIT_READ | Capability.GIT | Capability.READ_FS,
    description="Read-only Git operations within the workspace.",
    destructive=False,
)

#: Git mutations (checkout, commit). Default off; enabled per-session.
GIT_WRITE = ToolPermission(
    capabilities=Capability.GIT_WRITE | Capability.GIT | Capability.WRITE_FS,
    description="Git write operations (checkout, commit) within the workspace.",
    destructive=True,
)

#: Dependency detection only (read manifests/lockfiles).
DEPENDENCY_READ = ToolPermission(
    capabilities=Capability.DEPENDENCY_READ | Capability.READ_FS,
    description="Read dependency manifests and lockfiles.",
    destructive=False,
)

#: Dependency installation (executes a package manager).
DEPENDENCY_INSTALL = ToolPermission(
    capabilities=Capability.DEPENDENCY_INSTALL | Capability.EXECUTE,
    description="Install dependencies using the detected package manager.",
    destructive=True,
    constraints={"requires_timeout": True, "workspace_only": True},
)

#: Workspace inspection.
WORKSPACE_READ = ToolPermission(
    capabilities=Capability.WORKSPACE_READ | Capability.READ_FS,
    description="Read workspace metadata and structure.",
    destructive=False,
)

#: Workspace creation/cleanup.
WORKSPACE_WRITE = ToolPermission(
    capabilities=Capability.WORKSPACE_WRITE | Capability.WRITE_FS,
    description="Create or remove workspace directories.",
    destructive=True,
)
