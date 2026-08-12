"""Shared path-safety utilities.

Centralizing path resolution keeps the workspace boundary enforcement in one
place: every subsystem (workspace, filesystem/git/dependency tools) resolves
paths the same way and rejects traversal/symlink-escape attempts identically.
"""

from __future__ import annotations

from pathlib import Path

from kinetic.errors import SecurityError


def safe_resolve(root: Path, path: str | Path) -> Path:
    """Resolve ``path`` against ``root`` and reject anything escaping ``root``.

    Handles:
      * relative paths (resolved against root)
      * absolute paths inside root (allowed)
      * absolute paths outside root (rejected)
      * ``..`` traversal (rejected)
      * symlinks that escape root (rejected, after resolution)

    Raises ``SecurityError`` on any escape attempt.
    """
    root = root.resolve()
    p = Path(path)
    if not p.is_absolute():
        p = root / p
    resolved = p.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise SecurityError(
            f"path traversal blocked: {path!r} resolves to {resolved} outside {root}"
        ) from exc
    return resolved


def is_within(root: Path, path: Path) -> bool:
    """True if ``path`` resolves inside ``root`` (no exceptions)."""
    try:
        root = root.resolve()
        resolved = path.resolve(strict=False)
        resolved.relative_to(root)
        return True
    except (ValueError, OSError, RuntimeError):
        return False
