"""Structured project metadata.

The scanner returns a ``ProjectManifest`` — a small, structured description of
what a repository is. We deliberately do NOT load file contents into context;
only names/types/paths are recorded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ManifestFile:
    """A detected manifest/lockfile/config file."""

    path: str  # relative to project root
    kind: str  # e.g. "python:pyproject", "node:package-json", "lockfile", "docker"
    exists: bool = True


@dataclass
class ProjectManifest:
    """Structured metadata describing a scanned project."""

    root: Path
    languages: list[str] = field(default_factory=list)
    package_managers: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    manifests: list[ManifestFile] = field(default_factory=list)
    test_systems: list[str] = field(default_factory=list)
    build_systems: list[str] = field(default_factory=list)
    git_repository: bool = False
    docker: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "root": str(self.root),
            "languages": self.languages,
            "package_managers": self.package_managers,
            "frameworks": self.frameworks,
            "manifests": [m.__dict__ for m in self.manifests],
            "test_systems": self.test_systems,
            "build_systems": self.build_systems,
            "git_repository": self.git_repository,
            "docker": self.docker,
        }
