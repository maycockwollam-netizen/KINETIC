"""Dependencies package."""

from kinetic.dependencies.adapters import (
    ADAPTERS,
    CargoAdapter,
    NpmAdapter,
    PackageManagerAdapter,
    PipAdapter,
    PnpmAdapter,
    PoetryAdapter,
    UvAdapter,
    YarnAdapter,
)
from kinetic.dependencies.detector import detect_dependencies, detect_primary
from kinetic.dependencies.installer import DependencyInstaller
from kinetic.dependencies.models import DependencyInfo

__all__ = [
    "ADAPTERS",
    "CargoAdapter",
    "DependencyInfo",
    "DependencyInstaller",
    "NpmAdapter",
    "PackageManagerAdapter",
    "PipAdapter",
    "PnpmAdapter",
    "PoetryAdapter",
    "UvAdapter",
    "YarnAdapter",
    "detect_dependencies",
    "detect_primary",
]
