"""Environment / workspace subsystem.

Phase 2 provides the ``Workspace`` abstraction. Phase 3 will add a full
sandbox/container ``Environment`` behind the same boundary — without changing
the agent or tools layer, since they only depend on ``Workspace``.
"""

from kinetic.environment.metadata import WorkspaceMeta
from kinetic.environment.status import WorkspaceStatus
from kinetic.environment.workspace import Workspace

__all__ = ["Workspace", "WorkspaceMeta", "WorkspaceStatus"]
