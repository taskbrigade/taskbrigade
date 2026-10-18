"""Real MariaDB slot atomicity, named-lock ownership and queue semantics."""
import json
from datetime import datetime

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.errors import Conflict, Forbidden
from app.models import (AppSetting, WorkflowParam, WorkflowRun, WorkflowSchedule,
                        WorkflowScheduleFire)
from app.services import settings_store
from app.services.workflows import engine, schedules
from app.services.workflows.tick import LOCK, tick
from tests.test_workflow_engine import graph  # noqa: F401

pytestmark = pytest.mark.db


def setup(graph, **kwargs):
    s, owner, agent, other, template, steps = graph
    agent.max_parallel_tasks = 20
    settings_store.update(s, {"workflows_enabled": "true"}, owner.id)
    row = schedules.save(s, owner, template, dict(name="Daily", kind="cron", cron_expr="0 7 * * *",
        timezone="Europe/Berlin", enabled=True, **kwargs), now=datetime(2026, 11, 1, 6))
    s.commit()
    return s, owner, template, row


def fires(s):
    return list(s.scalars(select(WorkflowScheduleFire).order_by(WorkflowScheduleFire.scheduled_for)))


def test_idempotency_and_two_unique_slot_barriers(graph):
    s, owner, template, row = setup(graph)
    now = datetime(2026, 11, 2, 6)
    assert tick(s, now)["processed"] == 1
    run = s.scalar(select(WorkflowRun))
    assert run.started_by is None and run.trigger_kind == "schedule" and run.schedule_id == row.id
    assert run.scheduled_for == now and row.next_fire_at == datetime(2026, 11, 3, 6)
    assert tick(s, now)["processed"] == 0
    assert len(fires(s)) == 1 and fires(s)[0].run_id == run.id
    assert s.get(AppSetting, "workflow_last_tick_at").setting_value == now.isoformat()
    with pytest.raises(IntegrityError), s.begin_nested():
        s.add(WorkflowScheduleFire(schedule_id=row.id, scheduled_for=now, fired_at=now, outcome="started"))
        s.flush()
    with pytest.raises(IntegrityError), s.begin_nested():
        s.add(WorkflowRun(template_id=template.id, version_id=run.version_id, project_id=run.project_id,
            run_seq=99, trigger_kind="schedule", schedule_id=row.id, scheduled_for=now,
            params_json="{}", status="queued"))
        s.flush()


def test_lock_held_on_dedicated_connection(graph, ready_db):
    s, owner, template, row = setup(graph)
    with ready_db.engine.connect() as lock:
        assert lock.scalar(text("SELECT GET_LOCK(:name, 0)"), {"name": LOCK}) == 1
        assert tick(s, datetime(2026, 11, 2, 6)) == {"skipped": "locked"}
        lock.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": LOCK})
    assert not fires(s)
    assert tick(s, datetime(2026, 11, 2, 6))["processed"] == 1


def test_queue_keeps_original_period_and_one_waiter(graph):
    s, owner, template, row = setup(graph, overlap_policy="queue")
    s.add(WorkflowParam(version_id=template.current_version_id, name="period", label="Period", param_type="month",
                        default_expr="{{run.period}}"))
    s.commit()
    tick(s, datetime(2026, 11, 2, 6))
    first = s.scalar(select(WorkflowRun))
    engine.pause(s, owner, first)
    s.commit()
    tick(s, datetime(2026, 12, 2, 6))
    runs = list(s.scalars(select(WorkflowRun).order_by(WorkflowRun.run_seq)))
    assert len(runs) == 2 and runs[1].status == "queued" and runs[1].started_at is None
    assert json.loads(runs[1].params_json)["period"] == "2026-11"
    assert fires(s)[-2].outcome == "queued"
    assert fires(s)[-2].scheduled_for == datetime(2026, 12, 1, 6)
    assert fires(s)[-1].outcome == "skipped_overlap"
    tick(s, datetime(2026, 12, 3, 6))
    assert fires(s)[-1].outcome == "skipped_overlap"
    engine.cancel(s, owner, first)
    s.commit()
    tick(s, datetime(2026, 12, 3, 6))
    assert runs[1].status == "running" and runs[1].started_at == datetime(2026, 12, 3, 6)
    assert json.loads(runs[1].params_json)["period"] == "2026-11"


@pytest.mark.parametrize("policy,expected", [("run_late", ["skipped_misfire", "skipped_misfire", "started"]),
    ("skip", ["skipped_misfire"] * 3)])
def test_misfires_after_outage(graph, policy, expected):
    s, owner, template, row = setup(graph, misfire_policy=policy)
    tick(s, datetime(2026, 11, 4, 20))
    assert [r.outcome for r in fires(s)] == expected
    assert row.next_fire_at == datetime(2026, 11, 5, 6)
    if policy == "run_late":
        assert s.scalar(select(WorkflowRun)).scheduled_for == datetime(2026, 11, 4, 6)


def test_inside_grace_and_inactive_template(graph):
    s, owner, template, row = setup(graph, misfire_policy="skip")
    template.status = "paused"
    s.commit()
    assert tick(s, datetime(2026, 11, 2, 12))["processed"] == 0
    template.status = "active"
    s.commit()
    tick(s, datetime(2026, 11, 2, 12))
    assert fires(s)[0].outcome == "started"  # exactly six hours is inside grace


def test_broken_schedule_does_not_block_others(graph):
    s, owner, template, row = setup(graph)
    row.params_json = '{"unknown":"value"}'
    other = schedules.save(s, owner, template, dict(name="Good", kind="cron", cron_expr="0 7 * * *",
        enabled=True), now=datetime(2026, 11, 1, 6))
    s.commit()
    result = tick(s, datetime(2026, 11, 2, 6))
    assert result == {"processed": 2, "failed": 1}
    assert {r.outcome for r in fires(s)} == {"failed", "started"}
    assert row.last_outcome == "failed" and other.last_outcome == "started"


def test_edit_resets_cursor_preview_does_not_write_and_agent_denied(graph):
    s, owner, template, row = setup(graph)
    now = datetime(2026, 12, 1)
    values = dict(name="Changed", kind="cron", cron_expr="0 8 * * *", enabled=False)
    candidate = schedules.save(s, owner, template, values, row.id, now=now, persist=False)
    assert candidate.id is None and row.name == "Daily"
    assert s.scalar(select(func.count()).select_from(WorkflowSchedule)) == 1
    schedules.save(s, owner, template, values, row.id, now=now)
    assert row.last_fire_at == now and row.next_fire_at == datetime(2026, 12, 1, 7)
    assert not row.enabled
    with pytest.raises(Forbidden):
        schedules.save(s, graph[2], template, values)


def test_manual_overlap_controls(graph):
    s, owner, template, row = setup(graph)
    first = engine.start(s, owner, template)
    with pytest.raises(Conflict) as exc:
        engine.start(s, owner, template)
    assert exc.value.code == "workflow_overlap"
    queued = engine.start(s, owner, template, queue=True)
    assert queued.status == "queued"
    with pytest.raises(Conflict):
        engine.start(s, owner, template, queue=True)
    parallel = engine.start(s, owner, template, force_parallel=True)
    assert parallel.status in ("running", "paused") and first.id != parallel.id


def test_notices_limited_to_one_per_template_and_utc_day(graph, monkeypatch):
    s, owner, template, row = setup(graph)
    from app.services.workflows import tick as scheduler
    calls = []
    monkeypatch.setattr(scheduler, "_notice", lambda s, run, event: calls.append((run.template_id, event)))
    engine.start(s, owner, template)
    row.cron_expr = "0 * * * *"
    row.next_fire_at = datetime(2026, 11, 2, 6)
    # Two schedules of the same template must share the daily throttle.
    other = schedules.save(s, owner, template, dict(name="Also daily", kind="cron", cron_expr="0 8 * * *",
        enabled=True), now=datetime(2026, 11, 1, 8))
    s.commit()
    tick(s, datetime(2026, 11, 2, 10))
    assert len(calls) == 1
    assert row.last_outcome == other.last_outcome == "skipped_overlap"
    tick(s, datetime(2026, 11, 3, 6))
    assert len(calls) == 2


def test_terminal_task_reconciliation(graph):
    s, owner, template, row = setup(graph)
    run = engine.start(s, owner, template)
    first = engine._latest(s, run)[1]
    # Simulate a previous code path that saved the terminal task without the workflow hook.
    first.task.status = "done"
    s.commit()
    tick(s, datetime(2026, 11, 1, 7))
    assert engine._latest(s, run)[1].status == "succeeded"
    assert run.current_position == 2 and run.status == "waiting"


def test_schedule_expression_uses_slot_timezone_and_latest_version(graph):
    s, owner, template, row = setup(graph)
    from app.models import WorkflowTemplateVersion, WorkflowStep
    version = WorkflowTemplateVersion(template_id=template.id, version=2, state="published", created_by=owner.id)
    s.add(version)
    s.flush()
    s.add(WorkflowParam(version_id=version.id, name="month", param_type="month", label="Month"))
    s.add(WorkflowStep(version_id=version.id, position=1, step_key="write", step_type="agent",
                      title_tpl="Report {{month}}", agent_role="writer", review_mode="manual",
                      description_tpl="Create the report", acceptance_tpl="Report exists"))
    template.current_version = version
    row.timezone = "America/New_York"
    row.params_json = '{"month":"{{run.period}}"}'
    s.flush()
    slot = datetime(2026, 11, 1, 0, 30)  # Still October in New York: period is September.
    run = engine.start(s, owner, template, schedule=row, trigger_kind="schedule", scheduled_for=slot,
                       params=json.loads(row.params_json))
    assert run.version_id == version.id and run.schedule_timezone == "America/New_York"
    assert json.loads(run.params_json)["month"] == "2026-09"
    row.timezone = "Europe/Berlin"
    from app.services.workflows.rendering import context
    from app.services.workflows.placeholders import render
    assert render("{{run.period}}", context(run)).text == "2026-09"


def test_calendar_cli_persists_exceptions_and_recalculates_schedules(graph, ready_db, monkeypatch):
    from datetime import UTC
    from typer.testing import CliRunner
    from cli import tm_admin, workflow_admin
    from app.models import WorkflowCalendar

    s, owner, template, row = setup(graph)
    runner = CliRunner()
    monkeypatch.setattr(workflow_admin, "database", lambda: ready_db)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 1, tzinfo=UTC)

    monkeypatch.setattr(workflow_admin, "datetime", FixedDateTime)
    result = runner.invoke(tm_admin.app, ["workflows", "calendar", "add", "Region", "--country", "DE",
                                       "--subdivision", "BW"])
    assert result.exit_code == 0, result.output
    calendar_id = int(result.stdout.strip())
    row.calendar_id = calendar_id
    s.commit()
    assert s.get(WorkflowCalendar, calendar_id).subdivision == "BW"
    result = runner.invoke(tm_admin.app, ["workflows", "calendar", "day", str(calendar_id), "2026-11-02",
                                       "closed", "--note", "Company closure"])
    assert result.exit_code == 0, result.output
    s.commit(); s.refresh(row)
    assert row.last_fire_at == datetime(2026, 11, 1) and row.next_fire_at == datetime(2026, 11, 3, 6)
    result = runner.invoke(tm_admin.app, ["workflows", "calendar", "check", str(calendar_id), "2026-11"])
    assert result.exit_code == 0 and "2026-11-02" not in result.stdout and "2026-11-03" in result.stdout
    result = runner.invoke(tm_admin.app, ["workflows", "calendar", "day", str(calendar_id), "2026-11-02", "remove"])
    assert result.exit_code == 0, result.output
    s.commit(); s.refresh(row)
    assert row.next_fire_at == datetime(2026, 11, 2, 6)
