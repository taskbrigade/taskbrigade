"""Working-time clocks and failure recovery, under the engine's run lock.

No commits here: a retry/timeout/reminder and its notification commit with the enclosing task action or tick.
Runner activation belongs to WF 10; retry_task deliberately uses the normal engine activation for new jobs.
"""
import json
from datetime import datetime, timedelta

from sqlalchemy import select

from app.i18n import _
from app.models import WorkflowRunStep
from app.services import notifications, settings_store, state_machine as sm
from app.services.workflows.roles import resolve


def working_us(row, now):
    elapsed = max(timedelta(), now - row.work_started_at) if row.work_started_at else timedelta()
    return (row.work_us or 0) + elapsed // timedelta(microseconds=1)


def stop_clock(row, now):
    row.work_us = working_us(row, now)
    row.work_started_at, row.deadline_at = None, None


def sync_clock(row, now):
    """Called after every task state change; only in_progress consumes the timeout."""
    task = row.task
    if task is None:
        return
    if row.work_started_at is not None and task.status != "in_progress":
        row.work_us = working_us(row, now)
        row.work_started_at = None
    if task.status == "in_progress" and row.work_started_at is None:
        row.work_started_at = now
    default_minutes = {"agent": 24 * 60, "script": 48 * 60}.get(row.step.step_type)
    minutes = (row.step.timeout_minutes or default_minutes) if default_minutes else None
    row.deadline_at = None
    if minutes and task.status == "in_progress":
        remaining = timedelta(microseconds=minutes * 60 * 1_000_000 - working_us(row, now))
        row.deadline_at = now + min(remaining, datetime.max - now)
    waiting = task.status == "questions" or (task.status == "for_review" and (
        row.step.step_type in ("approval", "gate") or row.step.review_mode == "manual"))
    if waiting and row.wait_started_at is None:
        row.wait_started_at, row.last_reminder_at = now, None
    elif not waiting:
        row.wait_started_at, row.last_reminder_at = None, None
    if task.status == "defined" and row.idle_since is None:
        row.idle_since, row.idle_notified_at = now, None
    elif task.status != "defined":
        row.idle_since, row.idle_notified_at = None, None


def fail(session, run, row, code, now=None):
    """Only transient execution failures retry; configuration, rejection and output checks fail closed."""
    now = now or sm.utcnow()
    stop_clock(row, now)
    row.status, row.error_code, row.finished_at = "failed", code, now
    row.next_retry_at = None
    execution_failure = code in ("timeout", "task_aborted")
    retryable = execution_failure and (row.step.step_type == "runner" or (
        row.step.step_type in ("agent", "script") and row.step.agent_role))
    if run.status != "paused" and retryable and row.attempt <= row.step.max_retries and row.attempt < 255:
        # Bound datetime arithmetic even for deliberately very large configured backoffs/attempt counts.
        seconds = min(row.step.retry_backoff_s * 2 ** (row.attempt - 1),
                      (datetime.max - now) // timedelta(seconds=1))
        row.next_retry_at = now + timedelta(seconds=seconds)
        run.status = "running"
        return
    was_paused = run.status == "paused"
    run.status = "paused"
    if not was_paused:
        run.pause_reason = code
        from app.services.workflows.events import emit_run_end
        emit_run_end(session, run, outcome="failed")
    notifications.workflow_event(session, run, "workflow_paused")


def _used_assignees(session, row):
    return set(session.scalars(select(WorkflowRunStep.assignee_id).where(
        WorkflowRunStep.run_id == row.run_id, WorkflowRunStep.step_id == row.step_id,
        WorkflowRunStep.assignee_id.is_not(None))))


def retry_task(session, run, row, now):
    from app.services.workflows import engine

    if not row.next_retry_at or row.next_retry_at > now:
        return
    row.next_retry_at = None
    new = engine._attempt(session, run, row.step)
    if row.step.step_type == "runner":
        # A new task/job/result is essential; never reopen a runner task with existing results.
        engine.advance(session, run)
        return
    excluded = _used_assignees(session, row) | {row.task.reviewer_id if row.task else None}
    candidate = resolve(session, run, row.step, exclude=excluded, task=row.task)
    if candidate is None:
        engine._fail(session, run, new, "no_assignee")
        return
    task = row.task
    if task is None:
        engine._fail(session, run, new, "no_assignee")
        return
    new.task, new.assignee_id, new.activated_at, new.status = task, candidate.id, now, "active"
    metadata = json.loads(row.output_json or "{}")
    new.output_json = json.dumps({key: metadata[key] for key in (
        "input_profiles", "input_results", "repair_pin_id", "repair_reason", "failed_checks", "manual_repair", "schema_diff")
        if key in metadata})
    # Flush the step -> task link before the reverse link to avoid an ORM dependency cycle.
    session.flush()
    task.workflow_run_step = new
    task.workflow_run_step_id = new.id
    task.assigned_to = candidate.id
    from app.services.tasks import _apply_model

    _apply_model(session, run.template.owner, task, {}, reassigned=True)
    task.row_version += 1
    sm.reset_for_reassignment(session, task, run.template.owner, candidate.name)
    sm.add_comment(session, task, run.template.owner, "system", _("Workflow step retried with another agent"))
    sync_clock(new, now)
    session.flush()


def _idle_reassign(session, run, row, now):
    from app.services.workflows import engine

    if not row.step.agent_role or engine._config(row.step).get("reassign_on_idle") is not True:
        return
    candidate = resolve(session, run, row.step, exclude=(row.task.assigned_to, row.task.reviewer_id), task=row.task)
    if candidate is None:
        return
    row.task.assigned_to = row.assignee_id = candidate.id
    from app.services.tasks import _apply_model

    _apply_model(session, run.template.owner, row.task, {}, reassigned=True)
    row.task.row_version += 1
    sm.reset_for_reassignment(session, row.task, run.template.owner, candidate.name)
    sm.add_comment(session, row.task, run.template.owner, "system", _("Unclaimed workflow task reassigned"))
    # One optional reassignment per unclaimed interval, not an endless rank 1/rank 2 loop.
    row.idle_notified_at = now


def progress(session, run, now):
    """Idempotent tick work for a running/waiting run. Manual pauses defer retries and reminders."""
    from app.services.workflows import engine

    for row in engine._latest(session, run).values():
        if row.status == "failed":
            if run.status != "paused":
                retry_task(session, run, row, now)
            continue
        if row.step.step_type == "gate" and row.status == "waiting" and run.status != "paused":
            hours = row.step.wait_reminder_hours or 24
            if (row.wait_started_at and now >= row.wait_started_at + timedelta(hours=hours)
                    and (row.last_reminder_at is None or now >= row.last_reminder_at + timedelta(days=1))):
                previous = engine._latest(session, run).get(row.position - 1)
                if previous and previous.task:
                    notifications.workflow_event(session, run, "workflow_wait_reminder",
                        _("Workflow task %(task)s is waiting for a decision", task=previous.task.task_nr))
                    row.last_reminder_at = now
        if row.status not in ("active", "waiting") or row.task is None:
            continue
        sync_clock(row, now)
        if row.deadline_at and row.deadline_at <= now and row.task.status == "in_progress":
            engine._internal_status(session, row.task, "defined", _("Workflow working-time timeout; lease released"))
            fail(session, run, row, "timeout", now)
            continue
        if run.status == "paused":
            continue
        if row.wait_started_at:
            hours = row.step.wait_reminder_hours or 24
            due = row.wait_started_at + timedelta(hours=hours)
            if now >= due and (row.last_reminder_at is None or now >= row.last_reminder_at + timedelta(days=1)):
                notifications.workflow_event(session, run, "workflow_wait_reminder",
                    _("Workflow task %(task)s is waiting for a decision", task=row.task.task_nr))
                row.last_reminder_at = now
        hours = int(settings_store.get(session, "claim_reminder_hours") or 4)
        if row.idle_since and now >= row.idle_since + timedelta(hours=hours) and row.idle_notified_at is None:
            notifications.workflow_event(session, run, "workflow_claim_reminder",
                _("No agent has claimed workflow task %(task)s", task=row.task.task_nr), to=["owner"])
            row.idle_notified_at = now
            _idle_reassign(session, run, row, now)
