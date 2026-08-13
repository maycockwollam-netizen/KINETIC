"""Verifier tests: PASS / FAIL / INCONCLUSIVE, project-specific commands."""

from __future__ import annotations

from pathlib import Path

import pytest

from kinetic.environment import Environment
from kinetic.environment.config import RUNTIME_LOCAL, EnvironmentConfig
from kinetic.environment.network import NetworkPolicy
from kinetic.project.models import ManifestFile, ProjectManifest
from kinetic.tasks.policies import VerificationOutcome
from kinetic.tasks.verifier import Verifier, command_for_manifest


def _manifest(**kw) -> ProjectManifest:
    return ProjectManifest(root=Path("/ws"), **kw)


@pytest.fixture
async def env(tmp_path: Path) -> Environment:
    cfg = EnvironmentConfig(runtime_type=RUNTIME_LOCAL, sandbox_mode=False, network=NetworkPolicy.ALLOW)
    e = Environment.create(tmp_path / "ws", cfg, session_id="v")
    await e.provision()
    yield e
    await e.destroy()


class TestCommandResolution:
    def test_configured_command_wins(self) -> None:
        m = _manifest(test_systems=["pytest"])
        assert command_for_manifest(m, configured="custom run") == "custom run"

    def test_no_manifest_returns_none(self) -> None:
        assert command_for_manifest(None) is None

    def test_pytest_default(self) -> None:
        m = _manifest(test_systems=["pytest"], languages=["python"],
                      manifests=[ManifestFile(path="pyproject.toml", kind="python:pyproject")])
        assert command_for_manifest(m) == "uv run pytest -q"

    def test_jest_default(self) -> None:
        m = _manifest(test_systems=["jest"], languages=["node"])
        assert command_for_manifest(m) == "npm test"

    def test_rust_fallback(self) -> None:
        m = _manifest(languages=["rust"], manifests=[ManifestFile(path="Cargo.toml", kind="rust:cargo-toml")])
        assert command_for_manifest(m) == "cargo test"

    def test_go_fallback(self) -> None:
        m = _manifest(languages=["go"], package_managers=["go"])
        assert command_for_manifest(m) == "go test ./..."

    def test_no_known_system_returns_none(self) -> None:
        m = _manifest(test_systems=["unknownframework"])
        assert command_for_manifest(m) is None


class TestVerifier:
    async def test_pass_on_zero_exit(self, env: Environment) -> None:
        v = Verifier(environment=env, configured_command="true")
        result = await v.verify()
        assert result.outcome is VerificationOutcome.PASS
        assert result.exit_code == 0

    async def test_fail_on_nonzero_exit(self, env: Environment) -> None:
        v = Verifier(environment=env, configured_command="false")
        result = await v.verify()
        assert result.outcome is VerificationOutcome.FAIL

    async def test_inconclusive_when_no_command(self, env: Environment) -> None:
        v = Verifier(environment=env, configured_command=None, manifest=None)
        result = await v.verify()
        assert result.outcome is VerificationOutcome.INCONCLUSIVE

    async def test_inconclusive_when_no_environment(self) -> None:
        v = Verifier(environment=None, configured_command="pytest")
        result = await v.verify()
        assert result.outcome is VerificationOutcome.INCONCLUSIVE

    async def test_never_pretends_success(self, env: Environment) -> None:
        # A command that doesn't exist -> nonzero exit -> FAIL, never PASS.
        v = Verifier(environment=env, configured_command="this-command-does-not-exist-xyz")
        result = await v.verify()
        assert result.outcome is VerificationOutcome.FAIL

    async def test_verification_goes_through_environment_permission(self, tmp_path: Path) -> None:
        # An environment whose ENVIRONMENT_EXEC is denied must raise / deny,
        # proving verification never bypasses the security boundary.
        from kinetic.security import PermissionPolicy

        policy = PermissionPolicy(allow_environment_exec=False)
        cfg = EnvironmentConfig(runtime_type=RUNTIME_LOCAL, sandbox_mode=False, network=NetworkPolicy.ALLOW)
        env = Environment.create(tmp_path / "ws", cfg, policy=policy, session_id="v")
        await env.provision()
        try:
            v = Verifier(environment=env, configured_command="true")
            from kinetic.errors import PermissionDeniedError, VerificationError

            with pytest.raises((VerificationError, PermissionDeniedError)):
                await v.verify()
        finally:
            await env.destroy()
