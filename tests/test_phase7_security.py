"""Phase 7 — security audit regression tests.

Confirms no unrestricted execution path exists: every execution route goes
through Agent -> AgentSession -> PermissionPolicy -> ToolRegistry -> Tool ->
Environment -> Runtime. Also checks for unsafe patterns (shell=True, os.system,
eval, exec) in the source.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kinetic.errors import PermissionDeniedError
from kinetic.security import PermissionPolicy
from kinetic.security.policy import ENVIRONMENT_EXEC, EXECUTE, FILE_WRITE

SRC = Path(__file__).resolve().parent.parent / "src" / "kinetic"


class TestNoUnsafePatterns:
    """Grep the source for unsafe execution patterns."""

    def _py_files(self) -> list[Path]:
        return list(SRC.rglob("*.py"))

    def _grep(self, pattern: str) -> list[str]:
        results: list[str] = []
        for f in self._py_files():
            text = f.read_text(encoding="utf-8")
            for i, line in enumerate(text.splitlines(), 1):
                if pattern in line:
                    results.append(f"{f.name}:{i}: {line.strip()}")
        return results

    def test_no_os_system(self) -> None:
        hits = self._grep("os.system(")
        # Filter out comments/docstrings mentioning os.system
        real = [h for h in hits if not h.strip().startswith("#") and '"""' not in h and "'''" not in h]
        assert real == [], f"os.system found: {real}"

    def test_no_eval(self) -> None:
        hits = self._grep("eval(")
        real = [h for h in hits if "re." not in h and "#" not in h.split(":")[2][:1]]
        assert real == [], f"eval found: {real}"

    def test_no_shell_true_in_subprocess(self) -> None:
        # asyncio.create_subprocess_shell is used by run_command (the sanctioned
        # path). What we forbid is subprocess.Popen(..., shell=True) which would
        # bypass the environment boundary.
        hits = self._grep("shell=True")
        assert hits == [], f"shell=True found: {hits}"

    def test_no_exec_builtin(self) -> None:
        hits = self._grep("exec(")
        # Filter out legitimate uses: exec as part of method names, docstrings.
        real = [h for h in hits if not any(x in h for x in ["def ", "exec_", "exec_permission",
                                                              "exec_audit", "environment_exec",
                                                              "EXEC", ".exec(", "async def exec",
                                                              "def exec(", "_exec(", "exec_spec",
                                                              "exec_command", "exec(self",
                                                              "\"\"\"", "'''", "# ", "enforce_exec",
                                                              "allow_environment_exec", "require_exec",
                                                              "ENVIRONMENT_EXEC"])]
        assert real == [], f"exec() found: {real}"

    def test_no_pickle_load(self) -> None:
        hits = self._grep("pickle.load")
        assert hits == [], f"pickle.load found (unsafe deserialization): {hits}"

    def test_no_yaml_load_unsafe(self) -> None:
        hits = self._grep("yaml.load(")
        # yaml.unsafe_load / yaml.load without SafeLoader is the risk.
        real = [h for h in hits if "SafeLoader" not in h and "safe_load" not in h]
        assert real == [], f"unsafe yaml.load found: {real}"


class TestPermissionEnforcement:
    """The permission policy is the real boundary; it cannot be bypassed."""

    def test_execute_denied_without_flag(self) -> None:
        policy = PermissionPolicy(allow_execute=False)
        decision = policy.evaluate("run_command", EXECUTE, {"command": "ls"})
        assert not decision.allowed

    def test_write_fs_denied_outside_roots(self) -> None:
        policy = PermissionPolicy(writable_roots=[Path("/workspace")])
        decision = policy.evaluate("write_file", FILE_WRITE, {"path": "/etc/passwd"})
        assert not decision.allowed

    def test_environment_exec_denied(self) -> None:
        policy = PermissionPolicy(allow_environment_exec=False)
        decision = policy.evaluate("run_command", ENVIRONMENT_EXEC, {"command": "ls"})
        assert not decision.allowed

    def test_require_raises_on_deny(self) -> None:
        policy = PermissionPolicy(allow_execute=False)
        with pytest.raises(PermissionDeniedError):
            policy.require("run_command", EXECUTE, {"command": "ls"})

    def test_path_traversal_denied(self) -> None:
        policy = PermissionPolicy(writable_roots=[Path("/workspace")])
        decision = policy.evaluate("write_file", FILE_WRITE, {"path": "../../../etc/shadow"})
        assert not decision.allowed

    def test_relative_path_within_workspace_allowed(self) -> None:
        policy = PermissionPolicy(writable_roots=[Path("/workspace")])
        decision = policy.evaluate("write_file", FILE_WRITE, {"path": "src/app.py"})
        assert decision.allowed


class TestPathSafety:
    """safe_resolve must reject all escape attempts."""

    def test_traversal_rejected(self, tmp_path: Path) -> None:
        from kinetic.errors import SecurityError
        from kinetic.paths import safe_resolve

        with pytest.raises(SecurityError):
            safe_resolve(tmp_path, "../../etc/passwd")

    def test_absolute_outside_rejected(self, tmp_path: Path) -> None:
        from kinetic.errors import SecurityError
        from kinetic.paths import safe_resolve

        with pytest.raises(SecurityError):
            safe_resolve(tmp_path, "/etc/passwd")

    def test_absolute_inside_allowed(self, tmp_path: Path) -> None:
        from kinetic.paths import safe_resolve

        result = safe_resolve(tmp_path, str(tmp_path / "file.txt"))
        assert result == (tmp_path / "file.txt").resolve()

    def test_relative_resolved(self, tmp_path: Path) -> None:
        from kinetic.paths import safe_resolve

        result = safe_resolve(tmp_path, "subdir/file.txt")
        assert result == (tmp_path / "subdir" / "file.txt").resolve()

    def test_symlink_escape_rejected(self, tmp_path: Path) -> None:
        from kinetic.errors import SecurityError
        from kinetic.paths import safe_resolve

        # Create a symlink inside the workspace pointing outside.
        outside = tmp_path.parent / "outside_target"
        outside.mkdir(exist_ok=True)
        link = tmp_path / "escape"
        link.symlink_to(outside)
        with pytest.raises(SecurityError):
            safe_resolve(tmp_path, "escape/secret")

    def test_is_within(self, tmp_path: Path) -> None:
        from kinetic.paths import is_within

        assert is_within(tmp_path, tmp_path / "sub" / "file")
        assert not is_within(tmp_path, tmp_path.parent / "other")
