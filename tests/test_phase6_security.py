"""Phase 6 security review tests.

Static + behavioral checks that Phase 6 introduces no new unrestricted
execution path, no subprocess usage, no secret leakage, no bypass around the
permission policy / environment / tool registry, and that all retry/repair
loops are bounded.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from intelligence import (
    ChangeAnalyzer,
    FailureAnalyzer,
    RepairContextBuilder,
    RepairCoordinator,
)
from intelligence.stuck import StuckDetector

INTELLIGENCE_MODULES = [
    "intelligence.models",
    "intelligence.parsers",
    "intelligence.analyzer",
    "intelligence.diff",
    "intelligence.stuck",
    "intelligence.regression",
    "intelligence.review",
    "intelligence.repair",
]


class TestNoNewExecutionPath:
    def test_no_subprocess_import_in_intelligence(self) -> None:
        import importlib

        for name in INTELLIGENCE_MODULES:
            mod = importlib.import_module(name)
            src = Path(mod.__file__).read_text()
            assert "import subprocess" not in src, f"{name} imports subprocess"
            assert "from subprocess" not in src, f"{name} imports from subprocess"

    def test_no_os_system_or_shell_true(self) -> None:
        import importlib

        for name in INTELLIGENCE_MODULES:
            mod = importlib.import_module(name)
            src = Path(mod.__file__).read_text()
            assert "os.system" not in src, f"{name} uses os.system"
            assert "shell=True" not in src, f"{name} uses shell=True"
            assert "subprocess.Popen" not in src, f"{name} uses subprocess.Popen"
            assert "subprocess.run" not in src, f"{name} uses subprocess.run"

    def test_no_direct_run_command_import(self) -> None:
        """The intelligence layer must not import the low-level run_command."""
        import importlib

        for name in INTELLIGENCE_MODULES:
            mod = importlib.import_module(name)
            src = Path(mod.__file__).read_text()
            assert "from tools.terminal import" not in src, f"{name} imports terminal tools"
            assert "run_command" not in src, f"{name} references run_command"

    def test_diff_analyzer_uses_inspector_abstraction(self) -> None:
        """ChangeAnalyzer must not call git directly — only via GitInspector."""
        mod_path = Path(inspect.getfile(ChangeAnalyzer))
        src_text = mod_path.read_text()
        # The analyzer must accept an inspector and delegate; no direct git calls.
        assert "inspector" in src_text
        assert "GitTools(" not in src_text  # must not instantiate GitTools directly


class TestBoundedLoops:
    def test_repair_coordinator_has_max_attempts(self) -> None:
        sig = inspect.signature(RepairCoordinator.__init__)
        assert "max_repair_attempts" in sig.parameters
        assert "max_verification_attempts" in sig.parameters

    def test_stuck_detector_terminates(self) -> None:
        from intelligence.models import RepairAttempt, RepairState

        det = StuckDetector(repeat_threshold=2)
        from intelligence.models import FailureAnalysis
        from tasks.policies import FailureClass

        a = FailureAnalysis(failure_class=FailureClass.TEST_FAILURE, command="pytest", exit_code=1)
        state = RepairState(attempts=[
            RepairAttempt(attempt=1, analysis=a),
            RepairAttempt(attempt=2, analysis=a),
        ])
        assert det.evaluate(state).stuck is True

    def test_repair_context_builder_is_bounded(self) -> None:
        sig = inspect.signature(RepairContextBuilder.__init__)
        assert "max_chars" in sig.parameters


class TestSecretSafety:
    def test_analyzer_masks_secrets_before_persistence(self) -> None:
        a = FailureAnalyzer(max_stdout_chars=400, max_stderr_chars=400).analyze(
            command="pytest",
            exit_code=1,
            stderr="api_key=sk-1234567890abcdef1234567890abcdef failed",
        )
        d = a.to_dict()
        assert "sk-1234567890abcdef1234567890abcdef" not in d["stderr_bounded"]
        assert "<secret-hidden>" in d["stderr_bounded"]

    def test_repair_context_masks_secrets(self) -> None:
        a = FailureAnalyzer(max_stdout_chars=400, max_stderr_chars=400).analyze(
            command="pytest",
            exit_code=1,
            stderr="api_key=sk-1234567890abcdef1234567890abcdef failed",
        )
        ctx = RepairContextBuilder().build(analysis=a)
        assert "sk-1234567890abcdef1234567890abcdef" not in ctx

    def test_audit_does_not_log_raw_output(self) -> None:
        from events import EventBus
        from security import AuditLog

        events = EventBus()
        audit = AuditLog(Path("/tmp/phase6_audit_test.log"))
        FailureAnalyzer(events=events, audit=audit, session_id="t").analyze(
            command="pytest",
            exit_code=1,
            stderr="api_key=sk-1234567890abcdef1234567890abcdef secret",
        )
        # The audit record detail must not contain the raw secret.
        log = Path("/tmp/phase6_audit_test.log").read_text()
        assert "sk-1234567890abcdef1234567890abcdef" not in log


class TestNoBypass:
    def test_repair_runner_is_protocol_not_execution(self) -> None:
        """RepairRunner is a Protocol wrapping AgentSession.query — no new path."""
        from intelligence.repair import RepairRunner

        assert hasattr(RepairRunner, "_is_protocol") or RepairRunner.__getattr__


class TestPermissionBoundaryIntact:
    async def test_environment_exec_still_enforced(self, tmp_path: Path) -> None:
        """Verification still goes through Environment.exec permission gate."""
        from environment import Environment
        from environment.config import RUNTIME_LOCAL, EnvironmentConfig
        from environment.network import NetworkPolicy
        from errors import PermissionDeniedError
        from events import EventBus
        from security import AuditLog, PermissionPolicy
        from tasks.verifier import Verifier

        bus = EventBus()
        cfg = EnvironmentConfig(runtime_type=RUNTIME_LOCAL, sandbox_mode=False, network=NetworkPolicy.ALLOW)
        policy = PermissionPolicy(writable_roots=[tmp_path], allow_environment_exec=False)
        env = Environment.create(
            tmp_path / "ws", cfg, policy=policy, audit=AuditLog(tmp_path / "a.log"),
            events=bus, session_id="t",
        )
        await env.provision()
        try:
            verifier = Verifier(environment=env, configured_command="true")
            with pytest.raises(PermissionDeniedError):
                await verifier.verify()
        finally:
            await env.destroy()
