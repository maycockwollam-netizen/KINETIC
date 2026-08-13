"""Phase 7 — configuration hardening tests.

Verifies bounded numeric limits, env-var precedence, config-file loading, and
that invalid values fail early (no silent fallback).
"""

from __future__ import annotations

import json
from pathlib import Path

import pydantic
import pytest

from config import Settings, load_settings
from errors import ConfigError


class TestConfigBounds:
    """Every untrusted quantity must have a bound; invalid values raise."""

    @pytest.mark.parametrize("field,value", [
        ("max_step_attempts", -1),
        ("max_task_attempts", -1),
        ("max_replans", -1),
        ("max_repair_attempts", -1),
        ("max_verification_attempts", -1),
        ("max_total_recovery_attempts", -1),
    ])
    def test_negative_retries_rejected(self, field: str, value: int) -> None:
        with pytest.raises((pydantic.ValidationError, ValueError, Exception)):
            Settings(**{field: value})

    @pytest.mark.parametrize("field,value", [
        ("max_step_attempts", 100),
        ("max_task_attempts", 100),
        ("max_replans", 100),
        ("max_repair_attempts", 100),
    ])
    def test_excessive_retries_rejected(self, field: str, value: int) -> None:
        with pytest.raises((pydantic.ValidationError, ValueError, Exception)):
            Settings(**{field: value})

    @pytest.mark.parametrize("field,value", [
        ("default_command_timeout", 0),
        ("default_command_timeout", -5),
        ("max_command_timeout", 0),
        ("execution_timeout", 0),
        ("execution_timeout", -1),
    ])
    def test_nonpositive_timeout_rejected(self, field: str, value: float) -> None:
        with pytest.raises((pydantic.ValidationError, ValueError, Exception)):
            Settings(**{field: value})

    def test_timeout_cap_24h(self) -> None:
        with pytest.raises((pydantic.ValidationError, ValueError, Exception)):
            Settings(max_command_timeout=100_000)
        with pytest.raises((pydantic.ValidationError, ValueError, Exception)):
            Settings(execution_timeout=100_000)

    def test_default_exceeds_max_rejected(self) -> None:
        with pytest.raises((pydantic.ValidationError, ValueError, Exception)):
            Settings(default_command_timeout=200, max_command_timeout=100)

    @pytest.mark.parametrize("field,value", [
        ("max_plan_steps", 0),
        ("max_plan_steps", 200),
        ("max_plan_dependencies", 0),
        ("max_plan_dependencies", 100),
        ("observation_max_stdout_chars", 0),
        ("context_max_memory_items", 0),
        ("memory_candidate_limit", 0),
        ("embedding_dimensions", 0),
        ("embedding_dimensions", 9999),
    ])
    def test_invalid_limits_rejected(self, field: str, value: int) -> None:
        with pytest.raises((pydantic.ValidationError, ValueError, Exception)):
            Settings(**{field: value})

    def test_weights_all_zero_rejected(self) -> None:
        with pytest.raises((pydantic.ValidationError, ValueError, Exception)):
            Settings(semantic_weight=0, lexical_weight=0,
                     recency_weight=0, importance_weight=0)

    def test_negative_weight_rejected(self) -> None:
        with pytest.raises((pydantic.ValidationError, ValueError, Exception)):
            Settings(semantic_weight=-0.1)

    def test_invalid_network_policy_rejected(self) -> None:
        with pytest.raises((pydantic.ValidationError, ValueError, Exception)):
            Settings(network_policy="open")

    def test_invalid_runtime_type_rejected(self) -> None:
        with pytest.raises((pydantic.ValidationError, ValueError, Exception)):
            Settings(runtime_type="kubernetes")

    def test_invalid_permission_mode_rejected(self) -> None:
        with pytest.raises((pydantic.ValidationError, ValueError, Exception)):
            Settings(permission_mode="dangerous")

    def test_negative_memory_limit_rejected(self) -> None:
        with pytest.raises((pydantic.ValidationError, ValueError, Exception)):
            Settings(memory_limit_mb=-1)

    def test_zero_cpu_rejected(self) -> None:
        with pytest.raises((pydantic.ValidationError, ValueError, Exception)):
            Settings(cpu_limit=0)


class TestEnvVarPrecedence:
    """Environment variables override built-in defaults."""

    def test_env_var_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KINETIC_MAX_TURNS", "7")
        s = Settings()
        assert s.max_turns == 7

    def test_env_var_for_network(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KINETIC_NETWORK_POLICY", "allow")
        s = Settings()
        assert s.network_policy == "allow"

    def test_env_var_invalid_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KINETIC_NETWORK_POLICY", "bogus")
        with pytest.raises((pydantic.ValidationError, ValueError, Exception)):
            Settings()


class TestConfigFile:
    """Config file loading + precedence."""

    def test_from_file_loads(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"max_turns": 15, "network_policy": "allow"}))
        s = Settings.from_file(cfg)
        assert s.max_turns == 15
        assert s.network_policy == "allow"

    def test_from_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not found"):
            Settings.from_file(tmp_path / "missing.json")

    def test_from_file_invalid_json(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.json"
        cfg.write_text("{not json")
        with pytest.raises(ConfigError, match="not valid JSON"):
            Settings.from_file(cfg)

    def test_from_file_invalid_value(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"max_step_attempts": -1}))
        with pytest.raises(ConfigError, match="invalid configuration"):
            Settings.from_file(cfg)

    def test_load_settings_no_file(self) -> None:
        s = load_settings()
        assert isinstance(s, Settings)

    def test_load_settings_env_overrides_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"max_turns": 10}))
        monkeypatch.setenv("KINETIC_MAX_TURNS", "20")
        s = load_settings(cfg)
        assert s.max_turns == 20
