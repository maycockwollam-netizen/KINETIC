"""Persistent JSON-backed stores for the web console.

The web layer (``web/*.py``) must not perform filesystem mutation directly
(enforced by the security tests). All persistence lives here, in this separate
package, behind plain methods the web console calls. Writes are atomic
(temp file + replace) and bounded; corrupt files fail closed rather than
silently wiping data.

Stores are JSON files under the KINETIC data directory. They hold small,
user-authored configuration (agent definitions, automations, uploaded file
metadata) — never secrets, never raw model output, never task state (which
lives in the TaskManager / checkpoint store).
"""

from __future__ import annotations

from .files import MAX_UPLOAD_BYTES, delete_upload_file, save_upload_file
from .json_store import JsonStore
from .models import AgentConfig, AutomationConfig, FileEntry

__all__ = [
    "AgentConfig",
    "AutomationConfig",
    "FileEntry",
    "JsonStore",
    "MAX_UPLOAD_BYTES",
    "delete_upload_file",
    "save_upload_file",
]
