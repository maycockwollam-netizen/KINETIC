"""Bounded change / diff analysis.

Inspects Git diff/status to characterize the working-tree changes produced by a
task. The analyzer itself performs NO subprocess/git calls and NO filesystem
mutation — it operates purely on text fetched through a :class:`GitInspector`
abstraction. The concrete :class:`GitToolsInspector` delegates to the existing
:class:`~kinetic.tools.git.GitTools`, which enforces the permission boundary
(``GIT_READ``), audit, and timeout/cancellation. This keeps the security
surface exactly where Phase 2 put it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from kinetic.events import EventBus, EventType
from kinetic.intelligence.models import ChangeAnalysis, ChangeRecord
from kinetic.memory.metadata import SecretDetector

if TYPE_CHECKING:
    from kinetic.tools.git import GitTools


# Heuristics for likely-generated files (no filesystem inspection — name based).
_GENERATED_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(^|/)\.venv/"),
    re.compile(r"(^|/)node_modules/"),
    re.compile(r"(^|/)__pycache__/"),
    re.compile(r"\.pyc$"),
    re.compile(r"(^|/)dist/"),
    re.compile(r"(^|/)build/"),
    re.compile(r"(^|/)target/"),
    re.compile(r"\.egg-info/"),
    re.compile(r"(^|/)\.next/"),
    re.compile(r"(^|/)\.pytest_cache/"),
]


@runtime_checkable
class GitInspector(Protocol):
    """Read-only Git inspection abstraction (no mutation, no subprocess here)."""

    async def status_porcelain(self) -> str:
        """Return ``git status --porcelain=v1 -b`` output."""
        ...

    async def diff_text(self, *, staged: bool = False) -> str:
        """Return ``git diff`` output (working tree or staged)."""
        ...


class GitToolsInspector:
    """A :class:`GitInspector` backed by the existing :class:`GitTools`.

    Delegates to ``GitTools.status`` / ``GitTools.diff`` — which run through the
    permission policy (``GIT_READ``), audit log, and timeout/cancellation. This
    class adds no new execution path.
    """

    def __init__(self, git: GitTools) -> None:
        self._git = git

    async def status_porcelain(self) -> str:
        result = await self._git.status({})
        return _extract_text(result)

    async def diff_text(self, *, staged: bool = False) -> str:
        result = await self._git.diff({"staged": staged})
        return _extract_text(result)


def _extract_text(result: dict[str, object]) -> str:
    """Extract the textual output from a GitTools tool_result dict."""
    content = result.get("content") if isinstance(result, dict) else None
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict) and isinstance(first.get("text"), str):
            return first["text"]
    if isinstance(result, dict):
        out = result.get("output")
        if isinstance(out, str):
            return out
    return ""


class ChangeAnalyzer:
    """Turns fetched git status/diff text into a bounded :class:`ChangeAnalysis`.

    Pure analysis over text — no subprocess, no filesystem writes. Output text
    is secret-masked before exposure.
    """

    def __init__(
        self,
        *,
        inspector: GitInspector | None = None,
        workspace: str | Path | None = None,
        broad_threshold: int = 50,
        max_changed: int = 200,
        max_diff_chars: int = 8000,
        secret_detector: SecretDetector | None = None,
        events: EventBus | None = None,
        session_id: str = "intelligence",
    ) -> None:
        self._inspector = inspector
        self._workspace = Path(str(workspace)).resolve() if workspace else None
        self._broad_threshold = broad_threshold
        self._max_changed = max_changed
        self._max_diff = max_diff_chars
        self._secrets = secret_detector or SecretDetector()
        self._events = events
        self._session_id = session_id

    async def analyze(self) -> ChangeAnalysis:
        """Fetch (if an inspector is configured) and analyze the working tree."""
        status_text = ""
        diff_text = ""
        if self._inspector is not None:
            try:
                status_text = await self._inspector.status_porcelain()
            except Exception:  # noqa: BLE001 - degrade gracefully, never crash the task
                status_text = ""
            try:
                diff_text = await self._inspector.diff_text(staged=False)
            except Exception:  # noqa: BLE001
                diff_text = ""
        return self.analyze_text(status_text, diff_text)

    def analyze_text(self, status_text: str, diff_text: str = "") -> ChangeAnalysis:
        """Pure analysis over already-fetched status/diff text."""
        records = self._parse_status(status_text)
        changed = records[: self._max_changed]
        added = [r.path for r in changed if r.is_added]
        deleted = [r.path for r in changed if r.is_deleted]
        modified = [r.path for r in changed if r.is_modified]
        generated = [r.path for r in changed if self._is_generated(r.path)]
        outside = [r.path for r in changed if self._is_outside_workspace(r.path)]
        broad = len(changed) >= self._broad_threshold
        empty = not changed
        diff_bounded = self._truncate(self._mask(diff_text), self._max_diff)
        analysis = ChangeAnalysis(
            changed=changed,
            added=added,
            deleted=deleted,
            modified=modified,
            outside_workspace=outside,
            generated=generated,
            broad=broad,
            empty=empty,
            diff_bounded=diff_bounded,
        )
        if self._events is not None:
            self._events.emit(
                EventType.FAILURE_ANALYZED,  # reuse analysis event for change analysis
                self._session_id,
                changed_files=len(changed),
                broad=broad,
                empty=empty,
            )
        return analysis

    # --- parsing -----------------------------------------------------------

    @staticmethod
    def _parse_status(text: str) -> list[ChangeRecord]:
        """Parse ``git status --porcelain=v1`` lines into records."""
        records: list[ChangeRecord] = []
        if not text:
            return records
        for line in text.splitlines():
            if not line or line.startswith("##"):  # branch header
                continue
            if len(line) < 4:
                continue
            xy = line[:2]
            path = line[3:].strip()
            # Rename: "R  old -> new" — take the new path.
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            path = path.strip('"')
            if not path:
                continue
            staged = xy[0] not in (" ", "?")
            records.append(ChangeRecord(path=path, status=xy, staged=staged))
        return records

    @staticmethod
    def _is_generated(path: str) -> bool:
        return any(p.search(path) for p in _GENERATED_PATTERNS)

    def _is_outside_workspace(self, path: str) -> bool:
        """True if a path escapes the workspace root (traversal or absolute)."""
        if not self._workspace:
            return False
        if path.startswith("/"):
            return True
        try:
            resolved = (self._workspace / path).resolve()
            resolved.relative_to(self._workspace)
        except (ValueError, OSError):
            return True
        return False

    # --- bounding + masking ------------------------------------------------

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        if limit and len(text) > limit:
            return text[:limit] + "\n…(truncated)"
        return text

    def _mask(self, text: str) -> str:
        if not text:
            return text
        masked = text
        for m in self._secrets.detect(text):
            target = m.original or m.snippet
            if target:
                masked = masked.replace(target, "<secret-hidden>")
        return masked
