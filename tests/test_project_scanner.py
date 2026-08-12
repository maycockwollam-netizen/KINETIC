"""Unit tests for project scanning."""

from __future__ import annotations

import subprocess
from pathlib import Path

from kinetic.project import find_project_root, scan_project


def _write(root: Path, name: str, content: str = "") -> None:
    (root / name).parent.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(content)


def test_find_project_root_git(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    sub = root / "src" / "deep"
    sub.mkdir(parents=True)
    assert find_project_root(sub) == root.resolve()


def test_find_project_root_no_git(tmp_path: Path):
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    assert find_project_root(sub) == sub.resolve()


def test_scan_empty_dir(tmp_path: Path):
    m = scan_project(tmp_path)
    assert m.languages == []
    assert m.package_managers == []
    assert m.git_repository is False
    assert m.docker is False


def test_scan_python_uv(tmp_path: Path):
    _write(tmp_path, "pyproject.toml", '[project]\nname="x"\n')
    _write(tmp_path, "uv.lock")
    m = scan_project(tmp_path)
    assert "python" in m.languages
    assert "uv" in m.package_managers
    kinds = [mf.kind for mf in m.manifests]
    assert "python:pyproject" in kinds
    assert "python:uv-lock" in kinds


def test_scan_python_pip(tmp_path: Path):
    _write(tmp_path, "requirements.txt", "requests==2.0\n")
    _write(tmp_path, "setup.py", "from setuptools import setup\n")
    m = scan_project(tmp_path)
    assert "python" in m.languages
    assert "pip" in m.package_managers
    assert "python:requirements" in [mf.kind for mf in m.manifests]


def test_scan_python_poetry(tmp_path: Path):
    _write(tmp_path, "pyproject.toml")
    _write(tmp_path, "poetry.lock")
    m = scan_project(tmp_path)
    assert "poetry" in m.package_managers


def test_scan_node_npm(tmp_path: Path):
    _write(tmp_path, "package.json", '{"name":"x"}\n')
    _write(tmp_path, "package-lock.json")
    m = scan_project(tmp_path)
    assert "node" in m.languages
    assert "npm" in m.package_managers
    assert "node:package-json" in [mf.kind for mf in m.manifests]


def test_scan_node_pnpm(tmp_path: Path):
    _write(tmp_path, "package.json")
    _write(tmp_path, "pnpm-lock.yaml")
    m = scan_project(tmp_path)
    assert "pnpm" in m.package_managers


def test_scan_node_yarn(tmp_path: Path):
    _write(tmp_path, "package.json")
    _write(tmp_path, "yarn.lock")
    m = scan_project(tmp_path)
    assert "yarn" in m.package_managers


def test_scan_rust(tmp_path: Path):
    _write(tmp_path, "Cargo.toml", "[package]\nname=\"x\"\n")
    _write(tmp_path, "Cargo.lock")
    m = scan_project(tmp_path)
    assert "rust" in m.languages
    assert "cargo" in m.package_managers
    assert "rust:cargo-toml" in [mf.kind for mf in m.manifests]


def test_scan_docker(tmp_path: Path):
    _write(tmp_path, "Dockerfile", "FROM scratch\n")
    _write(tmp_path, "docker-compose.yml", "services: {}\n")
    m = scan_project(tmp_path)
    assert m.docker is True
    assert "docker:dockerfile" in [mf.kind for mf in m.manifests]
    assert "docker:compose" in [mf.kind for mf in m.manifests]


def test_scan_git_repository(tmp_path: Path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    m = scan_project(tmp_path)
    assert m.git_repository is True


def test_scan_build_and_test_systems(tmp_path: Path):
    _write(tmp_path, "Makefile", "all:\n")
    _write(tmp_path, "pytest.ini", "[pytest]\n")
    m = scan_project(tmp_path)
    assert "make" in m.build_systems
    assert "pytest" in m.test_systems


def test_scan_frameworks(tmp_path: Path):
    _write(tmp_path, "next.config.js")
    m = scan_project(tmp_path)
    assert "next.js" in m.frameworks


def test_scan_to_dict(tmp_path: Path):
    _write(tmp_path, "pyproject.toml")
    m = scan_project(tmp_path)
    d = m.to_dict()
    assert d["root"] == str(tmp_path.resolve())
    assert "python" in d["languages"]
    assert isinstance(d["manifests"], list)
