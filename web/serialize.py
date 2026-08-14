"""Safe serialization for the web layer.

The web layer never sends raw model output, raw tool output, environment
variables, or secrets to the browser. This module turns KINETIC domain objects
into bounded, JSON-serializable dicts and masks credential-like content in any
free-form string. It is the single chokepoint where every response/event is
scrubbed before crossing the application boundary.

This module performs NO subprocess, NO filesystem mutation, and NO model calls.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from events.types import Event
from memory.metadata import DEFAULT_SECRET_DETECTOR

if TYPE_CHECKING:
    from tasks.executor import ExecutionOutcome
    from tasks.models import Plan, Task
    from tasks.observer import Observation
    from tasks.verifier import VerificationResult

#: Hard cap on any free-form string shipped to the browser.
MAX_STRING_CHARS = 8000

#: Keys whose values are system-generated identifiers (UUIDs, slugs), not
#: user/tool/model content. These are never masked — masking our own task IDs
#: would break the API contract while providing no security benefit (a UUID is
#: not a credential). Secret-shaped *content* in other fields is still masked.
_IDENTIFIER_KEYS = frozenset({
    "id", "task_id", "session_id", "plan_id", "step_id", "workspace",
    "project_id", "parent_id", "name",
})


def mask(text: Any, *, _id_keys: frozenset[str] = _IDENTIFIER_KEYS) -> Any:
    """Recursively redact credential-like values from a value tree.

    Identifier keys (task_id, session_id, ...) are passed through unmasked:
    they are system-generated UUIDs the browser must use, not credentials.
    """
    if isinstance(text, dict):
        out: dict[str, Any] = {}
        for k, v in text.items():
            if k in _id_keys:
                out[k] = v
            else:
                out[k] = mask(v, _id_keys=_id_keys)
        return out
    if isinstance(text, str):
        redacted = text
        for m in DEFAULT_SECRET_DETECTOR.detect(text):
            target = m.original or m.snippet
            if target:
                redacted = redacted.replace(target, "<redacted>")
        return redacted
    if isinstance(text, (list, tuple)):
        return [mask(v, _id_keys=_id_keys) for v in text]
    return text


def truncate(text: Any, *, _id_keys: frozenset[str] = _IDENTIFIER_KEYS) -> Any:
    """Bound free-form string length so a runaway field cannot blow up the browser."""
    if isinstance(text, str) and len(text) > MAX_STRING_CHARS:
        return text[:MAX_STRING_CHARS] + "\n…(truncated)"
    if isinstance(text, dict):
        return {k: (v if k in _id_keys else truncate(v, _id_keys=_id_keys)) for k, v in text.items()}
    if isinstance(text, (list, tuple)):
        return [truncate(v, _id_keys=_id_keys) for v in text]
    return text


def scrub(value: Any) -> Any:
    """Mask + truncate, then verify JSON-serializable (fallback to str)."""
    out = truncate(mask(value))
    if isinstance(out, (dict, list)):
        return _ensure_json(out)
    return _ensure_json({"_": out})["_"]


def _ensure_json(value: Any) -> Any:
    """Replace any non-JSON-serializable value with its string form."""
    import json

    if isinstance(value, dict):
        return {k: _ensure_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_ensure_json(v) for v in value]
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


def serialize_event(event: Event) -> dict[str, Any]:
    """A bounded, secret-masked dict for one event (safe for SSE)."""
    return scrub(event.to_dict())


def serialize_task(task: Task) -> dict[str, Any]:
    """A bounded public snapshot of a task (no private reasoning)."""
    summary = scrub(task.summary())
    summary["user_request"] = scrub(task.user_request)
    summary["workspace"] = scrub(str(task.workspace))
    return summary


def serialize_plan(plan: Plan | None) -> dict[str, Any] | None:
    if plan is None:
        return None
    return scrub(plan.model_dump())


def serialize_observation(obs: Observation) -> dict[str, Any]:
    return scrub(obs.to_dict())


def serialize_verification(result: VerificationResult | None) -> dict[str, Any] | None:
    if result is None:
        return None
    return scrub(
        {
            "outcome": result.outcome.value,
            "command": result.command,
            "exit_code": result.exit_code,
            "reason": result.reason,
            "passed": result.passed,
        }
    )


def serialize_outcome(outcome: ExecutionOutcome) -> dict[str, Any]:
    """A bounded summary of a finished task run (no raw model output)."""
    return scrub(
        {
            "task": serialize_task(outcome.task),
            "plan": serialize_plan(outcome.plan),
            "observations": [serialize_observation(o) for o in outcome.observations],
            "final_verification": serialize_verification(outcome.final_verification),
            "failure": outcome.failure.model_dump() if outcome.failure else None,
            "cancelled": outcome.cancelled,
            "repair": _serialize_repair(outcome.repair),
            "review": _serialize_review(outcome.review),
        }
    )


def _serialize_repair(repair: Any) -> dict[str, Any] | None:
    if repair is None:
        return None
    return scrub(repair.model_dump() if hasattr(repair, "model_dump") else dict(repair))


def _serialize_review(review: Any) -> dict[str, Any] | None:
    if review is None:
        return None
    return scrub(review.model_dump() if hasattr(review, "model_dump") else dict(review))


__all__ = [
    "MAX_STRING_CHARS",
    "mask",
    "truncate",
    "scrub",
    "serialize_event",
    "serialize_task",
    "serialize_plan",
    "serialize_observation",
    "serialize_verification",
    "serialize_outcome",
]
