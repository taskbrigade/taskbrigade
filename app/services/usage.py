"""Usage tracking and workflow-run budget enforcement (research/konzept-workflows.md §1.5/§7.4).

Usage reports are voluntary and unverifiable; the budget is a brake against endless repair loops, not billing.
`record` persists one `task_usage` row and counts it towards the task's workflow run; `enforce` then notifies at
80 % and pauses the run (`budget_exceeded`) at 100 %.
"""
from app.models import TaskUsage

TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


def record(session, actor, task, usage):
    """Persist a usage report and increment the task's run counters. Returns `(run, before)` – the locked run
    (or None) and its budget state *before* this report, so `enforce` can notify on the 80 % crossing only."""
    if not usage:
        return None, None
    input_tokens = usage.get("input_tokens") or 0
    output_tokens = usage.get("output_tokens") or 0
    cost_cents = usage.get("cost_cents")
    model = (usage.get("model") or "").strip()[:100] or None
    if not (input_tokens or output_tokens or cost_cents):
        return None, None
    run, before = None, None
    if task.workflow_run_step_id:
        from app.services.workflows import engine

        run = engine.lock_for_task(session, task)
        if run is not None:
            before = budget_state(run)
            run.used_tokens += input_tokens + output_tokens
            if cost_cents:
                run.used_cents += cost_cents
    session.add(TaskUsage(task_id=task.id, actor_id=actor.id, model=model, input_tokens=input_tokens,
                          output_tokens=output_tokens, cost_cents=cost_cents, source="agent"))
    session.flush()
    return run, before


def budget_state(run):
    """'ok' | 'warn' (>= 80 % of a set budget) | 'exceeded' (>= 100 %). A budget of 0 or NULL means no limit."""
    tokens = run.budget_tokens if run.budget_tokens and run.budget_tokens > 0 else None
    cents = run.budget_cents if run.budget_cents and run.budget_cents > 0 else None
    if (tokens is not None and run.used_tokens >= tokens) or (cents is not None and run.used_cents >= cents):
        return "exceeded"
    if (tokens is not None and run.used_tokens >= tokens * 0.8) or (cents is not None and run.used_cents >= cents * 0.8):
        return "warn"
    return "ok"


def enforce(session, run, before=None):
    """After a usage report: notify on the 80 % crossing, pause (`budget_exceeded`) at 100 %."""
    if run is None or run.status in TERMINAL or run.status == "paused":
        return None
    from app.services import notifications

    state = budget_state(run)
    if state == "exceeded":
        run.status, run.pause_reason = "paused", "budget_exceeded"
        notifications.workflow_event(session, run, "workflow_paused")
    elif state == "warn" and before == "ok":
        notifications.workflow_event(session, run, "workflow_budget_warning")
    return state
