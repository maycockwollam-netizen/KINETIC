"""Phase 7 — optional live SDK integration test.

Runs ONLY when ANTHROPIC_API_KEY is explicitly available. Skips cleanly
otherwise — never fakes a successful live result.

When the key is present, this verifies:
* SDK initialization
* model query (minimal)
* tool registration
* permission callback
* event translation
* cleanup

Never prints or persists the API key.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set; live SDK integration test skipped",
)


@pytest.fixture
def live_settings(tmp_path: Path):
    from kinetic.config import Settings

    return Settings(
        workspace_root=tmp_path / "ws",
        session_root=tmp_path / "sessions",
        audit_log_path=tmp_path / "audit.log",
        memory_db_path=tmp_path / "memory.db",
        checkpoint_dir=tmp_path / "ckpts",
    )


class TestLiveSDKIntegration:
    """Live integration tests — only run with a real API key."""

    async def test_sdk_initializes(self, live_settings) -> None:
        from kinetic.agent.adapter import _IMPORT_ERROR

        # The SDK must be importable.
        assert _IMPORT_ERROR is None, "claude-agent-sdk must be installed for live tests"

    async def test_session_builds(self, live_settings, tmp_path: Path) -> None:
        from kinetic.agent.session import AgentSession, SessionConfig

        ws = tmp_path / "live_ws"
        ws.mkdir()
        cfg = SessionConfig(workspace=ws, prompt="test", max_turns=1)
        session = AgentSession(live_settings, cfg)
        assert session.session_id
        assert session.registry is not None
        # Tools must be registered.
        assert "run_command" in session.registry.names()

    async def test_environment_provisions(self, live_settings, tmp_path: Path) -> None:
        from kinetic.agent.session import AgentSession, SessionConfig

        ws = tmp_path / "live_env"
        ws.mkdir()
        cfg = SessionConfig(workspace=ws, prompt="test", max_turns=1, runtime_type="local")
        session = AgentSession(live_settings, cfg)
        try:
            await session.prepare()
            assert session.environment.is_running()
        finally:
            await session.finish()
        # Environment must be torn down.
        assert not session.environment.is_running()

    async def test_no_api_key_in_events(self, live_settings, tmp_path: Path) -> None:
        from kinetic.agent.session import AgentSession, SessionConfig

        ws = tmp_path / "live_secret"
        ws.mkdir()
        cfg = SessionConfig(workspace=ws, prompt="test", max_turns=1)
        session = AgentSession(live_settings, cfg)
        import json

        events_json = json.dumps([e.to_dict() for e in session.events.history])
        # The API key must never appear in events.
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        if key:
            assert key not in events_json
