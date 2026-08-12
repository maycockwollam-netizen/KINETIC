"""Typed configuration for a sandboxed environment.

A single ``EnvironmentConfig`` bundles the runtime type, network policy,
resource limits, environment-variable policy, and execution defaults. It is
constructed from the global :class:`~kinetic.config.Settings` (or directly for
tests) and injected into an :class:`~kinetic.environment.environment.Environment`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kinetic.environment.envvars import EnvironmentVariablePolicy
from kinetic.environment.network import DEFAULT_NETWORK_POLICY, NetworkPolicy, NetworkRule
from kinetic.environment.resources import ResourceLimits

#: Runtime implementations. Only the runtime module knows what these mean.
RUNTIME_LOCAL = "local"
RUNTIME_DOCKER = "docker"


@dataclass(frozen=True)
class EnvironmentConfig:
    """All knobs for one sandboxed environment."""

    runtime_type: str = RUNTIME_LOCAL
    #: Whether to run in a strict sandbox (isolated) or a loose dev mode.
    sandbox_mode: bool = True
    network: NetworkPolicy = DEFAULT_NETWORK_POLICY
    #: Egress allowlist for ``RESTRICTED`` network mode.
    network_rules: tuple[NetworkRule, ...] = field(default_factory=tuple)
    resources: ResourceLimits = field(default_factory=ResourceLimits.default)
    env_vars: EnvironmentVariablePolicy = field(default_factory=EnvironmentVariablePolicy)
    #: Extra host directories explicitly allowed to be mounted (sandbox only).
    extra_mounts: tuple[str, ...] = field(default_factory=tuple)
    #: Docker image used by the docker runtime.
    image: str = "python:3.11-slim"
    #: Optional label for audit/event correlation.
    label: str = "environment"

    @staticmethod
    def safe_defaults() -> EnvironmentConfig:
        """Defaults that fail closed: local runtime, deny everything sensitive.

        Local runtime with network DENY is honest about *not* isolating network;
        callers wanting true isolation use the docker runtime. See the runtime
        module for how each policy is enforced.
        """
        return EnvironmentConfig(
            runtime_type=RUNTIME_LOCAL,
            sandbox_mode=False,
            network=DEFAULT_NETWORK_POLICY,
            resources=ResourceLimits.default(),
            env_vars=EnvironmentVariablePolicy(),
        )

    @staticmethod
    def docker_defaults(image: str = "python:3.11-slim") -> EnvironmentConfig:
        """Defaults for a real isolated docker sandbox."""
        return EnvironmentConfig(
            runtime_type=RUNTIME_DOCKER,
            sandbox_mode=True,
            network=DEFAULT_NETWORK_POLICY,
            resources=ResourceLimits.default(),
            env_vars=EnvironmentVariablePolicy(),
            image=image,
        )
