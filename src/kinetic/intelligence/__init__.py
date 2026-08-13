"""Phase 6 — Coding Intelligence, Verification & Recovery.

Makes KINETIC substantially better at completing real coding tasks *after* the
Phase 5 orchestration layer has planned and executed work. This package adds:

  * structured failure analysis (``analyzer``) + test-output parsers
    (``parsers``)
  * bounded change/diff analysis (``diff``) through the existing Git tools
  * a bounded repair coordinator (``repair``) that reuses the *same*
    ``AgentSession.query`` safe path — there is NO second agent loop, NO second
    ToolRegistry, NO second permission system
  * stuck detection (``stuck``), regression checking (``regression``) and a
    deterministic final review (``review``)

Security boundaries from Phase 1–5 are unchanged: every execution path still
goes through ``Environment.exec`` / the existing Git tools / the existing
permission policy. This package performs analysis (pure functions over bounded
text) and orchestration only — it never spawns subprocesses or mutates the
filesystem directly.
"""

from __future__ import annotations

from kinetic.intelligence.analyzer import FailureAnalysis, FailureAnalyzer
from kinetic.intelligence.diff import ChangeAnalysis, ChangeAnalyzer, ChangeRecord, GitInspector
from kinetic.intelligence.models import (
    RepairAttempt,
    RepairOutcome,
    RepairState,
    ReviewResult,
    StuckSignal,
    TestFailureInfo,
)
from kinetic.intelligence.parsers import analyze_test_output
from kinetic.intelligence.regression import RegressionResult
from kinetic.intelligence.repair import RepairContextBuilder, RepairCoordinator, RepairRunner
from kinetic.intelligence.review import FinalReviewer
from kinetic.intelligence.stuck import StuckDetector

__all__ = [
    "ChangeAnalysis",
    "ChangeAnalyzer",
    "ChangeRecord",
    "FailureAnalysis",
    "FailureAnalyzer",
    "FinalReviewer",
    "GitInspector",
    "RepairAttempt",
    "RepairContextBuilder",
    "RepairCoordinator",
    "RepairOutcome",
    "RepairRunner",
    "RepairState",
    "RegressionResult",
    "ReviewResult",
    "StuckDetector",
    "StuckSignal",
    "TestFailureInfo",
    "analyze_test_output",
]
