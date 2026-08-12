"""Filesystem tools: read, write, edit, list, search — workspace-scoped.

All paths are resolved against the workspace root and checked to stay inside it,
preventing path traversal. The permission policy also checks writable roots, so
defense is layered (tool + policy), not prompt-based.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kinetic.errors import SecurityError, ToolError
from kinetic.security.policy import FILE_WRITE, READ_ONLY
from kinetic.tools.base import ToolDefinition, tool_result


def _resolve(root: Path, path: str) -> Path:
    """Resolve ``path`` against ``root`` and ensure it stays inside ``root``."""
    root = root.resolve()
    candidate = (root / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise SecurityError(f"path traversal blocked: {path} is outside {root}") from exc
    return candidate


class FilesystemTools:
    """Filesystem operations scoped to a single workspace root."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    # --- read -----------------------------------------------------------------
    async def read_file(self, args: dict[str, Any]) -> dict[str, Any]:
        path = _require(args, "path")
        target = _resolve(self._root, path)
        if not target.exists():
            raise ToolError("read_file", f"not found: {path}")
        if target.is_dir():
            raise ToolError("read_file", f"is a directory: {path}")
        text = target.read_text(encoding="utf-8", errors="replace")
        return tool_result(text)

    # --- write ----------------------------------------------------------------
    async def write_file(self, args: dict[str, Any]) -> dict[str, Any]:
        path = _require(args, "path")
        content = args.get("content", "")
        target = _resolve(self._root, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return tool_result(f"wrote {len(content)} bytes to {path}")

    # --- edit (line-based replace) -------------------------------------------
    async def edit_file(self, args: dict[str, Any]) -> dict[str, Any]:
        path = _require(args, "path")
        old = args.get("old_str")
        new = args.get("new_str", "")
        if not isinstance(old, str) or not old:
            raise ToolError("edit_file", "requires non-empty 'old_str'")
        target = _resolve(self._root, path)
        if not target.exists():
            raise ToolError("edit_file", f"not found: {path}")
        text = target.read_text(encoding="utf-8")
        occurrences = text.count(old)
        if occurrences == 0:
            raise ToolError("edit_file", f"old_str not found in {path}")
        if occurrences > 1:
            raise ToolError("edit_file", f"old_str matches {occurrences} times; must be unique")
        new_text = text.replace(old, new, 1)
        target.write_text(new_text, encoding="utf-8")
        return tool_result(f"edited {path}: replaced 1 occurrence")

    # --- list directory ------------------------------------------------------
    async def list_dir(self, args: dict[str, Any]) -> dict[str, Any]:
        path = args.get("path", ".")
        target = _resolve(self._root, path)
        if not target.exists():
            raise ToolError("list_dir", f"not found: {path}")
        if not target.is_dir():
            raise ToolError("list_dir", f"not a directory: {path}")
        entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name))
        lines = [f"{'d' if e.is_dir() else 'f'}  {e.name}" for e in entries]
        return tool_result("\n".join(lines) if lines else "(empty)")

    # --- search (recursive text grep) ---------------------------------------
    async def search_files(self, args: dict[str, Any]) -> dict[str, Any]:
        pattern = _require(args, "pattern")
        path = args.get("path", ".")
        target = _resolve(self._root, path)
        matches: list[str] = []
        for file in _iter_files(target):
            try:
                text = file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if pattern in line:
                    rel = file.relative_to(self._root)
                    matches.append(f"{rel}:{i}: {line.strip()[:200]}")
                    if len(matches) >= 100:
                        matches.append("... (truncated at 100 matches)")
                        return tool_result("\n".join(matches))
        return tool_result("\n".join(matches) if matches else "(no matches)")


def _require(args: dict[str, Any], key: str) -> str:
    val = args.get(key)
    if not isinstance(val, str) or not val:
        raise ToolError("filesystem", f"missing required argument '{key}'")
    return val


def _iter_files(root: Path):
    skip = {".git", "__pycache__", "node_modules", ".venv", ".uv", "dist", "build"}
    for path in root.rglob("*"):
        if any(part in skip for part in path.parts):
            continue
        if path.is_file():
            yield path


# --- Schemas ------------------------------------------------------------------

_READ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"path": {"type": "string", "description": "Path relative to the workspace root."}},
    "required": ["path"],
}

_WRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "Path relative to the workspace root."},
        "content": {"type": "string", "description": "Full file content to write."},
    },
    "required": ["path", "content"],
}

_EDIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "old_str": {"type": "string", "description": "Exact text to replace (must be unique)."},
        "new_str": {"type": "string", "description": "Replacement text."},
    },
    "required": ["path", "old_str", "new_str"],
}

_LIST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"path": {"type": "string", "default": "."}},
}

_SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pattern": {"type": "string", "description": "Substring to search for in file contents."},
        "path": {"type": "string", "default": "."},
    },
    "required": ["pattern"],
}


def filesystem_tools(root: Path) -> list[ToolDefinition]:
    """Build the filesystem tool set scoped to ``root``."""
    fs = FilesystemTools(root)
    return [
        ToolDefinition("read_file", "Read a file's text contents.", _READ_SCHEMA, READ_ONLY, fs.read_file),
        ToolDefinition("list_dir", "List directory entries.", _LIST_SCHEMA, READ_ONLY, fs.list_dir),
        ToolDefinition("search_files", "Recursively search file contents for a substring.", _SEARCH_SCHEMA, READ_ONLY, fs.search_files),
        ToolDefinition("write_file", "Create or overwrite a file.", _WRITE_SCHEMA, FILE_WRITE, fs.write_file),
        ToolDefinition("edit_file", "Replace a unique string in a file.", _EDIT_SCHEMA, FILE_WRITE, fs.edit_file),
    ]
