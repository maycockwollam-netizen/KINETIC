"""Verification abstraction.

A small, pluggable verifier — not a framework. It checks whether the work of a
step (or the whole task) actually achieved its goal, producing a
:class:`~kinetic.tasks.policies.VerificationOutcome` (PASS / FAIL /
INCONCLUSIVE). It never pretends success: if no appropriate verification command
exists, it returns INCONCLUSIVE rather than guessing.

The verifier runs commands through the existing :class:`Environment.exec` path
(which enforces ENVIRONMENT_EXEC + audit + events), so verification never
introduces a backdoor around the security boundary. Project-specific test
commands come from the Phase 2 :class:`ProjectManifest`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from kinetic.errors import PermissionDeniedError, VerificationError
from kinetic.tasks.policies import VerificationOutcome

if TYPE_CHECKING:
    from kinetic.environment import Environment
    from kinetic.project.models import ProjectManifest


# Default verification commands per detected test/build system.
_DEFAULT_COMMANDS: dict[str, str] = {
    "pytest": "uv run pytest -q",
    "tox": "uv run tox -q",
    "jest": "npm test",
    "vitest": "npx vitest run",
    "mocha": "npx mocha",
    "phpunit": "vendor/bin/phpunit",
    "go": "go test ./...",
    "cargo": "cargo test",
    "make": "make test",
    "just": "just test",
}


def command_for_manifest(
    manifest: ProjectManifest | None,
    *,
    configured: str | None = None,
) -> str | None:
    """Resolve a verification command from project metadata or explicit config.

    An explicitly configured command always wins. Otherwise the first detected
    test/build system with a known default command is used. Returns ``None`` if
    nothing appropriate exists (caller then returns INCONCLUSIVE).
    """
    if configured:
        return configured
    if manifest is None:
        return None
    for system in manifest.test_systems:
        if system in _DEFAULT_COMMANDS:
            return _DEFAULT_COMMANDS[system]
    for system in manifest.build_systems:
        if system in _DEFAULT_COMMANDS:
            return _DEFAULT_COMMANDS[system]
    # Language-based fallbacks.
    if "python" in manifest.languages and any(m.path == "pyproject.toml" for m in manifest.manifests):
        return "uv run pytest -q"
    if "rust" in manifest.languages:
        return "cargo test"
    if "go" in manifest.languages:
        return "go test ./..."
    if "node" in manifest.languages:
        return "npm test"
    return None


@dataclass
class VerificationResult:
    outcome: VerificationOutcome
    command: str | None = None
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    reason: str = ""

    @property
    def passed(self) -> bool:
        return self.outcome is VerificationOutcome.PASS


class Verifier:
    """Runs project-appropriate verification commands via the Environment.

    Fail-closed: a verifier *internal* error raises :class:`VerificationError`;
    a failing test command returns ``FAIL``; no suitable command returns
    ``INCONCLUSIVE`` (never a silent PASS).
    """

    def __init__(
        self,
        *,
        environment: Environment | None = None,
        manifest: ProjectManifest | None = None,
        configured_command: str | None = None,
        timeout: float = 600.0,
    ) -> None:
        self._env = environment
        self._manifest = manifest
        self._configured = configured_command
        self._timeout = timeout

    @classmethod
    def from_settings(
        cls,
        s: object,
        *,
        environment: Environment | None = None,
        manifest: ProjectManifest | None = None,
    ) -> Verifier:
        return cls(
            environment=environment,
            manifest=manifest,
            configured_command=getattr(s, "verification_command", None),
            timeout=getattr(s, "execution_timeout", 600.0),
        )

    async def verify(self, *, command: str | None = None) -> VerificationResult:
        cmd = command or command_for_manifest(self._manifest, configured=self._configured)
        if not cmd:
            return VerificationResult(
                outcome=VerificationOutcome.INCONCLUSIVE,
                reason="no verification command available for this project",
            )
        if self._env is None:
            # No environment: cannot run; be honest rather than guess.
            return VerificationResult(
                outcome=VerificationOutcome.INCONCLUSIVE,
                command=cmd,
                reason="no execution environment available to run verification",
            )
        if not self._env.is_running():
            raise VerificationError("environment is not running; cannot verify")
        from kinetic.environment import ProcessSpec

        try:
            result = await self._env.exec(ProcessSpec(command=cmd, cwd=".", timeout=self._timeout))
        except PermissionDeniedError:
            # A permission denial is a security boundary event — never wrap it.
            raise
        except Exception as exc:  # noqa: BLE001 - verifier internal error
            raise VerificationError(f"verification execution failed: {exc}") from exc
        if result.succeeded:
            return VerificationResult(
                outcome=VerificationOutcome.PASS,
                command=cmd,
                exit_code=result.exit_code,
                stdout=result.stdout,
                stderr=result.stderr,
            )
        return VerificationResult(
            outcome=VerificationOutcome.FAIL,
            command=cmd,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            reason=f"verification command exited with code {result.exit_code}",
        )

    def classify(self, result: VerificationResult) -> dict[str, Any]:
        """A small, serializable summary of a verification result."""
        return {
            "outcome": result.outcome.value,
            "command": result.command,
            "exit_code": result.exit_code,
            "reason": result.reason,
        }
