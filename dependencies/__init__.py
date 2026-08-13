"""Dependencies package."""

from dependencies.adapters import (
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
from dependencies.detector import detect_dependencies, detect_primary
from dependencies.installer import DependencyInstaller
from dependencies.models import DependencyInfo

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
