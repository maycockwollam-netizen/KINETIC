"""The Starlette ASGI application for the KINETIC Web Agent Test Console.

This is a thin HTTP/SSE adapter. It holds a :class:`~web.console.WebConsole`
and routes requests to it. Every response is built through
:mod:`web.serialize`, which masks secrets and bounds payloads. The app owns no
task state of its own — it reads state from the TaskManager through the
console and streams events from each task's EventBus.

No subprocess, no filesystem mutation, no direct Environment access happens
here. Those all live behind the existing backend boundary.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from errors import KINETICError, TaskError, TaskStateError
from web.console import WebConsole
from web.serialize import scrub, serialize_outcome

if TYPE_CHECKING:
    from config import Settings

VERSION_ATTR = "__version__"
try:
    from __init__ import __version__ as _VERSION  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    _VERSION = "0.1.0"

_STATIC_DIR = Path(__file__).resolve().parent / "static"


def _safe_error(message: str, status: int, *, detail: Any = None) -> JSONResponse:
    """An error response that never includes secrets or stack traces."""
    body: dict[str, Any] = {"error": scrub(message)}
    if detail is not None:
        body["detail"] = scrub(detail)
    return JSONResponse(body, status_code=status)


async def _read_json(request: Request) -> Any:
    raw = await request.body()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"malformed JSON body: {exc}") from exc


# --- route handlers ---------------------------------------------------------


async def health(request: Request) -> JSONResponse:
    console: WebConsole = request.app.state.console
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    return JSONResponse(
        {
            "status": "ok",
            "version": _VERSION,
            "workspace": str(console.workspace),
            "backend_ready": not console.is_closed,
            "api_key_configured": has_key,
        }
    )


async def create_task(request: Request) -> Response:
    console: WebConsole = request.app.state.console
    try:
        payload = await _read_json(request)
    except ValueError as exc:
        return _safe_error(str(exc), 400)
    if not isinstance(payload, dict):
        return _safe_error("request body must be a JSON object", 400)
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return _safe_error("field 'prompt' is required and must be a non-empty string", 400)
    try:
        result = await console.create_task(prompt=prompt)
    except KINETICError as exc:
        return _safe_error(str(exc), 403 if "ANTHROPIC_API_KEY" in str(exc) else 500)
    except ValueError as exc:
        return _safe_error(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        return _safe_error(f"failed to create task: {type(exc).__name__}", 500)
    return JSONResponse(scrub(result), status_code=201)


async def list_tasks(request: Request) -> JSONResponse:
    console: WebConsole = request.app.state.console
    out = []
    for tid in console.tasks():
        try:
            out.append(console.task_snapshot(tid))
        except KeyError:
            continue
    return JSONResponse(scrub({"tasks": out}))


async def get_task(request: Request) -> Response:
    console: WebConsole = request.app.state.console
    task_id = request.path_params["task_id"]
    try:
        snapshot = console.task_snapshot(task_id)
    except KeyError:
        return _safe_error(f"unknown task: {task_id}", 404)
    return JSONResponse(snapshot)


async def start_task(request: Request) -> Response:
    # Tasks start automatically on creation (the existing backend semantics).
    # This endpoint exists for symmetry; it returns the current state.
    return await get_task(request)


async def resume_task(request: Request) -> Response:
    console: WebConsole = request.app.state.console
    task_id = request.path_params["task_id"]
    try:
        result = await console.resume(task_id)
    except KeyError:
        return _safe_error(f"unknown task: {task_id}", 404)
    except TaskError as exc:
        return _safe_error(str(exc), 409)
    except KINETICError as exc:
        return _safe_error(str(exc), 403 if "ANTHROPIC_API_KEY" in str(exc) else 500)
    except Exception as exc:  # noqa: BLE001
        return _safe_error(f"failed to resume task: {type(exc).__name__}", 500)
    return JSONResponse(scrub(result))


async def cancel_task(request: Request) -> Response:
    console: WebConsole = request.app.state.console
    task_id = request.path_params["task_id"]
    try:
        result = await console.cancel(task_id)
    except KeyError:
        return _safe_error(f"unknown task: {task_id}", 404)
    except TaskStateError as exc:
        return _safe_error(str(exc), 409)
    except KINETICError as exc:
        return _safe_error(str(exc), 500)
    except Exception as exc:  # noqa: BLE001
        return _safe_error(f"failed to cancel task: {type(exc).__name__}", 500)
    return JSONResponse(scrub(result))


async def task_events(request: Request) -> Response:
    """Server-Sent Events stream for one task.

    On connect we replay recent history (bounded, masked) then stream live
    events from the task's EventBus until the client disconnects or the task
    terminates. Reconnect is supported via the ``Last-Event-ID`` header (the
    numeric event id within the per-task log).
    """
    console: WebConsole = request.app.state.console
    task_id = request.path_params["task_id"]
    run = console.get_run(task_id)
    if run is None:
        return _safe_error(f"unknown task: {task_id}", 404)

    last_event_id = 0
    raw_last = request.headers.get("last-event-id")
    if raw_last:
        try:
            last_event_id = max(0, int(raw_last))
        except ValueError:
            last_event_id = 0

    poll_timeout = console.settings.web_event_poll_timeout

    async def event_stream():
        import asyncio

        # Replay history after the cursor.
        for evt in console.recent_events(task_id, after=last_event_id):
            yield _sse_frame(evt)
        cursor = last_event_id
        recent = console.recent_events(task_id, after=cursor)
        if recent:
            cursor = recent[-1]["id"]
        # Stream live until the task finishes and the log is fully drained.
        idle_ticks = 0
        max_idle = 200  # bounded: stop after ~poll*200 of no new events on a dead stream
        while True:
            recent = console.recent_events(task_id, after=cursor)
            if recent:
                idle_ticks = 0
                for evt in recent:
                    yield _sse_frame(evt)
                    cursor = evt["id"]
                continue
            # No new events.
            terminal = run.is_terminal
            if terminal:
                # Drain once more in case final events landed after termination.
                tail = console.recent_events(task_id, after=cursor)
                if tail:
                    for evt in tail:
                        yield _sse_frame(evt)
                        cursor = evt["id"]
                    continue
                yield _sse_frame({"type": "stream_end", "task_id": task_id, "terminal": True})
                return
            idle_ticks += 1
            if idle_ticks > max_idle:
                yield _sse_frame({"type": "stream_end", "task_id": task_id, "terminal": True})
                return
            await asyncio.sleep(poll_timeout)

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=headers)


def _sse_frame(payload: dict[str, Any]) -> str:
    """Format one SSE frame (masked JSON payload)."""
    safe = scrub(payload)
    event_type = str(safe.get("type", "message"))
    data = json.dumps(safe, default=str)
    return f"event: {event_type}\ndata: {data}\n\n"


async def task_outcome(request: Request) -> Response:
    """Return the bounded outcome of a finished task (or its current state)."""
    console: WebConsole = request.app.state.console
    task_id = request.path_params["task_id"]
    run = console.get_run(task_id)
    if run is None:
        return _safe_error(f"unknown task: {task_id}", 404)
    if run.outcome is not None:
        return JSONResponse(serialize_outcome(run.outcome))
    snapshot = console.task_snapshot(task_id)
    return JSONResponse(snapshot)


# --- security middleware ----------------------------------------------------


class _OriginGuardMiddleware:
    """Reject cross-site requests to the API surface (pure ASGI middleware).

    The test console binds to localhost by default and has no auth layer, so a
    browser-based CSRF attempt is the main remote risk. We require either no
    Origin header (same-origin/curl) or an Origin whose host matches the Host
    header. Implemented as a pure ASGI middleware (not BaseHTTPMiddleware) to
    avoid buffering the response stream — important for SSE.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
        origin = ""
        host = ""
        for name, value in headers:
            if name == b"origin":
                origin = value.decode("latin-1")
            elif name == b"host":
                host = value.decode("latin-1")
        if origin:
            try:
                parsed = urlparse(origin)
                origin_host = parsed.netloc
            except ValueError:
                origin_host = ""
            if origin_host and host and origin_host != host:
                await _send_json_error(send, "cross-origin request blocked", 403, scope)
                return
        await self._app(scope, receive, send)


async def _send_json_error(send: Send, message: str, status: int, scope: Scope) -> None:
    body = json.dumps({"error": scrub(message)}).encode("utf-8")
    response: Message = {
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
        ],
    }
    await send(response)
    await send({"type": "http.response.body", "body": body, "more_body": False})


def build_app(console: WebConsole) -> Starlette:
    """Assemble the Starlette app wired to a :class:`WebConsole`."""
    routes = [
        Route("/api/health", health, methods=["GET"]),
        Route("/api/tasks", create_task, methods=["POST"]),
        Route("/api/tasks", list_tasks, methods=["GET"]),
        Route("/api/tasks/{task_id}", get_task, methods=["GET"]),
        Route("/api/tasks/{task_id}/start", start_task, methods=["POST"]),
        Route("/api/tasks/{task_id}/resume", resume_task, methods=["POST"]),
        Route("/api/tasks/{task_id}/cancel", cancel_task, methods=["POST"]),
        Route("/api/tasks/{task_id}/events", task_events, methods=["GET"]),
        Route("/api/tasks/{task_id}/outcome", task_outcome, methods=["GET"]),
    ]
    if _STATIC_DIR.is_dir():
        routes.append(Mount("/static", app=StaticFiles(directory=str(_STATIC_DIR))))
        routes.append(Route("/", lambda req: FileResponse(str(_STATIC_DIR / "index.html"))))
    app = Starlette(routes=routes, middleware=[Middleware(_OriginGuardMiddleware)])
    app.state.console = console
    return app


def create_app(
    *,
    settings: Settings,
    workspace: Path | str,
    orchestrator_factory: Any = None,
    require_api_key: bool = True,
) -> Starlette:
    """Build the app + console from settings (used by the CLI + tests)."""
    from web.console import WebConsole, default_orchestrator_factory

    factory = orchestrator_factory or default_orchestrator_factory
    console = WebConsole(
        settings=settings,
        workspace=Path(workspace),
        orchestrator_factory=factory,
        require_api_key=require_api_key,
    )
    return build_app(console)


__all__ = ["build_app", "create_app"]
