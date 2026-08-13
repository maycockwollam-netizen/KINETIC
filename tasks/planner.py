"""Plan validation and dependency ordering.

Plans produced by the model (or constructed directly) are never executed
blindly: they are parsed, validated, and ordered. Validation is fail-safe —
malformed plans raise :class:`~errors.PlanError` and are never
executed.

Validation checks:
  * required fields present
  * unique step IDs (no duplicates)
  * step IDs referenced in ``depends_on`` exist
  * no circular dependencies (acyclic)
  * plan size bounded by configured limits
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from errors import PlanError
from tasks.models import Plan, PlanStep
from tasks.policies import validate_plan_bounds

if TYPE_CHECKING:
    pass


def validate_plan(plan: Plan, *, max_steps: int, max_deps: int) -> Plan:
    """Validate a plan in place and return it; raise :class:`PlanError` if invalid.

    Checks bounds, unique IDs, dependency existence, and acyclicity. Does not
    mutate step statuses.
    """
    if not plan.steps:
        raise PlanError("plan has no steps", reason="empty_plan")

    validate_plan_bounds(plan.steps, max_steps=max_steps, max_deps=max_deps)

    ids = plan.step_ids
    if len(ids) != len(set(ids)):
        dupes = {x for x in ids if ids.count(x) > 1}
        raise PlanError(
            f"duplicate step IDs: {sorted(dupes)}", reason="duplicate_step_ids"
        )

    for step in plan.steps:
        for dep in step.depends_on:
            if dep == step.step_id:
                raise PlanError(
                    f"step {step.step_id} depends on itself",
                    reason="self_dependency",
                )
            if dep not in ids:
                raise PlanError(
                    f"step {step.step_id} depends on unknown step {dep!r}",
                    reason="unknown_dependency",
                )

    _detect_cycle(plan)
    return plan


def _detect_cycle(plan: Plan) -> None:
    """Raise :class:`PlanError` if the dependency graph has a cycle (DFS)."""
    graph: dict[str, list[str]] = {s.step_id: list(s.depends_on) for s in plan.steps}
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {sid: WHITE for sid in graph}

    def visit(node: str, path: list[str]) -> None:
        color[node] = GRAY
        for nxt in graph.get(node, []):
            if color[nxt] == GRAY:
                cycle = " -> ".join(path + [node, nxt])
                raise PlanError(
                    f"dependency cycle detected: {cycle}",
                    reason="dependency_cycle",
                )
            if color[nxt] == WHITE:
                visit(nxt, path + [node])
        color[node] = BLACK

    for sid in graph:
        if color[sid] == WHITE:
            visit(sid, [])


def topological_order(plan: Plan) -> list[PlanStep]:
    """Return steps in a deterministic topological (dependency) order.

    Ties are broken by original plan order so the result is stable and
    reproducible. Raises :class:`PlanError` if a cycle exists (defensive;
    :func:`validate_plan` should already have caught it).
    """
    _detect_cycle(plan)
    steps_by_id = {s.step_id: s for s in plan.steps}
    original_index = {s.step_id: i for i, s in enumerate(plan.steps)}
    visited: set[str] = set()
    order: list[PlanStep] = []

    def visit(node: str) -> None:
        if node in visited:
            return
        for dep in steps_by_id[node].depends_on:
            visit(dep)
        visited.add(node)
        order.append(steps_by_id[node])

    for s in sorted(plan.steps, key=lambda x: original_index[x.step_id]):
        visit(s.step_id)
    return order


def next_executable_step(plan: Plan) -> PlanStep | None:
    """Return the next step whose dependencies are all completed.

    Considers only PENDING steps; a step is ready when every dependency is
    COMPLETED. Returns ``None`` when no step is currently executable (either
    all done, or remaining steps are blocked).
    """
    from tasks.models import StepStatus

    completed = set(plan.completed_step_ids())
    for step in plan.steps:
        if step.status is not StepStatus.PENDING:
            continue
        if all(dep in completed for dep in step.depends_on):
            return step
    return None


def is_blocked(plan: Plan) -> bool:
    """True if remaining PENDING steps all have unmet dependencies.

    Combined with ``next_executable_step`` returning ``None`` and ``all_done``
    being false, this indicates a stuck plan (should not happen after
    validation, but is checked defensively).
    """
    from tasks.models import StepStatus

    completed = set(plan.completed_step_ids())
    pending = [s for s in plan.steps if s.status is StepStatus.PENDING]
    if not pending:
        return False
    return all(
        any(dep not in completed for dep in s.depends_on) for s in pending
    )


def reset_failed_steps(plan: Plan) -> None:
    """Reset FAILED steps back to PENDING so they can be retried/re-planned."""
    from tasks.models import StepStatus

    for s in plan.steps:
        if s.status is StepStatus.FAILED:
            s.status = StepStatus.PENDING


def invalidate_remaining_steps(plan: Plan, *, from_step_id: str) -> None:
    """Reset ``from_step_id`` and all its dependents back to PENDING.

    Used by re-planning: the failed step and its obsolete downstream steps are
    invalidated so a fresh plan can drive execution. Other already-completed
    steps are preserved.
    """
    from tasks.models import StepStatus

    # BFS over dependents of from_step_id, including from_step_id itself.
    graph: dict[str, list[str]] = {s.step_id: [] for s in plan.steps}
    for s in plan.steps:
        for dep in s.depends_on:
            graph.setdefault(dep, []).append(s.step_id)
    affected: set[str] = set()
    frontier = [from_step_id]
    while frontier:
        node = frontier.pop()
        for child in graph.get(node, []):
            if child not in affected:
                affected.add(child)
                frontier.append(child)
    # Reset the failed step and ALL its dependents (including completed ones),
    # since a reset upstream makes downstream results obsolete. Completed
    # steps that are NOT dependents are preserved.
    reset = affected | {from_step_id}
    for s in plan.steps:
        if s.step_id in reset:
            s.status = StepStatus.PENDING


# --- model-output plan parsing ---------------------------------------------

def parse_model_plan(
    raw: dict,
    *,
    task_id: str,
    max_steps: int,
    max_deps: int,
) -> Plan:
    """Parse + validate a model-generated plan structure into a :class:`Plan`.

    Never trusts arbitrary model output: required fields are checked, unknown
    fields ignored, and the result is fully validated before return. A
    malformed plan raises :class:`PlanError` (fail-safe — never executed).
    """
    if not isinstance(raw, dict):
        raise PlanError("model plan is not a dict", reason="malformed_plan")
    plan_id = raw.get("plan_id") or f"plan-{task_id[:8]}"
    if not isinstance(plan_id, str) or not plan_id.strip():
        raise PlanError("plan_id missing or invalid", reason="malformed_plan")

    raw_steps = raw.get("steps")
    if not isinstance(raw_steps, list):
        raise PlanError("plan 'steps' must be a list", reason="malformed_plan")

    steps: list[PlanStep] = []
    for i, rs in enumerate(raw_steps):
        if not isinstance(rs, dict):
            raise PlanError(f"step {i} is not a dict", reason="malformed_plan")
        sid = rs.get("step_id") or rs.get("id") or f"step-{i + 1}"
        if not isinstance(sid, str) or not sid.strip():
            raise PlanError(f"step {i} has no valid step_id", reason="malformed_plan")
        deps = rs.get("depends_on") or rs.get("dependencies") or []
        if not isinstance(deps, list):
            raise PlanError(f"step {sid} depends_on must be a list", reason="malformed_plan")
        steps.append(
            PlanStep(
                step_id=str(sid),
                description=str(rs.get("description", "")),
                depends_on=[str(d) for d in deps],
                expected_outcome=str(rs.get("expected_outcome", "")),
                verification=str(rs.get("verification", "")),
            )
        )

    plan = Plan(
        plan_id=str(plan_id),
        task_id=task_id,
        goal=str(raw.get("goal", "")),
        steps=steps,
        verification_criteria=str(raw.get("verification_criteria", "")),
        estimated_risk=str(raw.get("estimated_risk", "unknown")),
    )
    return validate_plan(plan, max_steps=max_steps, max_deps=max_deps)

