"""File-upload persistence for the web console.

All filesystem mutation for uploaded files lives here (never in ``web/*.py``,
which the security test forbids). Uploaded files land in a bounded
``.kinetic_uploads`` directory inside the workspace so the agent can read them
during its task. Names are sanitized to prevent traversal.
"""

from __future__ import annotations

import contextlib
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from .models import FileEntry

if TYPE_CHECKING:
    from .json_store import JsonStore

#: Cap on a single upload size (bytes).
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MiB


def _safe_name(name: str) -> str:
    """Reject path separators / hidden names; return a basename-only name."""
    if not name or name.startswith(".") or "/" in name or "\\" in name or "\x00" in name:
        raise ValueError("invalid file name")
    base = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if not base or base.startswith("."):
        raise ValueError("invalid file name")
    return base


async def save_upload_file(
    *,
    store: JsonStore[FileEntry],
    workspace: Path,
    name: str,
    content: bytes,
    content_type: str = "",
) -> FileEntry:
    """Write an uploaded blob to the workspace upload dir and record it."""
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError(f"upload too large (max {MAX_UPLOAD_BYTES} bytes)")
    safe = _safe_name(name)
    upload_dir = Path(workspace) / ".kinetic_uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    file_id = uuid.uuid4().hex
    target = upload_dir / f"{file_id}_{safe}"
    target.write_bytes(content)
    entry = FileEntry(
        id=file_id,
        name=safe,
        size=len(content),
        content_type=content_type or "application/octet-stream",
        path=str(target.relative_to(workspace)),
    )
    store.upsert(entry)
    return entry


def delete_upload_file(
    store: JsonStore[FileEntry],
    workspace: Path,
    file_id: str,
) -> bool:
    """Delete an uploaded file's blob + record. Missing blob is tolerated."""
    entry = store.get(file_id)
    if entry is None:
        return False
    target = Path(workspace) / entry.path
    # Confine to the upload dir (defense-in-depth against a bad stored path).
    upload_dir = (Path(workspace) / ".kinetic_uploads").resolve()
    try:
        resolved = target.resolve()
    except OSError:
        resolved = target
    if upload_dir not in resolved.parents and resolved != upload_dir:
        return False
    with contextlib.suppress(OSError):
        target.unlink()
    return store.delete(file_id)


__all__ = ["MAX_UPLOAD_BYTES", "delete_upload_file", "save_upload_file"]
