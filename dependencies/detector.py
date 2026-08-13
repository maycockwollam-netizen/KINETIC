"""Dependency detection.

Inspects a project root with the registered adapters and returns the set of
detected dependency ecosystems. Detection is file-based — never guesses.
"""

from __future__ import annotations

from pathlib import Path

from dependencies.adapters import ADAPTERS, PackageManagerAdapter
from dependencies.models import DependencyInfo


def detect_dependencies(root: Path, *, adapters: list[PackageManagerAdapter] | None = None) -> list[DependencyInfo]:
    """Detect all dependency ecosystems present in ``root``.

    Adapters are checked in priority order (lockfile-bearing first within each
    ecosystem). For each ecosystem, only the first matching adapter reports a
    match — e.g. a project with both ``uv.lock`` and ``poetry.lock`` (unusual)
    reports uv only.
    """
    root = root.resolve()
    adapters = adapters or ADAPTERS
    seen_ecosystems: set[str] = set()
    results: list[DependencyInfo] = []
    for adapter in adapters:
        if adapter.ecosystem in seen_ecosystems:
            continue
        info = adapter.detect(root)
        if info is not None:
            results.append(info)
            seen_ecosystems.add(adapter.ecosystem)
    return results


def detect_primary(root: Path) -> DependencyInfo | None:
    """Return the first detected dependency ecosystem, or None."""
    deps = detect_dependencies(root)
    return deps[0] if deps else None
