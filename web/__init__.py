"""KINETIC Phase 7.3 — Web Agent Test Console.

A thin HTTP/SSE adapter over the existing P1–P7.2 backend. This is a
test/control surface, NOT the final KINETIC product UI. It owns no execution
path: it routes requests to the existing TaskManager/Orchestrator/AgentSession
and streams events from the existing EventBus.
"""

from web.app import build_app, create_app
from web.console import WebConsole

__all__ = ["WebConsole", "build_app", "create_app"]
