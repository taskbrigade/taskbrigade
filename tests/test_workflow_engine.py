"""Pinned, transactional workflow execution against MariaDB (no real notifications or repository writes)."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import func, select

from app.errors import Forbidden, Unprocessable
from app.models import (
    Actor, Notification, Project, ProjectMember, Sprint, Task, TaskComment, TaskEvent, TelegramLink,
    WorkflowParam, WorkflowRoleBinding, WorkflowRun, WorkflowStep, WorkflowTemplate, WorkflowTemplateVersion,
)
from app.services import notifications, settings_store, sprints, state_machine as sm
from app.services.workflows import engine as wf

pytestmark = pytest.mark.db


@pytest.fixture
def graph(ready_db):
    with ready_db.session() as s:
        owner = s.scalar(select(Actor).where(Actor.name == "admin"))
        agent = s.scalar(select(Actor).where(Actor.name == "codex"))
        other = s.scalar(select(Actor).where(Actor.name == "hermes"))
        project = Project(project_key="DEMO", name="Demo", status="active", privacy="cloud_ok",
                          owner_id=owner.id, created_by=owner.id)
        s.add(project)
        s.flush()
        for actor in (owner, agent, other):
            s.add(ProjectMember(project_id=project.id, actor_id=actor.id, gitea_permission="write"))
        template = WorkflowTemplate(project_id=project.id, workflow_key="content", name="Content",
                                    owner_id=owner.id, created_by=owner.id)
        s.add(template)
        s.flush()
        version = WorkflowTemplateVersion(template_id=template.id, version=1, state="published", created_by=owner.id)
        s.add(version)
        s.flush()
        template.current_version = version
        steps = [
            WorkflowStep(version_id=version.id, position=1, step_key="write", step_type="agent",
                         title_tpl="Write report", description_tpl="Create the report", acceptance_tpl="Report exists",
                         agent_role="writer", review_mode="manual"),
            WorkflowStep(version_id=version.id, position=2, step_key="approve", step_type="approval",
                         title_tpl="Release report", assignee_id=owner.id, review_mode="manual"),
            WorkflowStep(version_id=version.id, position=3, step_key="inform", step_type="notify",
                         title_tpl="Report released", config_json='{"to":["owner"],"text":"Report is ready"}'),
        ]
        s.add_all(steps)
        s.add(WorkflowRoleBinding(project_id=project.id, role="writer", actor_id=agent.id, rank=1))
        s.flush()
        yield s, owner, agent, other, template, steps


def latest(s, run, position):
    return wf._latest(s, run)[position]


def finish(s, owner, task):
    # Human status overrides must invoke the same hook as normal approve.
    sm.transition_to(s, task, "done", owner, override=True)


def enable_notifications(s, owner):
    settings_store.update(s, {"telegram_enabled": "true", "telegram_bot_token": "123456:test-token"}, owner.id)
    s.add(TelegramLink(actor_id=owner.id, chat_id=12345))
    s.flush()


def test_three_steps_pinned_and_idempotent(graph):
    s, owner, agent, other, template, steps = graph
    enable_notifications(s, owner)
    run = wf.start(s, owner, template)
    first = latest(s, run, 1)
    assert run.status == "running" and run.current_position == 1
    assert first.task.assigned_to == agent.id and first.task.workflow_run_step_id == first.id
    assert first.task.sprint_id is None and first.task.task_kind == "work"
    assert s.scalar(select(func.count()).select_from(Task)) == 1
    # Publishing another version cannot affect the pinned run, including resume/rework.
    v2 = WorkflowTemplateVersion(template_id=template.id, version=2, state="published", created_by=owner.id)
    s.add(v2); s.flush()
    template.current_version = v2
    finish(s, owner, first.task)
    approval = latest(s, run, 2)
    assert run.version_id == steps[0].version_id and run.status == "waiting"
    assert approval.task.task_kind == "approval" and approval.task.status == "for_review"
    assert approval.task.assigned_to is None and approval.task.reviewer_id == owner.id
    assert approval.task.follow_up_of_id == first.task_id
    wf.advance(s, run)
    assert s.scalar(select(func.count()).select_from(Task)) == 2
    sm.transition(s, approval.task, "approve", owner)
    assert run.status == "succeeded" and run.finished_at
    assert latest(s, run, 3).status == "succeeded" and latest(s, run, 3).task_id is None
    wf.on_task_transition(s, approval.task, "approve", owner)
    wf.advance(s, run)
    messages = s.scalars(select(Notification).where(Notification.event == "workflow_notify")).all()
    assert len(messages) == 1 and "Report is ready" in messages[0].text


@pytest.mark.parametrize("explicit", [True, False])
def test_rejection_reopens_same_task_and_new_approval_attempt(graph, explicit):
    s, owner, agent, other, template, steps = graph
    if explicit:
        steps[1].config_json = '{"on_reject":"rework:write"}'
    run = wf.start(s, owner, template)
    first = latest(s, run, 1)
    finish(s, owner, first.task)
    old = latest(s, run, 2)
    sm.transition(s, old.task, "reject", owner, {"body": "Add sources"})
    assert first.task.status == "changes_requested" and first.status == "active"
    assert run.current_position == 1 and run.status == "running"
    assert old.status == "failed" and old.error_code == "approval_rejected"
    assert latest(s, run, 2).attempt == 2 and latest(s, run, 2).task_id is None
    assert s.scalar(select(TaskComment).where(TaskComment.task_id == first.task_id,
                                              TaskComment.body == "Add sources"))
    finish(s, owner, first.task)
    new = latest(s, run, 2)
    assert new.task_id != old.task_id and new.status == "waiting"
    wf.on_task_transition(s, old.task, "approve", owner)
    assert run.status == "waiting" and new.status == "waiting"
    sm.transition(s, new.task, "approve", owner)
    assert run.status == "succeeded" and first.attempt == 1


@pytest.mark.parametrize("config", [{"on_reject": "fail"}, {"max_rework": 0}, {"on_reject": "rework:missing"}])
def test_rejection_pauses_and_resume_retries_approval(graph, config):
    s, owner, agent, other, template, steps = graph
    steps[1].config_json = json.dumps(config)
    run = wf.start(s, owner, template)
    finish(s, owner, latest(s, run, 1).task)
    sm.transition(s, latest(s, run, 2).task, "reject", owner, {"body": "No"})
    assert run.status == "paused" and run.pause_reason == "approval_rejected"
    wf.resume(s, owner, run)
    assert run.status == "waiting" and latest(s, run, 2).attempt == 2


def test_pause_records_completion_resume_and_cancel(graph):
    s, owner, agent, other, template, steps = graph
    run = wf.start(s, owner, template)
    wf.pause(s, owner, run)
    first = latest(s, run, 1)
    sm.transition(s, first.task, "claim", agent)
    assert run.status == "paused"
    finish(s, owner, first.task)
    assert first.status == "succeeded" and run.status == "paused"
    assert latest(s, run, 2).task_id is None
    wf.resume(s, owner, run)
    approval = latest(s, run, 2)
    wf.cancel(s, owner, run)
    assert run.status == "cancelled" and approval.task.status == "cancelled"
    assert first.task.status == "done" and latest(s, run, 3).status == "cancelled"
    wf.cancel(s, owner, run)
    wf.on_task_transition(s, first.task, "approve", owner)
    assert run.status == "cancelled"


@pytest.mark.parametrize("action,reason", [("abort", "task_aborted"), ("cancel", "task_cancelled")])
def test_terminal_task_hooks_and_failed_step_retry(graph, action, reason):
    s, owner, agent, other, template, steps = graph
    run = wf.start(s, owner, template)
    task = latest(s, run, 1).task
    sm.transition(s, task, "claim", agent)
    sm.transition(s, task, action, owner, {"body": "Cannot continue"})
    assert run.status == "paused" and run.pause_reason == reason
    wf.resume(s, owner, run)
    retry = latest(s, run, 1)
    assert retry.attempt == 2 and retry.task_id != task.id and retry.task.status == "defined"
    assert task.status == "cancelled"


def test_questions_pause_work_and_answer_restores_run(graph):
    s, owner, agent, other, template, steps = graph
    run = wf.start(s, owner, template)
    task = latest(s, run, 1).task
    sm.transition(s, task, "claim", agent)
    sm.transition(s, task, "ask", agent, {"body": "Which format?"})
    assert run.status == "waiting" and latest(s, run, 1).status == "waiting"
    sm.transition(s, task, "answer", owner, {"body": "Markdown"})
    assert run.status == "running" and latest(s, run, 1).status == "active"


def test_roles_rank_load_capacity_and_zone(graph):
    s, owner, agent, other, template, steps = graph
    s.add(WorkflowRoleBinding(project_id=template.project_id, role="writer", actor_id=other.id, rank=1))
    agent.max_parallel_tasks = other.max_parallel_tasks = 3
    # More open tasks loses a rank tie, even if those tasks are not claimed yet.
    s.add(Task(project_id=template.project_id, task_nr="DEMO-9000", title="Busy", created_by=owner.id,
               assigned_to=agent.id, status="defined"))
    run = wf.start(s, owner, template)
    assert latest(s, run, 1).task.assigned_to == other.id
    wf.cancel(s, owner, run)
    other.max_parallel_tasks = 0
    agent.is_active = False
    run2 = wf.start(s, owner, template)
    assert run2.status == "paused" and run2.pause_reason == "no_assignee"
    assert latest(s, run2, 1).task_id is None


@pytest.mark.parametrize("change", ["inactive", "membership", "runner", "human", "ambiguous"])
def test_invalid_assignees_do_not_fall_back(graph, change):
    s, owner, agent, other, template, steps = graph
    if change == "inactive":
        agent.is_active = False
    elif change == "membership":
        s.delete(s.get(ProjectMember, (template.project_id, agent.id)))
    elif change == "runner":
        agent.model_location = "none"
    elif change == "human":
        steps[0].agent_role, steps[0].assignee_id = None, owner.id
    else:
        steps[0].assignee_id = agent.id
    run = wf.start(s, owner, template)
    assert run.pause_reason == "no_assignee" and latest(s, run, 1).status == "failed"


def test_resume_from_earlier_step_cancels_later_tasks_and_skip_rules(graph):
    s, owner, agent, other, template, steps = graph
    run = wf.start(s, owner, template)
    finish(s, owner, latest(s, run, 1).task)
    approval = latest(s, run, 2).task
    wf.pause(s, owner, run)
    with pytest.raises(Unprocessable):
        wf.resume(s, owner, run, from_step="inform")
    with pytest.raises(Unprocessable):
        wf.resume(s, owner, run, skip=True)
    wf.resume(s, owner, run, from_step="write")
    assert approval.status == "cancelled" and latest(s, run, 1).attempt == 2
    steps[0].optional = True
    wf.pause(s, owner, run)
    wf.resume(s, owner, run, skip=True)
    assert latest(s, run, 1).status == "skipped" and run.status == "waiting"


def test_permissions_and_sprint_guard(graph):
    s, owner, agent, other, template, steps = graph
    with pytest.raises(Forbidden):
        wf.start(s, agent, template)
    agent.role = "admin"
    with pytest.raises(Forbidden):
        wf.start(s, agent, template)
    agent.role = "worker"
    run = wf.start(s, owner, template)
    for action in (wf.pause, wf.resume, wf.cancel):
        with pytest.raises(Forbidden):
            action(s, other, run)
    sprint = Sprint(project_id=template.project_id, name="Sprint", created_by=owner.id)
    s.add(sprint); s.flush()
    with pytest.raises(Unprocessable) as exc:
        sprints.assign(s, latest(s, run, 1).task, sprint, owner)
    assert exc.value.code == "workflow_task_no_sprint"
    other.role = "orchestrator"
    other_run = wf.start(s, other, template, queue=True)
    wf.cancel(s, other, other_run)
    assert other_run.status == "cancelled"


@pytest.mark.parametrize("kind,reason", [("gate", "workflow_gate_invalid"), ("runner", "no_assignee"),
                                         ("script", "workflow_script_invalid")])
def test_unconfigured_step_types_never_auto_complete(graph, kind, reason):
    s, owner, agent, other, template, steps = graph
    steps[0].step_type = kind
    run = wf.start(s, owner, template)
    assert run.status == "paused" and run.pause_reason == reason


def test_unresolved_placeholder_pauses_before_task_creation(graph):
    s, owner, agent, other, template, steps = graph
    steps[0].description_tpl = "Use {{steps.missing.output.value}}"
    run = wf.start(s, owner, template)
    assert run.pause_reason == "placeholder_unresolved" and latest(s, run, 1).task_id is None


def test_zone_boundary_never_automatic(graph):
    s, owner, agent, other, template, steps = graph
    steps[0].step_type, steps[0].data_class = "notify", "sealed"
    steps[1].description_tpl = '{{steps.write.summary | default("hidden")}}'
    run = wf.start(s, owner, template)
    assert latest(s, run, 1).status == "succeeded"
    assert run.status == "paused" and run.pause_reason == "zone_boundary_without_gate"
    assert latest(s, run, 2).task_id is None


def test_restricted_notifications_omit_names_and_content(graph):
    s, owner, agent, other, template, steps = graph
    enable_notifications(s, owner)
    steps[0].data_class, steps[0].step_type = "local_only", "notify"
    steps[0].config_json = '{"text":"Confidential content"}'
    wf.start(s, owner, template)
    message = s.scalar(select(Notification).where(Notification.event == "workflow_notify"))
    assert "Confidential" not in message.text and "Content" not in message.text


def test_notify_queue_failure_rolls_back_task_completion(graph, monkeypatch):
    s, owner, agent, other, template, steps = graph
    run = wf.start(s, owner, template)
    finish(s, owner, latest(s, run, 1).task)
    s.commit()
    approval = latest(s, run, 2).task
    def fail(*args, **kwargs):
        raise RuntimeError("Queue unavailable")
    monkeypatch.setattr(notifications, "workflow_event", fail)
    with pytest.raises(RuntimeError), s.begin_nested():
        sm.transition(s, approval, "approve", owner)
    assert approval.status == "for_review" and run.status == "waiting"
    assert latest(s, run, 3).status == "pending"


def test_auto_review_uses_existing_merge_path(graph, monkeypatch):
    s, owner, agent, other, template, steps = graph
    steps[0].review_mode = "auto"
    run = wf.start(s, owner, template)
    task = latest(s, run, 1).task
    merged = []
    def approve(session, current, actor, payload):
        merged.append(current.id)
        current.merge_commit_sha = "c" * 40
        current.done_at = sm.utcnow()
    monkeypatch.setattr(sm, "_approve", approve)
    # The submission path is independently covered by existing repository integration tests.
    monkeypatch.setitem(sm.EFFECTS, "submit", lambda *args: None)
    sm.transition(s, task, "claim", agent)
    sm.transition(s, task, "submit", agent)
    assert merged == [task.id] and task.status == "done"
    assert task.merge_commit_sha == "c" * 40 and run.status == "waiting"
    assert s.scalar(select(TaskEvent).where(TaskEvent.task_id == task.id, TaskEvent.note == "auto_approved"))


def test_notifications_honour_preferences(graph):
    s, owner, agent, other, template, steps = graph
    enable_notifications(s, owner)
    notifications.save_prefs(s, owner, mute_all=sm.utcnow() + timedelta(hours=1))
    steps[0].step_type = "notify"
    run = wf.start(s, owner, template)
    message = s.scalar(select(Notification).where(Notification.event == "workflow_notify"))
    assert message.status == "skipped" and message.text is None
    assert latest(s, run, 1).status == "succeeded"


def test_default_rework_limit_is_three_rounds(graph):
    s, owner, agent, other, template, steps = graph
    run = wf.start(s, owner, template)
    for round_number in range(1, 5):
        finish(s, owner, latest(s, run, 1).task)
        sm.transition(s, latest(s, run, 2).task, "reject", owner, {"body": f"Revision {round_number}"})
        assert run.status == ("running" if round_number <= 3 else "paused")
    assert run.pause_reason == "approval_rejected" and latest(s, run, 2).attempt == 4


def test_role_resolution_respects_rank_then_zone(graph):
    s, owner, agent, other, template, steps = graph
    template.project.privacy = "local_only"
    s.add(WorkflowRoleBinding(project_id=template.project_id, role="writer", actor_id=other.id, rank=2))
    run = wf.start(s, owner, template)
    assert latest(s, run, 1).task.assigned_to == other.id
    wf.cancel(s, owner, run)
    template.project.privacy = "cloud_ok"
    other.max_parallel_tasks = 5
    run = wf.start(s, owner, template)
    assert latest(s, run, 1).task.assigned_to == agent.id  # Preferred rank wins, even if load differs.


def test_concurrent_starts_allocate_distinct_sequences(graph, ready_db):
    s, owner, agent, other, template, steps = graph
    agent.max_parallel_tasks = 3
    template_id, owner_id = template.id, owner.id
    s.commit()
    barrier = Barrier(2)
    def start_run():
        with ready_db.session() as own:
            # Both requests establish a repeatable-read snapshot before the template lock.
            current = own.get(WorkflowTemplate, template_id)
            by = own.get(Actor, owner_id)
            barrier.wait(timeout=10)
            run = wf.start(own, by, current, force_parallel=True)
            result = run.run_seq
            own.commit()
            return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(start_run), pool.submit(start_run)]
        assert sorted(f.result(timeout=15) for f in futures) == [1, 2]


def test_concurrent_activation_creates_one_successor(graph, ready_db):
    s, owner, agent, other, template, steps = graph
    run = wf.start(s, owner, template)
    wf.pause(s, owner, run)
    finish(s, owner, latest(s, run, 1).task)
    run.status = "running"
    run_id = run.id
    s.commit()
    barrier = Barrier(2)
    def activate():
        with ready_db.session() as own:
            current = own.get(WorkflowRun, run_id)
            barrier.wait(timeout=10)
            wf.advance(own, current)
            own.commit()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(activate), pool.submit(activate)]
        for future in futures:
            future.result(timeout=15)
    s.expire_all()
    assert s.scalar(select(func.count()).select_from(Task)) == 2
    assert latest(s, run, 2).task.status == "for_review"


def test_invalid_notification_config_pauses(graph):
    s, owner, agent, other, template, steps = graph
    steps[0].step_type, steps[0].config_json = "notify", '{"to":["unknown"]}'
    run = wf.start(s, owner, template)
    assert run.pause_reason == "workflow_config_invalid" and latest(s, run, 1).status == "failed"


def test_auto_review_refuses_lower_class_downstream(graph):
    s, owner, agent, other, template, steps = graph
    template.project.privacy = "local_only"
    steps[0].review_mode = "auto"
    steps[1].data_class = "cloud_ok"
    run = wf.start(s, owner, template)
    assert run.status == "paused" and run.pause_reason == "auto_review_forbidden"


def test_manual_approval_decision_requires_human_at_zone_boundary(graph):
    s, owner, agent, other, template, steps = graph
    run = wf.start(s, owner, template)
    finish(s, owner, latest(s, run, 1).task)
    # Simulate a project class change after publication/activation.
    steps[1].data_class = "local_only"
    other.role = "admin"
    with pytest.raises(Forbidden):
        sm.transition(s, latest(s, run, 2).task, "approve", other)
    assert latest(s, run, 2).task.status == "for_review"


def test_reviewer_agent_can_decide_approval_without_a_pull_request(graph):
    s, owner, agent, other, template, steps = graph
    other.role = "reviewer"
    steps[1].assignee_id, steps[1].review_mode = other.id, "agent"
    run = wf.start(s, owner, template)
    finish(s, owner, latest(s, run, 1).task)
    approval = latest(s, run, 2).task
    assert approval.reviewer_id == other.id
    sm.transition(s, approval, "approve", other, {"body": "Approved"})
    assert run.status == "succeeded" and approval.pr_number is None


def test_reassignment_after_question_updates_waiting_run(graph):
    from app.services import tasks

    s, owner, agent, other, template, steps = graph
    run = wf.start(s, owner, template)
    task = latest(s, run, 1).task
    sm.transition(s, task, "claim", agent)
    sm.transition(s, task, "ask", agent, {"body": "Need another agent"})
    tasks.update(s, owner, task, {"assigned_to": other.name}, if_match=task.row_version)
    assert task.status == "defined" and task.assigned_to == other.id
    assert run.status == "running" and latest(s, run, 1).status == "active"


@pytest.mark.parametrize("review_mode", ["manual", "auto"])
def test_real_submit_merge_rework_creates_a_new_pull_request(graph, respx_mock, review_mode):
    from tests.gitea_fake import FakeGitea, GITEA, SERVICE_USER

    s, owner, agent, other, template, steps = graph
    fake = FakeGitea(respx_mock)
    repo = fake.add_repo(SERVICE_USER, "demo")
    settings_store.update(s, {"gitea_url": GITEA, "gitea_token": "test-service-token"}, owner.id)
    template.project.gitea_owner, template.project.gitea_repo = SERVICE_USER, "demo"
    steps[0].review_mode = review_mode
    run = wf.start(s, owner, template)
    task = latest(s, run, 1).task
    def submit_and_merge():
        sm.transition(s, task, "claim", agent)
        fake.push_branch(SERVICE_USER, "demo", task.branch_name)
        sm.transition(s, task, "submit", agent, {"branch_name": task.branch_name,
                                               "commit_sha": "a" * 40, "summary": "Report complete"})
        if review_mode == "manual":
            sm.transition(s, task, "approve", owner)
        assert task.status == "done" and task.merge_commit_sha == "c" * 40
    submit_and_merge()
    first_pr = task.pr_number
    sm.transition(s, latest(s, run, 2).task, "reject", owner, {"body": "Add sources"})
    assert task.pr_number is None and task.merge_commit_sha is None
    submit_and_merge()
    assert task.pr_number != first_pr
    assert len(repo.pulls) == 2 and all(pr["merged"] for pr in repo.pulls.values())
    sm.transition(s, latest(s, run, 2).task, "approve", owner)
    assert run.status == "succeeded"


def test_condition_skips_step_with_reason(graph):
    s, owner, agent, other, template, steps = graph
    s.add(WorkflowParam(version_id=steps[0].version_id, name="mode", param_type="string", label="Mode"))
    steps[0].condition_tpl = "{{params.mode}}"
    s.flush()
    run = wf.start(s, owner, template, params={"mode": "off"})
    skipped = latest(s, run, 1)
    assert skipped.status == "skipped" and skipped.error_code == "condition_false"
    assert skipped.error_detail == "{{params.mode}}" and skipped.task_id is None
    # The run advances past the skipped step to the approval.
    assert run.status == "waiting" and latest(s, run, 2).status == "waiting"
    assert latest(s, run, 2).task.task_kind == "approval"


def test_condition_true_runs_the_step(graph):
    s, owner, agent, other, template, steps = graph
    s.add(WorkflowParam(version_id=steps[0].version_id, name="mode", param_type="string", label="Mode"))
    steps[0].condition_tpl = "{{params.mode}}"
    s.flush()
    run = wf.start(s, owner, template, params={"mode": "anything"})
    assert latest(s, run, 1).status == "active" and latest(s, run, 1).task_id


def test_condition_unresolved_placeholder_fails_closed(graph):
    s, owner, agent, other, template, steps = graph
    steps[0].condition_tpl = "{{steps.missing.task_nr}}"
    run = wf.start(s, owner, template)
    assert run.status == "paused" and run.pause_reason == "placeholder_unresolved"
    assert latest(s, run, 1).status == "failed" and latest(s, run, 1).task_id is None


def test_parallel_group_completes_only_when_all_members_done(graph):
    s, owner, agent, other, template, steps = graph
    agent.max_parallel_tasks = 3
    steps[0].step_key, steps[0].parallel_group = "one", "g"
    steps[1].step_key, steps[1].step_type = "two", "agent"
    steps[1].assignee_id, steps[1].agent_role, steps[1].parallel_group = None, "writer", "g"
    steps[1].review_mode = "manual"
    steps[1].description_tpl, steps[1].acceptance_tpl = "Draft the second part", "Part two exists"
    s.flush()
    run = wf.start(s, owner, template)
    assert latest(s, run, 1).status == "active" and latest(s, run, 1).task_id
    assert latest(s, run, 2).status == "active" and latest(s, run, 2).task_id
    assert run.status == "running"
    # The notify after the group stays pending while the group is open.
    assert latest(s, run, 3).status == "pending"
    finish(s, owner, latest(s, run, 1).task)
    assert run.status == "running" and latest(s, run, 3).status == "pending"
    finish(s, owner, latest(s, run, 2).task)
    assert latest(s, run, 3).status == "succeeded" and run.status == "succeeded"


def test_parallel_group_member_failure_consistent_with_single_step(graph):
    s, owner, agent, other, template, steps = graph
    agent.max_parallel_tasks = 3
    steps[0].parallel_group = "g"
    steps[1].step_type = "agent"
    steps[1].assignee_id, steps[1].agent_role, steps[1].parallel_group = None, "writer", "g"
    steps[1].review_mode = "manual"
    steps[1].description_tpl, steps[1].acceptance_tpl = "Draft the second part", "Part two exists"
    s.flush()
    run = wf.start(s, owner, template)
    one, two = latest(s, run, 1), latest(s, run, 2)
    sm.transition(s, two.task, "claim", agent)
    sm.transition(s, two.task, "cancel", owner, {"body": "Cannot continue"})
    # A cancelled member pauses the run while the sibling task keeps running.
    assert run.status == "paused" and two.status == "failed"
    assert one.status == "active" and one.task.status == "defined"
