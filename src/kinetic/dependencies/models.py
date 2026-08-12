"""Structured dependency detection result."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DependencyInfo:
    """One detected dependency ecosystem in a project."""

    ecosystem: str  # "python" | "node" | "rust" | "go"
    package_manager: str  # "uv" | "pip" | "poetry" | "npm" | "pnpm" | "yarn" | "cargo" | ...
    manifest: str  # manifest filename, relative to project root
    lockfile: str | None  # lockfile filename if present
    install_command: str  # the command to run to install deps
    project_dir: str  # directory the install command runs in

    def to_dict(self) -> dict[str, object]:
        return {
            "ecosystem": self.ecosystem,
            "package_manager": self.package_manager,
            "manifest": self.manifest,
            "lockfile": self.lockfile,
            "install_command": self.install_command,
            "project_dir": self.project_dir,
        }
