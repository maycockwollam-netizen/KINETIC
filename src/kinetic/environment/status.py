"""Workspace lifecycle status."""

from __future__ import annotations

from enum import StrEnum


class WorkspaceStatus(StrEnum):
    CREATED = "created"
    OPENED = "opened"
    DELETED = "deleted"
    ERROR = "error"
