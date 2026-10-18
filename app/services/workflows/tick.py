"""DB-locked scheduler, independent of the process triggering it.

The caller supplies a clean session; tick owns commits, one per slot/run. The named lock is held
on a dedicated connection across these commits. Unique slot keys provide a second idempotency barrier.
"""
import json
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text

from app.models import AppSetting, Task, WorkflowRun, WorkflowSchedule, WorkflowScheduleFire, WorkflowTemplate
from app.services import notifications, settings_store
from app.services.workflows import engine
from app.services.workflows.schedule import next_fire, utc

log = logging.getLogger(__name__)
LOCK = "tbr_workflow_tick"


def enabled(session):
    return settings_store.get_bool(session, "workflows_enabled")


def _notice(session, run, event):
    notifications.workflow_event(session, run, event, to=["owner"])


def _fire(session, schedule, slot, now, misfire):
    existing = session.scalar(select(WorkflowScheduleFire).where(
        WorkflowScheduleFire.schedule_id == schedule.id, WorkflowScheduleFire.scheduled_for == slot))
    if existing:
        schedule.last_fire_at, schedule.last_outcome = slot, existing.outcome
        schedule.next_fire_at = next_fire(schedule, slot)
        return existing.outcome
    row = WorkflowScheduleFire(schedule_id=schedule.id, scheduled_for=slot, fired_at=now, outcome="failed")
    session.add(row)
    if misfire:
        row.outcome = "skipped_misfire"
    else:
        try:
            # Savepoint removes a partially created run/tasks if activation raises; audit still commits.
            with session.begin_nested():
                run = engine.start(session, schedule.template.owner, schedule.template,
                    params=json.loads(schedule.params_json or "{}"), trigger_kind="schedule", schedule=schedule,
                    scheduled_for=slot, now=now, queue=schedule.overlap_policy == "queue")
                row.run_id, row.outcome = run.id, "queued" if run.status == "queued" else "started"
        except Exception as exc:  # Keep error details/data out of schedule logs and notices.
            if getattr(exc, "code", None) == "workflow_overlap":
                row.outcome = "skipped_overlap"
                previous_notice = session.scalar(select(WorkflowScheduleFire.id).join(
                    WorkflowSchedule, WorkflowSchedule.id == WorkflowScheduleFire.schedule_id).where(
                    WorkflowSchedule.template_id == schedule.template_id,
                    WorkflowScheduleFire.outcome == "skipped_overlap", WorkflowScheduleFire.fired_at >=
                    now.replace(hour=0, minute=0, second=0, microsecond=0), WorkflowScheduleFire.id != row.id).limit(1))
                if previous_notice is None:
                    active = session.scalar(select(WorkflowRun).where(WorkflowRun.template_id == schedule.template_id,
                        WorkflowRun.status.not_in(engine.TERMINAL)).order_by(WorkflowRun.id).limit(1))
                    if active:
                        _notice(session, active, "workflow_schedule_overlap")
            else:
                row.outcome, row.note = "failed", type(exc).__name__
                log.error("workflow_schedule_failed", extra={"schedule_id": schedule.id, "error_type": type(exc).__name__})
                notifications.workflow_schedule_event(session, schedule, "workflow_schedule_failed")
    schedule.last_fire_at, schedule.last_outcome = slot, row.outcome
    schedule.next_fire_at = next_fire(schedule, slot)
    session.flush()
    return row.outcome


def _progress(session, run, now):
    if run.status == "queued":
        others = list(session.scalars(select(WorkflowRun).where(WorkflowRun.template_id == run.template_id,
            WorkflowRun.id != run.id, WorkflowRun.status.not_in(engine.TERMINAL)).order_by(WorkflowRun.run_seq)))
        if any(r.status != "queued" or r.run_seq < run.run_seq for r in others):
            return
        run.status, run.started_at = "running", now
    if run.status not in ("running", "waiting", "paused"):
        return
    for row in engine._latest(session, run).values():
        if row.task_id:
            session.scalar(select(Task).where(Task.id == row.task_id).with_for_update()
                           .execution_options(populate_existing=True))
        if row.status in ("active", "waiting") and row.task and row.task.status in ("done", "cancelled", "aborted"):
            engine.on_task_transition(session, row.task, "tick")
        elif row.status == "failed" and row.next_retry_at and row.task and row.task.status == "cancelled":
            engine.on_task_transition(session, row.task, "tick")
    if run.status in ("running", "waiting", "paused"):
        from app.services.workflows.errors import progress

        progress(session, run, now)
    engine.advance(session, run)


def tick(session, now=None):
    now = utc(now or datetime.now(UTC))
    if not enabled(session):
        return {"skipped": "disabled"}
    result = {"processed": 0, "failed": 0}
    with session.get_bind().connect() as lock:
        if lock.scalar(text("SELECT GET_LOCK(:name, 0)"), {"name": LOCK}) != 1:
            return {"skipped": "locked"}
        try:
            ids = list(session.scalars(select(WorkflowSchedule.id).join(WorkflowTemplate).where(
                WorkflowSchedule.enabled.is_(True), WorkflowSchedule.next_fire_at <= now,
                WorkflowTemplate.status == "active").order_by(WorkflowSchedule.next_fire_at, WorkflowSchedule.id)))
            session.commit()
            for schedule_id in ids:
                try:
                    schedule = session.get(WorkflowSchedule, schedule_id)
                    slots = []
                    slot = schedule.next_fire_at
                    # Resume huge backlogs in bounded batches; never discard their audit records.
                    while slot is not None and slot <= now and len(slots) < 10000:
                        slots.append(slot)
                        slot = next_fire(schedule, slot)
                    missed = [value for value in slots if now - value > timedelta(minutes=schedule.misfire_grace_min)]
                    newest = missed[-1] if missed and (slot is None or slot > now or
                        now - slot <= timedelta(minutes=schedule.misfire_grace_min)) else None
                    for slot in slots:
                        # An editor/calendar change may reset the cursor between commits. Lock in the
                        # same order as configuration/start, then reload rather than firing stale slots.
                        session.scalar(select(WorkflowTemplate).where(
                            WorkflowTemplate.id == schedule.template_id).with_for_update()
                            .execution_options(populate_existing=True))
                        schedule = session.scalar(select(WorkflowSchedule).where(
                            WorkflowSchedule.id == schedule_id).with_for_update()
                            .execution_options(populate_existing=True))
                        if (schedule is None or not schedule.enabled or schedule.template.status != "active" or
                                schedule.next_fire_at != slot):
                            session.commit()
                            break
                        old = now - slot > timedelta(minutes=schedule.misfire_grace_min)
                        misfire = old and (schedule.misfire_policy == "skip" or slot != newest)
                        outcome = _fire(session, schedule, slot, now, misfire)
                        session.commit()
                        result["processed"] += 1
                        result["failed"] += outcome == "failed"
                except Exception:
                    session.rollback()
                    result["failed"] += 1
                    log.exception("workflow_schedule_tick_failed", extra={"schedule_id": schedule_id})
            from app.services.workflows.events import process as process_events
            event_processed, event_failed = process_events(session, now)
            result["processed"] += event_processed
            result["failed"] += event_failed
            ids = list(session.scalars(select(WorkflowRun.id).where(
                WorkflowRun.status.in_(("queued", "running", "waiting", "paused"))).order_by(WorkflowRun.id)))
            session.commit()
            for run_id in ids:
                try:
                    run = session.get(WorkflowRun, run_id)
                    session.scalar(select(WorkflowTemplate).where(WorkflowTemplate.id == run.template_id).with_for_update())
                    run = engine._lock(session, run)
                    _progress(session, run, now)
                    session.commit()
                except Exception:
                    session.rollback()
                    result["failed"] += 1
                    log.exception("workflow_progress_tick_failed", extra={"run_id": run_id})
            row = session.get(AppSetting, "workflow_last_tick_at")
            if row is None:
                session.add(AppSetting(setting_key="workflow_last_tick_at", setting_value=now.isoformat()))
            else:
                row.setting_value = now.isoformat()
            session.commit()
        finally:
            lock.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": LOCK})
    return result


def status(session, now=None):
    now = utc(now or datetime.now(UTC))
    last = settings_store.get(session, "workflow_last_tick_at")
    try:
        stale = last is None or now - datetime.fromisoformat(last) > timedelta(minutes=5)
    except ValueError:
        stale = True
    return {"last": last, "stale": stale, "enabled": enabled(session)}
