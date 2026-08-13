"""Environment / workspace subsystem.

Phase 2 provides the ``Workspace`` abstraction. Phase 3 adds a full
sandbox/container ``Environment`` behind the same boundary — without changing
the agent or tools layer, since they only depend on ``Workspace`` /
``Environment``.
"""

from environment.config import (
    RUNTIME_DOCKER,
    RUNTIME_LOCAL,
    EnvironmentConfig,
)
from environment.environment import Environment
from environment.envvars import DEFAULT_ENV_VAR_POLICY, EnvironmentVariablePolicy
from environment.metadata import WorkspaceMeta
from environment.network import DEFAULT_NETWORK_POLICY, NetworkPolicy, NetworkRule
from environment.process import ProcessResult, ProcessSpec, ProcessState
from environment.resources import ResourceLimits
from environment.runtime import EnvironmentRuntime, RuntimeStatus
from environment.states import EnvironmentState, require_transition, transition_allowed
from environment.status import WorkspaceStatus
from environment.workspace import Workspace

__all__ = [
    "DEFAULT_ENV_VAR_POLICY",
    "DEFAULT_NETWORK_POLICY",
    "Environment",
    "EnvironmentConfig",
    "EnvironmentRuntime",
    "EnvironmentState",
    "EnvironmentVariablePolicy",
    "NetworkPolicy",
    "NetworkRule",
    "ProcessResult",
    "ProcessSpec",
    "ProcessState",
    "RUNTIME_DOCKER",
    "RUNTIME_LOCAL",
    "ResourceLimits",
    "RuntimeStatus",
    "Workspace",
    "WorkspaceMeta",
    "WorkspaceStatus",
    "require_transition",
    "transition_allowed",
]
