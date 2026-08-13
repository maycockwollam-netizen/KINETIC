"""Package-manager adapters.

Each adapter knows how to detect its ecosystem's manifest/lockfile from a
project root and produce the correct install command. Detection is file-based
(never guesses): an adapter only reports a match when the manifest exists.

Priority order within an ecosystem mirrors lockfile presence: a project with a
``uv.lock`` is treated as uv-managed; with ``poetry.lock`` as poetry; otherwise
falls back to pip + requirements.txt.
"""

from __future__ import annotations

from pathlib import Path

from kinetic.dependencies.models import DependencyInfo


class PackageManagerAdapter:
    """Base class: detect one ecosystem and produce install commands."""

    ecosystem: str = ""
    package_manager: str = ""

    def detect(self, root: Path) -> DependencyInfo | None:
        raise NotImplementedError

    def install_command(self, root: Path) -> str:
        raise NotImplementedError


class PipAdapter(PackageManagerAdapter):
    ecosystem = "python"
    package_manager = "pip"

    def detect(self, root: Path) -> DependencyInfo | None:
        if not (root / "requirements.txt").exists() and not (root / "setup.py").exists():
            return None
        # Pip is the fallback for plain python repos without uv/poetry.
        lockfile = None
        cmd = "pip install -r requirements.txt" if (root / "requirements.txt").exists() else "pip install -e ."
        return DependencyInfo(
            ecosystem=self.ecosystem,
            package_manager=self.package_manager,
            manifest="requirements.txt" if (root / "requirements.txt").exists() else "setup.py",
            lockfile=lockfile,
            install_command=cmd,
            project_dir=str(root),
        )

    def install_command(self, root: Path) -> str:
        return "pip install -r requirements.txt" if (root / "requirements.txt").exists() else "pip install -e ."


class UvAdapter(PackageManagerAdapter):
    ecosystem = "python"
    package_manager = "uv"

    def detect(self, root: Path) -> DependencyInfo | None:
        has_pyproject = (root / "pyproject.toml").exists()
        has_uv_lock = (root / "uv.lock").exists()
        if not has_pyproject and not has_uv_lock:
            return None
        return DependencyInfo(
            ecosystem=self.ecosystem,
            package_manager=self.package_manager,
            manifest="pyproject.toml",
            lockfile="uv.lock" if has_uv_lock else None,
            install_command="uv sync" if has_uv_lock else "uv pip install -e .",
            project_dir=str(root),
        )

    def install_command(self, root: Path) -> str:
        return "uv sync" if (root / "uv.lock").exists() else "uv pip install -e ."


class PoetryAdapter(PackageManagerAdapter):
    ecosystem = "python"
    package_manager = "poetry"

    def detect(self, root: Path) -> DependencyInfo | None:
        if not (root / "poetry.lock").exists():
            return None
        return DependencyInfo(
            ecosystem=self.ecosystem,
            package_manager=self.package_manager,
            manifest="pyproject.toml",
            lockfile="poetry.lock",
            install_command="poetry install --no-interaction",
            project_dir=str(root),
        )

    def install_command(self, root: Path) -> str:
        return "poetry install --no-interaction"


class NpmAdapter(PackageManagerAdapter):
    ecosystem = "node"
    package_manager = "npm"

    def detect(self, root: Path) -> DependencyInfo | None:
        if not (root / "package.json").exists():
            return None
        lockfile = "package-lock.json" if (root / "package-lock.json").exists() else None
        return DependencyInfo(
            ecosystem=self.ecosystem,
            package_manager=self.package_manager,
            manifest="package.json",
            lockfile=lockfile,
            install_command="npm ci" if lockfile else "npm install",
            project_dir=str(root),
        )

    def install_command(self, root: Path) -> str:
        return "npm ci" if (root / "package-lock.json").exists() else "npm install"


class PnpmAdapter(PackageManagerAdapter):
    ecosystem = "node"
    package_manager = "pnpm"

    def detect(self, root: Path) -> DependencyInfo | None:
        if not (root / "pnpm-lock.yaml").exists():
            return None
        return DependencyInfo(
            ecosystem=self.ecosystem,
            package_manager=self.package_manager,
            manifest="package.json",
            lockfile="pnpm-lock.yaml",
            install_command="pnpm install --frozen-lockfile",
            project_dir=str(root),
        )

    def install_command(self, root: Path) -> str:
        return "pnpm install --frozen-lockfile"


class YarnAdapter(PackageManagerAdapter):
    ecosystem = "node"
    package_manager = "yarn"

    def detect(self, root: Path) -> DependencyInfo | None:
        if not (root / "yarn.lock").exists():
            return None
        return DependencyInfo(
            ecosystem=self.ecosystem,
            package_manager=self.package_manager,
            manifest="package.json",
            lockfile="yarn.lock",
            install_command="yarn install --frozen-lockfile",
            project_dir=str(root),
        )

    def install_command(self, root: Path) -> str:
        return "yarn install --frozen-lockfile"


class CargoAdapter(PackageManagerAdapter):
    ecosystem = "rust"
    package_manager = "cargo"

    def detect(self, root: Path) -> DependencyInfo | None:
        if not (root / "Cargo.toml").exists():
            return None
        lockfile = "Cargo.lock" if (root / "Cargo.lock").exists() else None
        return DependencyInfo(
            ecosystem=self.ecosystem,
            package_manager=self.package_manager,
            manifest="Cargo.toml",
            lockfile=lockfile,
            install_command="cargo fetch",
            project_dir=str(root),
        )

    def install_command(self, root: Path) -> str:
        return "cargo fetch"


#: Ordered adapters: most specific (lockfile-bearing) first within an ecosystem.
ADAPTERS: list[PackageManagerAdapter] = [
    UvAdapter(),
    PoetryAdapter(),
    PipAdapter(),
    PnpmAdapter(),
    YarnAdapter(),
    NpmAdapter(),
    CargoAdapter(),
]
