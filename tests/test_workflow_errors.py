"""Failure recovery with real task hooks, durable clocks, outbox rows and scheduler ticks."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.errors import Conflict
from app.models import Actor, Notification, Task, TaskComment, WorkflowRoleBinding
from app.services import notifications, settings_store, state_machine as sm, tasks
from app.services.workflows import engine, errors
from app.services.workflows.api import run_info
from app.services.workflows.tick import _progress, tick
from tests.test_workflow_engine import enable_notifications, finish, graph, latest  # noqa: F401

pytestmark = pytest.mark.db


@pytest.fixture
def clock(monkeypatch):
    value = [datetime(2026, 11, 2, 9)]
    monkeypatch.setattr(sm, "utcnow", lambda: value[0])
    return value


def advance(s, run, clock, **delta):
    clock[0] += timedelta(**delta)
    _progress(s, run, clock[0])
    s.flush()


def role_alternative(s, template, other, rank=2):
    s.add(WorkflowRoleBinding(project_id=template.project_id, role="writer", actor_id=other.id, rank=rank))
    s.flush()


def test_only_working_time_counts_across_reclaims_questions_reviews_and_restart(graph, clock):
    s, owner, agent, other, template, steps = graph
    steps[0].timeout_minutes = 10
    run = engine.start(s, owner, template)
    row = latest(s, run, 1)
    task = row.task
    advance(s, run, clock, days=2)  # Agent queue is not work.
    assert run.status == "running" and row.deadline_at is None
    sm.transition(s, task, "claim", agent)
    assert row.deadline_at == clock[0] + timedelta(minutes=10)
    advance(s, run, clock, minutes=3)
    sm.transition(s, task, "ask", agent, {"body": "Which format?"})
    assert row.work_us == 180_000_000 and row.deadline_at is None
    advance(s, run, clock, days=3)
    sm.transition(s, task, "answer", owner, {"body": "Markdown"})
    assert row.deadline_at is None  # Answer returns the task to defined.
    sm.transition(s, task, "claim", agent)
    assert row.deadline_at == clock[0] + timedelta(minutes=7)
    advance(s, run, clock, minutes=2)
    sm.transition(s, task, "release", agent, {"body": "Continue later"})
    advance(s, run, clock, days=1)
    sm.transition(s, task, "claim", agent)
    s.commit()
    s.expire_all()  # Persisted clock survives a session reload/process restart.
    assert row.deadline_at == clock[0] + timedelta(minutes=5)
    advance(s, run, clock, minutes=4)
    sm.transition_to(s, task, "for_review", owner, override=True)
    assert row.work_us == 540_000_000 and row.deadline_at is None
    advance(s, run, clock, days=10)
    assert run.status == "waiting" and task.status == "for_review"


def test_timeout_releases_lease_comments_pauses_and_cannot_be_claimed(graph, clock):
    s, owner, agent, other, template, steps = graph
    enable_notifications(s, owner)
    steps[0].timeout_minutes = 5
    run = engine.start(s, owner, template)
    row = latest(s, run, 1)
    task = row.task
    sm.transition(s, task, "claim", agent)
    task.waiting_reason, task.waiting_until = "Deliberate wait", clock[0] + timedelta(hours=1)
    advance(s, run, clock, minutes=5)
    assert run.status == "paused" and run.pause_reason == "timeout"
    assert row.status == "failed" and row.error_code == "timeout" and row.next_retry_at is None
    assert task.status == "defined" and task.lease_expires_at is None and task.waiting_reason is None
    assert s.scalar(select(TaskComment).where(TaskComment.task_id == task.id))
    assert tasks.claim(s, agent) is None
    with pytest.raises(Conflict) as exc:
        sm.transition(s, task, "claim", agent)
    assert exc.value.code == "workflow_recovery_pending"
    advance(s, run, clock, hours=1)
    assert len(s.scalars(select(Notification).where(Notification.event == "workflow_paused")).all()) == 1
    engine.resume(s, owner, run)
    assert run.status == "running" and latest(s, run, 1).attempt == 2
    engine.cancel(s, owner, run)
    assert run.status == "cancelled" and latest(s, run, 1).task.status == "cancelled"


@pytest.mark.parametrize("cause", ["abort", "timeout"])
def test_retry_preserves_task_branch_and_thread_and_changes_role_candidate(graph, clock, cause):
    s, owner, agent, other, template, steps = graph
    steps[0].timeout_minutes, steps[0].max_retries, steps[0].retry_backoff_s = 1, 1, 300
    role_alternative(s, template, other)
    run = engine.start(s, owner, template)
    old = latest(s, run, 1)
    task = old.task
    sm.transition(s, task, "claim", agent)
    branch = task.branch_name
    comment = tasks.comment(s, agent, task, "Keep the analysis so far")
    if cause == "abort":
        sm.transition(s, task, "abort", agent, {"body": "Cannot proceed"})
    else:
        advance(s, run, clock, minutes=1)
    assert old.status == "failed" and run.status == "running"
    assert old.next_retry_at == clock[0] + timedelta(seconds=300)
    advance(s, run, clock, seconds=299)
    assert latest(s, run, 1).id == old.id
    advance(s, run, clock, seconds=1)
    new = latest(s, run, 1)
    assert new.attempt == 2 and new.task_id == task.id
    assert new.task.workflow_run_step_id == new.id and task.workflow_run_step is new
    assert old.assignee_id == agent.id and new.assignee_id == other.id
    assert task.assigned_to == other.id and task.status == "defined" and task.lease_expires_at is None
    assert task.branch_name == branch and s.get(TaskComment, comment.id).body == "Keep the analysis so far"
    assert new.work_us == 0 and new.deadline_at is None
    advance(s, run, clock, minutes=1)
    assert latest(s, run, 1).id == new.id and s.scalar(select(func.count()).select_from(Task)) == 1
    sm.transition(s, task, "claim", other)
    sm.transition(s, task, "abort", other, {"body": "Still fails"})
    assert run.status == "paused" and new.next_retry_at is None
    finish(s, owner, task)  # Stale completion cannot advance a failed attempt.
    assert latest(s, run, 2).task_id is None


def test_exponential_backoff_and_each_candidate_used_once(graph, clock):
    s, owner, agent, other, template, steps = graph
    steps[0].max_retries, steps[0].retry_backoff_s = 2, 300
    role_alternative(s, template, other)
    run = engine.start(s, owner, template)
    task = latest(s, run, 1).task
    sm.transition(s, task, "claim", agent)
    sm.transition(s, task, "abort", agent, {"body": "Failure one"})
    advance(s, run, clock, seconds=300)
    row = latest(s, run, 1)
    sm.transition(s, task, "claim", other)
    sm.transition(s, task, "abort", other, {"body": "Failure two"})
    assert row.next_retry_at == clock[0] + timedelta(seconds=600)
    advance(s, run, clock, seconds=600)
    assert latest(s, run, 1).attempt == 3 and run.pause_reason == "no_assignee"
    assert task.assigned_to == other.id  # Never silently recycle the first failed candidate.
    engine.resume(s, owner, run)
    assert task.status == "cancelled"  # Earlier attempts' open tasks are also closed on restart.


@pytest.mark.parametrize("invalid", ["inactive", "capacity", "zone", "reviewer"])
def test_retry_candidate_revalidated_at_execution(graph, clock, invalid):
    s, owner, agent, other, template, steps = graph
    steps[0].max_retries = 1
    role_alternative(s, template, other)
    run = engine.start(s, owner, template)
    task = latest(s, run, 1).task
    sm.transition(s, task, "claim", agent)
    sm.transition(s, task, "abort", agent, {"body": "Try another"})
    if invalid == "inactive":
        other.is_active = False
    elif invalid == "capacity":
        other.max_parallel_tasks = 0
    elif invalid == "zone":
        task.data_class = "local_only"
        other.model_location = "cloud"
    else:
        task.reviewer_id = other.id
    advance(s, run, clock, minutes=5)
    assert run.status == "paused" and run.pause_reason == "no_assignee"


@pytest.mark.parametrize("code", ["placeholder_unresolved", "no_assignee", "task_cancelled", "checks_failed", "schema_changed"])
def test_non_execution_failures_do_not_retry(graph, clock, code):
    s, owner, agent, other, template, steps = graph
    steps[0].max_retries = 3
    run = engine.start(s, owner, template)
    row = latest(s, run, 1)
    errors.fail(s, run, row, code, clock[0])
    assert run.status == "paused" and row.next_retry_at is None


def test_fixed_assignee_never_silently_reassigned(graph, clock):
    s, owner, agent, other, template, steps = graph
    steps[0].agent_role, steps[0].assignee_id, steps[0].max_retries = None, agent.id, 2
    run = engine.start(s, owner, template)
    task = latest(s, run, 1).task
    sm.transition(s, task, "claim", agent)
    sm.transition(s, task, "abort", agent, {"body": "Failed"})
    assert run.status == "paused" and latest(s, run, 1).next_retry_at is None


def test_reminders_daily_no_timeout_and_long_wait_warning(graph, clock):
    s, owner, agent, other, template, steps = graph
    enable_notifications(s, owner)
    steps[1].timeout_minutes, steps[1].wait_reminder_hours = 1, 2
    run = engine.start(s, owner, template)
    finish(s, owner, latest(s, run, 1).task)
    row = latest(s, run, 2)
    advance(s, run, clock, hours=1)
    assert not s.scalars(select(Notification).where(Notification.event == "workflow_wait_reminder")).all()
    advance(s, run, clock, hours=1)
    advance(s, run, clock, hours=23)
    assert len(s.scalars(select(Notification).where(Notification.event == "workflow_wait_reminder")).all()) == 1
    advance(s, run, clock, hours=1)
    assert len(s.scalars(select(Notification).where(Notification.event == "workflow_wait_reminder")).all()) == 2
    advance(s, run, clock, days=31)
    assert run.status == "waiting" and row.deadline_at is None
    assert "workflow_waiting_too_long" in run_info(s, owner, run)["warnings"]
    engine.cancel(s, owner, run)
    assert row.task.status == "cancelled"


@pytest.mark.parametrize("reassign", [False, True])
def test_claim_reminder_once_and_optional_idle_reassignment(graph, clock, reassign):
    s, owner, agent, other, template, steps = graph
    enable_notifications(s, owner)
    steps[0].config_json = '{"reassign_on_idle":true}' if reassign else None
    role_alternative(s, template, other)
    run = engine.start(s, owner, template)
    row = latest(s, run, 1)
    advance(s, run, clock, hours=3)
    assert row.idle_notified_at is None
    advance(s, run, clock, hours=1)
    assert row.task.assigned_to == (other.id if reassign else agent.id)
    advance(s, run, clock, days=3)
    notices = s.scalars(select(Notification).where(Notification.event == "workflow_claim_reminder")).all()
    assert len(notices) == 1 and run.status == "running"
    assert latest(s, run, 1).attempt == 1


def test_cancel_pending_retry_and_manual_pause_defer_recovery(graph, clock):
    s, owner, agent, other, template, steps = graph
    steps[0].max_retries = 1
    role_alternative(s, template, other)
    run = engine.start(s, owner, template)
    row = latest(s, run, 1)
    sm.transition(s, row.task, "claim", agent)
    sm.transition(s, row.task, "abort", agent, {"body": "Unavailable"})
    engine.pause(s, owner, run)
    advance(s, run, clock, days=1)
    assert latest(s, run, 1).id == row.id
    engine.cancel(s, owner, run)
    assert row.next_retry_at is None and row.task.status == "cancelled"
    advance(s, run, clock, days=1)
    assert latest(s, run, 1).attempt == 1 and run.status == "cancelled"


def test_tick_restart_idempotent_and_disabled_switch(graph, clock):
    s, owner, agent, other, template, steps = graph
    enable_notifications(s, owner)
    steps[0].timeout_minutes = 1
    run = engine.start(s, owner, template)
    sm.transition(s, latest(s, run, 1).task, "claim", agent)
    s.commit()
    clock[0] += timedelta(minutes=1)
    assert tick(s, clock[0]) == {"skipped": "disabled"}
    settings_store.update(s, {"workflows_enabled": "true"}, owner.id)
    s.commit()
    assert tick(s, clock[0])["failed"] == 0
    s.expire_all()
    assert run.status == "paused"
    assert tick(s, clock[0])["failed"] == 0
    assert len(s.scalars(select(Notification).where(Notification.event == "workflow_paused")).all()) == 1


def test_runner_retry_creates_fresh_attempt_and_leaves_old_task_intact(graph, clock, monkeypatch):
    s, owner, agent, other, template, steps = graph
    # WF 10 plugs runner activation into the engine. Here exercise the recovery/activation seam
    # without starting a sandbox or pretending the unsupported step can already run.
    run = engine.start(s, owner, template)
    old = latest(s, run, 1)
    old.step.step_type, old.step.max_retries = "runner", 2
    task = old.task
    errors.fail(s, run, old, "task_aborted", clock[0])
    monkeypatch.setattr(engine, "advance", lambda session, current: None)
    advance(s, run, clock, minutes=5)
    new = latest(s, run, 1)
    assert new.id != old.id and new.attempt == 2 and new.task_id is None
    assert old.task_id == task.id and old.status == "failed"


def test_pause_notification_recipients_settings_link_and_privacy(graph, clock):
    s, owner, agent, other, template, steps = graph
    enable_notifications(s, owner)
    starter = Actor(name="starter", display_name="Starter", actor_type="human", role="orchestrator", runtime="local")
    s.add(starter)
    s.flush()
    from app.models import TelegramLink
    s.add(TelegramLink(actor_id=starter.id, chat_id=54321))
    settings_store.update(s, {"app_url": "https://taskbrigade.example.org"}, owner.id)
    run = engine.start(s, starter, template)
    steps[0].data_class = "local_only"
    errors.fail(s, run, latest(s, run, 1), "timeout", clock[0])
    s.flush()
    notices = s.scalars(select(Notification).where(Notification.event == "workflow_paused")).all()
    assert {n.actor_id for n in notices} == {owner.id, starter.id}
    assert all(f"/workflow-runs/{run.id}" in n.text and "Content" not in n.text for n in notices)
    notifications.save_prefs(s, starter, channels={e: set() for e in notifications.EVENTS})
    notifications.save_prefs(s, owner, mute_all=sm.utcnow() + timedelta(days=1))
    notifications.workflow_event(s, run, "workflow_paused")
    s.flush()
    notices = s.scalars(select(Notification).where(Notification.event == "workflow_paused")).all()
    assert len(notices) == 3 and notices[-1].status == "skipped"


def test_clock_precision_is_durable(graph, clock):
    s, owner, agent, other, template, steps = graph
    run = engine.start(s, owner, template)
    row = latest(s, run, 1)
    clock[0] += timedelta(microseconds=123456)
    sm.transition(s, row.task, "claim", agent)
    s.commit()
    s.expire_all()
    clock[0] += timedelta(microseconds=654321)
    sm.transition(s, row.task, "release", agent, {"body": "Pause work"})
    assert row.work_us == 654321


def test_task_cancel_during_backoff_prevents_retry(graph, clock):
    s, owner, agent, other, template, steps = graph
    steps[0].max_retries = 1
    role_alternative(s, template, other)
    run = engine.start(s, owner, template)
    row = latest(s, run, 1)
    sm.transition(s, row.task, "claim", agent)
    sm.transition(s, row.task, "abort", agent, {"body": "Temporary failure"})
    sm.transition(s, row.task, "cancel", owner)
    advance(s, run, clock, minutes=10)
    assert run.status == "paused" and run.pause_reason == "task_cancelled"
    assert row.next_retry_at is None and latest(s, run, 1).attempt == 1
    assert row.task.status == "cancelled"


def test_default_agent_timeout_and_pause_does_not_stop_work_clock(graph, clock):
    s, owner, agent, other, template, steps = graph
    run = engine.start(s, owner, template)
    row = latest(s, run, 1)
    sm.transition(s, row.task, "claim", agent)
    assert row.deadline_at == clock[0] + timedelta(hours=24)
    engine.pause(s, owner, run)
    advance(s, run, clock, hours=24)
    assert run.status == "paused" and run.pause_reason == "manual"
    assert row.status == "failed" and row.error_code == "timeout"


def test_error_handling_settings_roundtrip_in_editor_and_api(graph):
    from app.api.workflows import Draft, StepDraft
    from app.services.workflows.api import save_draft, template_info
    from app.services.workflows.ui import editor_form, to_draft

    s, owner, agent, other, template, steps = graph
    steps[0].wait_reminder_hours, steps[0].retry_backoff_s = 7, 42
    steps[0].max_retries, steps[0].timeout_minutes = 2, 11
    form = editor_form(template, template.current_version)
    body = to_draft(form)
    assert body.steps[0].wait_reminder_hours == 7 and body.steps[0].retry_backoff_s == 42
    assert body.steps[0].max_retries == 2 and body.steps[0].timeout_minutes == 11
    draft = save_draft(s, owner, template.project, template.workflow_key, Draft.model_validate(body.model_dump()))
    # save_draft returns the template/version pair.
    version = draft[1]
    fields = template_info(s, owner, template, version)["steps"][0]
    assert fields["wait_reminder_hours"] == 7 and fields["retry_backoff_s"] == 42
    assert StepDraft(step_key="run", step_type="runner", title_tpl="Run").max_retries == 2
    assert StepDraft(step_key="run", step_type="runner", title_tpl="Run", max_retries=0).max_retries == 0


def test_timeout_notification_failure_rolls_back_lease_and_recovery(graph, clock, monkeypatch):
    s, owner, agent, other, template, steps = graph
    steps[0].timeout_minutes = 1
    run = engine.start(s, owner, template)
    row = latest(s, run, 1)
    sm.transition(s, row.task, "claim", agent)
    s.commit()
    def unavailable(*args, **kwargs):
        raise RuntimeError("Queue unavailable")
    monkeypatch.setattr(notifications, "workflow_event", unavailable)
    clock[0] += timedelta(minutes=1)
    with pytest.raises(RuntimeError), s.begin_nested():
        _progress(s, run, clock[0])
    assert row.status == "active" and row.task.status == "in_progress"
    assert row.task.lease_expires_at is not None and run.status == "running"


def test_wait_warning_in_run_history(graph, clock):
    from app.services.workflows.ui import history

    s, owner, agent, other, template, steps = graph
    run = engine.start(s, owner, template)
    finish(s, owner, latest(s, run, 1).task)
    advance(s, run, clock, days=30)
    assert history(s, template)["cells"][run.id]["approve"]["waiting_too_long"] is True


def test_error_migration_roundtrip_preserves_running_tasks(graph, clock, ready_db):
    from alembic import command
    from sqlalchemy import inspect, text
    from app.db import alembic_config

    s, owner, agent, other, template, steps = graph
    run = engine.start(s, owner, template)
    row = latest(s, run, 1)
    sm.transition(s, row.task, "claim", agent)
    s.commit()
    cfg = alembic_config()
    with ready_db.engine.begin() as connection:
        cfg.attributes["connection"] = connection
        before = connection.execute(text("SELECT task_nr, status, lease_expires_at FROM tasks")).all()
        command.downgrade(cfg, "0026")
        assert "work_us" not in {c["name"] for c in inspect(connection).get_columns("workflow_run_steps")}
        command.upgrade(cfg, "head")
        assert connection.execute(text("SELECT task_nr, status, lease_expires_at FROM tasks")).all() == before
        clock_row = connection.execute(text("SELECT work_us, work_started_at, assignee_id FROM workflow_run_steps "
                                            "WHERE id=:id"), {"id": row.id}).one()
        assert clock_row[0] == 0 and clock_row[1] is not None and clock_row[2] == agent.id


def test_manual_resume_supersedes_pending_retry(graph, clock):
    s, owner, agent, other, template, steps = graph
    steps[0].max_retries = 1
    role_alternative(s, template, other)
    run = engine.start(s, owner, template)
    row = latest(s, run, 1)
    sm.transition(s, row.task, "claim", agent)
    sm.transition(s, row.task, "abort", agent, {"body": "Temporary failure"})
    assert row.next_retry_at is not None
    engine.pause(s, owner, run)
    engine.resume(s, owner, run)
    assert row.next_retry_at is None and row.task.status == "cancelled"
    assert latest(s, run, 1).attempt == 2 and run.status == "running"
