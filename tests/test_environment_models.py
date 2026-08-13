"""Unit tests for environment domain models: states, network, resources, envvars, process, config."""

from __future__ import annotations

from pathlib import Path

import pytest

from environment.config import RUNTIME_DOCKER, RUNTIME_LOCAL, EnvironmentConfig
from environment.envvars import EnvironmentVariablePolicy
from environment.network import DEFAULT_NETWORK_POLICY, NetworkPolicy, NetworkRule
from environment.process import ProcessSpec, ProcessState
from environment.resources import ResourceLimits
from environment.states import (
    EnvironmentState,
    require_transition,
    transition_allowed,
)
from errors import EnvironmentStateError

# --- state machine -----------------------------------------------------------


def test_state_machine_valid_transitions():
    assert transition_allowed(EnvironmentState.CREATING, EnvironmentState.READY)
    assert transition_allowed(EnvironmentState.READY, EnvironmentState.RUNNING)
    assert transition_allowed(EnvironmentState.RUNNING, EnvironmentState.STOPPING)
    assert transition_allowed(EnvironmentState.STOPPING, EnvironmentState.STOPPED)
    assert transition_allowed(EnvironmentState.STOPPED, EnvironmentState.DESTROYED)
    assert transition_allowed(EnvironmentState.CREATING, EnvironmentState.FAILED)
    assert transition_allowed(EnvironmentState.FAILED, EnvironmentState.DESTROYED)
    # STOPPED can be re-started.
    assert transition_allowed(EnvironmentState.STOPPED, EnvironmentState.READY)


def test_state_machine_invalid_transitions():
    assert not transition_allowed(EnvironmentState.CREATING, EnvironmentState.RUNNING)
    assert not transition_allowed(EnvironmentState.READY, EnvironmentState.STOPPING)
    assert not transition_allowed(EnvironmentState.RUNNING, EnvironmentState.READY)
    assert not transition_allowed(EnvironmentState.DESTROYED, EnvironmentState.READY)


def test_require_transition_raises():
    with pytest.raises(EnvironmentStateError, match="invalid environment state transition"):
        require_transition(EnvironmentState.CREATING, EnvironmentState.RUNNING)


def test_destroyed_is_terminal():
    assert not transition_allowed(EnvironmentState.DESTROYED, EnvironmentState.READY)
    assert not transition_allowed(EnvironmentState.DESTROYED, EnvironmentState.FAILED)


# --- network -----------------------------------------------------------------


def test_default_network_policy_is_deny():
    assert DEFAULT_NETWORK_POLICY is NetworkPolicy.DENY


def test_network_rule_requires_host():
    with pytest.raises(ValueError):
        NetworkRule(host="")
    r = NetworkRule(host="example.com", port=443)
    assert r.to_dict() == {"host": "example.com", "port": 443}
    assert NetworkRule(host="x").port is None


# --- resources ---------------------------------------------------------------


def test_resource_limits_requested_fields():
    rl = ResourceLimits(cpu=1.0, memory_bytes=512)
    assert rl.requested_fields() == ["cpu", "memory_bytes"]
    assert ResourceLimits().requested_fields() == []


def test_resource_limits_default_has_timeout():
    rl = ResourceLimits.default()
    assert rl.execution_timeout == 600.0
    assert rl.cpu is None


# --- environment variables ----------------------------------------------------


def test_env_var_policy_filters_denied():
    pol = EnvironmentVariablePolicy(allowed={"PATH", "MY_SECRET_API_KEY"})
    out = pol.filter({"PATH": "/usr/bin", "MY_SECRET_API_KEY": "sk-abc", "HOME": "/root"})
    assert out == {"PATH": "/usr/bin"}  # secret-name denied, HOME denied


def test_env_var_policy_injects():
    pol = EnvironmentVariablePolicy(inject={"FOO": "bar"})
    out = pol.filter({})
    assert out == {"FOO": "bar"}


def test_env_var_policy_inject_overrides_host():
    pol = EnvironmentVariablePolicy(allowed={"X"}, inject={"X": "injected"})
    out = pol.filter({"X": "host"})
    assert out["X"] == "injected"


def test_env_var_policy_no_host_forwarding_by_default():
    pol = EnvironmentVariablePolicy()
    out = pol.filter({"PATH": "/x", "HOME": "/root"})
    assert out == {}  # nothing forwarded without allowlist


def test_env_var_policy_redacts_secret_values():
    """A non-secret-named var whose value looks like a secret is value-redacted."""
    pol = EnvironmentVariablePolicy(allowed={"MY_CONFIG"})
    out = pol.filter({"MY_CONFIG": "sk-abcdefghijklmnopqrstuvwxyz123456"})
    assert "redacted" in out["MY_CONFIG"]


def test_env_var_policy_default_denies_cloud_creds():
    pol = EnvironmentVariablePolicy(allowed={"AWS_ACCESS_KEY_ID", "PATH"})
    out = pol.filter({"AWS_ACCESS_KEY_ID": "AKIA...", "PATH": "/x"})
    assert "AWS_ACCESS_KEY_ID" not in out
    assert out == {"PATH": "/x"}


def test_is_secret_name():
    assert EnvironmentVariablePolicy.is_secret_name("MY_API_KEY")
    assert EnvironmentVariablePolicy.is_secret_name("DATABASE_PASSWORD")
    assert not EnvironmentVariablePolicy.is_secret_name("PATH")


# --- process ------------------------------------------------------------------


def test_process_spec_to_dict_hides_env_values():
    spec = ProcessSpec(command="ls", env={"SECRET": "shh", "PATH": "/x"}, timeout=5)
    d = spec.to_dict()
    assert d["env_keys"] == ["PATH", "SECRET"]
    assert "SECRET" not in d or d.get("SECRET") != "shh"


def test_process_state_values():
    assert ProcessState.COMPLETED != ProcessState.TIMED_OUT
    assert ProcessState.CANCELLED != ProcessState.FAILED


# --- config ------------------------------------------------------------------


def test_config_safe_defaults_local_deny():
    cfg = EnvironmentConfig.safe_defaults()
    assert cfg.runtime_type == RUNTIME_LOCAL
    assert cfg.network is NetworkPolicy.DENY


def test_config_docker_defaults_isolated():
    cfg = EnvironmentConfig.docker_defaults()
    assert cfg.runtime_type == RUNTIME_DOCKER
    assert cfg.network is NetworkPolicy.DENY
    assert cfg.sandbox_mode is True


def test_config_unknown_runtime_raises(tmp_path: Path):
    from environment import Environment
    from errors import SandboxError

    cfg = EnvironmentConfig(runtime_type="kubernetes")
    with pytest.raises(SandboxError, match="unknown runtime type"):
        Environment.create(tmp_path / "ws", cfg)
