"""Unit tests for dependency detection + installation."""

from __future__ import annotations

from pathlib import Path

import pytest

from dependencies import (
    DependencyInstaller,
    detect_dependencies,
    detect_primary,
)
from dependencies.adapters import NpmAdapter, UvAdapter
from errors import DependencyError, PermissionDeniedError
from events import EventBus, EventType
from security import AuditLog, PermissionPolicy


def _write(root: Path, name: str, content: str = "") -> None:
    (root / name).parent.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(content)


def test_detect_python_uv(tmp_path: Path):
    _write(tmp_path, "pyproject.toml")
    _write(tmp_path, "uv.lock")
    deps = detect_dependencies(tmp_path)
    assert len(deps) == 1
    assert deps[0].ecosystem == "python"
    assert deps[0].package_manager == "uv"
    assert deps[0].lockfile == "uv.lock"
    assert deps[0].install_command == "uv sync"


def test_detect_python_pip(tmp_path: Path):
    _write(tmp_path, "requirements.txt", "requests\n")
    deps = detect_dependencies(tmp_path)
    assert deps[0].package_manager == "pip"
    assert deps[0].install_command == "pip install -r requirements.txt"


def test_detect_node_pnpm_priority(tmp_path: Path):
    # pnpm-lock should report pnpm, not npm, even if package-lock also present.
    _write(tmp_path, "package.json")
    _write(tmp_path, "pnpm-lock.yaml")
    _write(tmp_path, "package-lock.json")
    deps = detect_dependencies(tmp_path)
    assert deps[0].ecosystem == "node"
    assert deps[0].package_manager == "pnpm"


def test_detect_rust_cargo(tmp_path: Path):
    _write(tmp_path, "Cargo.toml")
    deps = detect_dependencies(tmp_path)
    assert deps[0].package_manager == "cargo"
    assert deps[0].install_command == "cargo fetch"


def test_detect_multi_ecosystem(tmp_path: Path):
    _write(tmp_path, "pyproject.toml")
    _write(tmp_path, "uv.lock")
    _write(tmp_path, "package.json")
    _write(tmp_path, "pnpm-lock.yaml")
    _write(tmp_path, "Cargo.toml")
    deps = detect_dependencies(tmp_path)
    ecosystems = {d.ecosystem for d in deps}
    assert ecosystems == {"python", "node", "rust"}


def test_detect_none(tmp_path: Path):
    assert detect_dependencies(tmp_path) == []
    assert detect_primary(tmp_path) is None


def test_detect_primary(tmp_path: Path):
    _write(tmp_path, "pyproject.toml")
    _write(tmp_path, "uv.lock")
    primary = detect_primary(tmp_path)
    assert primary is not None
    assert primary.package_manager == "uv"


def test_uv_adapter_no_manifest(tmp_path: Path):
    assert UvAdapter().detect(tmp_path) is None


def test_npm_adapter_install_command(tmp_path: Path):
    _write(tmp_path, "package.json")
    _write(tmp_path, "package-lock.json")
    assert NpmAdapter().install_command(tmp_path) == "npm ci"


@pytest.mark.timeout(20)
async def test_installer_permission_denied(tmp_path: Path):
    _write(tmp_path, "requirements.txt", "echo\n")
    audit = AuditLog(tmp_path / "audit.log")
    policy = PermissionPolicy(writable_roots=[tmp_path], allow_dependency_install=False)
    installer = DependencyInstaller(workspace=tmp_path, policy=policy, audit=audit)
    with pytest.raises(PermissionDeniedError):
        await installer.install()
    # Denial audited.
    entries = audit.read()
    assert any(e["action"] == "dependency_install" and e["allowed"] is False for e in entries)


@pytest.mark.timeout(20)
async def test_installer_no_dependencies_detected(tmp_path: Path):
    audit = AuditLog(tmp_path / "audit.log")
    policy = PermissionPolicy(writable_roots=[tmp_path], allow_dependency_install=True)
    installer = DependencyInstaller(workspace=tmp_path, policy=policy, audit=audit)
    with pytest.raises(DependencyError, match="no dependencies detected"):
        await installer.install()


@pytest.mark.timeout(20)
async def test_installer_runs_command_and_emits_events(tmp_path: Path):
    # Use a python project whose "install" is pip install -r requirements.txt.
    # We make requirements install a trivially available, safe package (echo).
    _write(tmp_path, "requirements.txt", "")  # empty -> pip install -r exits 0
    audit = AuditLog(tmp_path / "audit.log")
    events = EventBus()
    policy = PermissionPolicy(writable_roots=[tmp_path], allow_dependency_install=True)
    installer = DependencyInstaller(
        workspace=tmp_path, policy=policy, audit=audit, events=events, default_timeout=30
    )
    result = await installer.install()
    assert result["exit_code"] == 0
    # Events emitted.
    types = [e.type for e in events.history]
    assert EventType.DEPENDENCY_INSTALL_STARTED in types
    assert EventType.DEPENDENCY_INSTALL_FINISHED in types
    # Allow audited.
    assert any(e["action"] == "dependency_install" and e["allowed"] is True for e in audit.read())


@pytest.mark.timeout(20)
async def test_installer_rejects_outside_workspace(tmp_path: Path):
    other = tmp_path / "other"
    other.mkdir()
    _write(other, "requirements.txt")
    audit = AuditLog(tmp_path / "audit.log")
    policy = PermissionPolicy(writable_roots=[tmp_path], allow_dependency_install=True)
    installer = DependencyInstaller(workspace=tmp_path, policy=policy, audit=audit)
    from dependencies.detector import detect_primary

    info = detect_primary(other)
    with pytest.raises(DependencyError, match="outside the managed workspace"):
        await installer.install(info)


@pytest.mark.timeout(20)
async def test_installer_failure_raises_structured_error(tmp_path: Path):
    # requirements referencing a nonexistent package -> pip fails fast.
    _write(tmp_path, "requirements.txt", "this_package_does_not_exist_xyz==9999.0\n")
    audit = AuditLog(tmp_path / "audit.log")
    policy = PermissionPolicy(writable_roots=[tmp_path], allow_dependency_install=True)
    installer = DependencyInstaller(workspace=tmp_path, policy=policy, audit=audit, default_timeout=30)
    with pytest.raises(DependencyError) as exc_info:
        await installer.install()
    assert exc_info.value.exit_code is not None
    assert exc_info.value.ecosystem == "python"
