"""Tests for the change/diff analyzer (Phase 6).

Pure analysis over text — no subprocess, no git. Verifies parsing, broad-change
detection, generated-file detection, outside-workspace detection, and secret
masking.
"""

from __future__ import annotations

from pathlib import Path

from intelligence.diff import ChangeAnalyzer, GitToolsInspector
from intelligence.models import ChangeAnalysis

STATUS = """\
## main
 M src/app.py
A  src/new.py
D  src/old.py
?? src/untracked.py
R  src/old2.py -> src/new2.py
"""

STATUS_BROAD = "\n".join(f" M file{i}.py" for i in range(60)) + "\n"


class FakeInspector:
    """A GitInspector that returns canned text (no subprocess)."""

    def __init__(self, status: str = "", diff: str = "") -> None:
        self._status = status
        self._diff = diff

    async def status_porcelain(self) -> str:
        return self._status

    async def diff_text(self, *, staged: bool = False) -> str:
        return self._diff


class TestParsing:
    async def test_parses_status(self, tmp_path: Path) -> None:
        a = ChangeAnalyzer(inspector=FakeInspector(STATUS), workspace=tmp_path)
        result = await a.analyze()
        assert isinstance(result, ChangeAnalysis)
        assert not result.empty
        paths = [c.path for c in result.changed]
        assert "src/app.py" in paths
        assert "src/new.py" in paths
        assert "src/old.py" in paths
        assert "src/new2.py" in paths  # rename -> new path

    async def test_added_deleted_modified(self, tmp_path: Path) -> None:
        a = ChangeAnalyzer(inspector=FakeInspector(STATUS), workspace=tmp_path)
        result = await a.analyze()
        assert "src/new.py" in result.added
        assert "src/old.py" in result.deleted
        assert "src/app.py" in result.modified

    async def test_empty_when_no_changes(self, tmp_path: Path) -> None:
        a = ChangeAnalyzer(inspector=FakeInspector("## main\n"), workspace=tmp_path)
        result = await a.analyze()
        assert result.empty

    def test_analyze_text_pure(self, tmp_path: Path) -> None:
        a = ChangeAnalyzer(workspace=tmp_path)
        result = a.analyze_text(STATUS, "diff content")
        assert not result.empty
        assert result.diff_bounded == "diff content"


class TestHeuristics:
    async def test_broad_change_detected(self, tmp_path: Path) -> None:
        a = ChangeAnalyzer(
            inspector=FakeInspector(STATUS_BROAD), workspace=tmp_path, broad_threshold=50,
        )
        result = await a.analyze()
        assert result.broad is True
        assert len(result.changed) == 60

    async def test_generated_files_detected(self, tmp_path: Path) -> None:
        status = " M src/app.py\n?? node_modules/pkg/index.js\n?? __pycache__/x.pyc\n"
        a = ChangeAnalyzer(inspector=FakeInspector(status), workspace=tmp_path)
        result = await a.analyze()
        assert "node_modules/pkg/index.js" in result.generated
        assert "__pycache__/x.pyc" in result.generated
        assert "src/app.py" not in result.generated

    async def test_outside_workspace_detected(self, tmp_path: Path) -> None:
        status = " M ../outside.py\n M /etc/passwd\n M src/app.py\n"
        a = ChangeAnalyzer(inspector=FakeInspector(status), workspace=tmp_path)
        result = await a.analyze()
        assert "../outside.py" in result.outside_workspace
        assert "/etc/passwd" in result.outside_workspace
        assert "src/app.py" not in result.outside_workspace

    async def test_diff_secret_masked(self, tmp_path: Path) -> None:
        diff = "api_key=sk-1234567890abcdef1234567890abcdef\n+new line"
        a = ChangeAnalyzer(inspector=FakeInspector("", diff), workspace=tmp_path)
        result = await a.analyze()
        assert "sk-1234567890abcdef1234567890abcdef" not in result.diff_bounded

    async def test_diff_truncated(self, tmp_path: Path) -> None:
        diff = "line\n" * 5000
        a = ChangeAnalyzer(
            inspector=FakeInspector("", diff), workspace=tmp_path, max_diff_chars=200,
        )
        result = await a.analyze()
        assert len(result.diff_bounded) <= 220
        assert "truncated" in result.diff_bounded


class TestGracefulDegrade:
    async def test_inspector_error_degrades_gracefully(self, tmp_path: Path) -> None:
        class Broken:
            async def status_porcelain(self) -> str:
                raise RuntimeError("git failed")

            async def diff_text(self, *, staged: bool = False) -> str:
                raise RuntimeError("git failed")

        a = ChangeAnalyzer(inspector=Broken(), workspace=tmp_path)
        result = await a.analyze()
        assert result.empty  # degraded to empty, did not crash


class TestGitToolsInspectorProtocol:
    def test_git_tools_inspector_satisfies_protocol(self, tmp_path: Path) -> None:
        # GitToolsInspector is structural; verify it has the expected methods.
        from security import AuditLog, PermissionPolicy
        from tools.git import GitTools

        git = GitTools(
            workspace=tmp_path, policy=PermissionPolicy(writable_roots=[tmp_path]),
            audit=AuditLog(tmp_path / "a.log"),
        )
        insp = GitToolsInspector(git)
        assert hasattr(insp, "status_porcelain")
        assert hasattr(insp, "diff_text")
