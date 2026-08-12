"""Workspace management.

A ``Workspace`` owns one directory tree the agent operates in. It enforces
path boundaries (traversal + symlink escape), tracks lifecycle, and integrates
with the permission policy. It does NOT assume Docker — that is a Phase 3
concern implemented behind the future ``Environment``/sandbox interface.

Lifecycle::

    ws = Workspace.create(parent, name)   # create a fresh workspace
    ws.validate_path("src/app.py")         # safe, workspace-relative
    ws.cleanup()                           # remove the tree
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from kinetic.environment.metadata import WorkspaceMeta
from kinetic.environment.status import WorkspaceStatus
from kinetic.errors import WorkspaceError
from kinetic.events import EventBus, EventType
from kinetic.paths import safe_resolve


class Workspace:
    """A managed workspace directory with enforced path boundaries."""

    def __init__(
        self,
        root: Path,
        *,
        parent: Path | None = None,
        status: WorkspaceStatus = WorkspaceStatus.OPENED,
        readonly: bool = False,
        events: EventBus | None = None,
        session_id: str = "workspace",
    ) -> None:
        self._root = root.resolve()
        self._parent = parent.resolve() if parent else None
        self._status = status
        self._readonly = readonly
        self._events = events
        self._session_id = session_id

    # --- construction -------------------------------------------------------

    @classmethod
    def create(
        cls,
        parent: Path,
        name: str | None = None,
        *,
        exist_ok: bool = False,
        events: EventBus | None = None,
        session_id: str = "workspace",
    ) -> Workspace:
        parent = parent.resolve()
        parent.mkdir(parents=True, exist_ok=True)
        name = name or f"ws-{uuid.uuid4().hex[:12]}"
        root = parent / name
        if root.exists() and not exist_ok:
            raise WorkspaceError(str(root), "workspace already exists")
        root.mkdir(parents=True, exist_ok=exist_ok)
        ws = cls(root, parent=parent, status=WorkspaceStatus.CREATED, events=events, session_id=session_id)
        ws._emit(EventType.WORKSPACE_CREATED, root=str(root))
        return ws

    @classmethod
    def open(
        cls,
        root: Path,
        *,
        readonly: bool = False,
        events: EventBus | None = None,
        session_id: str = "workspace",
    ) -> Workspace:
        root = root.resolve()
        if not root.exists():
            raise WorkspaceError(str(root), "workspace does not exist")
        if not root.is_dir():
            raise WorkspaceError(str(root), "workspace root is not a directory")
        return cls(
            root,
            status=WorkspaceStatus.OPENED,
            readonly=readonly,
            events=events,
            session_id=session_id,
        )

    # --- path safety --------------------------------------------------------

    @property
    def root(self) -> Path:
        return self._root

    @property
    def status(self) -> WorkspaceStatus:
        return self._status

    @property
    def readonly(self) -> bool:
        return self._readonly

    def validate_path(self, path: str | Path) -> Path:
        """Resolve ``path`` against the workspace; reject escapes.

        Symlinks inside the workspace that point outside are rejected by
        ``safe_resolve`` because the resolved target falls outside ``root``.
        """
        return safe_resolve(self._root, path)

    def assert_writable(self, path: Path | None = None) -> None:
        if self._readonly:
            raise WorkspaceError(str(self._root), "workspace is read-only")
        if path is not None:
            self.validate_path(path)

    # --- metadata -----------------------------------------------------------

    def metadata(self) -> WorkspaceMeta:
        return WorkspaceMeta(
            root=self._root,
            status=str(self._status),
            parent=self._parent,
            readonly=self._readonly,
        )

    # --- lifecycle ----------------------------------------------------------

    def cleanup(self) -> None:
        if self._status == WorkspaceStatus.DELETED:
            return
        if self._parent is None:
            # Never delete a workspace we didn't create (no parent recorded).
            raise WorkspaceError(str(self._root), "cannot cleanup a workspace with no recorded parent")
        try:
            shutil.rmtree(self._root)
        except OSError as exc:
            self._status = WorkspaceStatus.ERROR
            raise WorkspaceError(str(self._root), f"cleanup failed: {exc}") from exc
        self._status = WorkspaceStatus.DELETED
        self._emit(EventType.WORKSPACE_DELETED, root=str(self._root))

    def _emit(self, event_type: EventType, **data: object) -> None:
        if self._events is not None:
            self._events.emit(event_type, self._session_id, **data)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"Workspace(root={self._root!s}, status={self._status.value})"
