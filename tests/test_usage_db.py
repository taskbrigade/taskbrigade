"""Usage tracking and workflow-run budget enforcement against MariaDB (research/konzept-workflows.md §7.4)."""
import pytest
from sqlalchemy import func, select

from app.errors import Conflict
from app.models import Actor, Notification, Project, ProjectMember, TaskUsage, TelegramLink, WorkflowRoleBinding, \
    WorkflowStep, WorkflowTemplate, WorkflowTemplateVersion
from app.services import settings_store, state_machine as sm, usage
from app.services.workflows import engine as wf

pytestmark = pytest.mark.db


@pytest.fixture
def graph(ready_db):
    with ready_db.session() as s:
        owner = s.scalar(select(Actor).where(Actor.name == "admin"))
        agent = s.scalar(select(Actor).where(Actor.name == "codex"))
        project = Project(project_key="DEMO", name="Demo", status="active", privacy="cloud_ok",
                          owner_id=owner.id, created_by=owner.id)
        s.add(project)
        s.flush()
        s.add(ProjectMember(project_id=project.id, actor_id=owner.id, gitea_permission="write"))
        s.add(ProjectMember(project_id=project.id, actor_id=agent.id, gitea_permission="write"))
        template = WorkflowTemplate(project_id=project.id, workflow_key="content", name="Content",
                                    owner_id=owner.id, created_by=owner.id, default_budget_tokens=1000)
        s.add(template)
        s.flush()
        version = WorkflowTemplateVersion(template_id=template.id, version=1, state="published", created_by=owner.id)
        s.add(version)
        s.flush()
        template.current_version = version
        steps = [
            WorkflowStep(version_id=version.id, position=1, step_key="write", step_type="agent",
                         title_tpl="Write", description_tpl="Create it", acceptance_tpl="It exists",
                         agent_role="writer", review_mode="manual"),
            WorkflowStep(version_id=version.id, position=2, step_key="edit", step_type="agent",
                         title_tpl="Edit", description_tpl="Edit it", acceptance_tpl="It is edited",
                         agent_role="writer", review_mode="manual"),
        ]
        s.add_all(steps)
        s.add(WorkflowRoleBinding(project_id=project.id, role="writer", actor_id=agent.id, rank=1))
        s.flush()
        yield s, owner, agent, template, steps


def test_usage_record_counts_towards_budget_and_enforces(graph):
    s, owner, agent, template, steps = graph
    settings_store.update(s, {"telegram_enabled": "true", "telegram_bot_token": "123456:test-token"}, owner.id)
    s.add(TelegramLink(actor_id=owner.id, chat_id=12345))
    s.flush()
    run = wf.start(s, owner, template, budget_tokens=100)
    task = wf._latest(s, run)[1].task
    assert run.used_tokens == 0
    # 50 tokens: below 80 % -> no notice, no pause.
    run2, before = usage.record(s, agent, task, {"input_tokens": 40, "output_tokens": 10})
    assert run2.used_tokens == 50 and before == "ok"
    usage.enforce(s, run2, before)
    assert run2.status == "running"
    # 35 more -> 85 tokens, crosses 80 % -> warning notice, still running.
    run2, before = usage.record(s, agent, task, {"input_tokens": 35})
    usage.enforce(s, run2, before)
    assert before == "ok" and run2.status == "running"
    assert s.scalar(select(func.count()).select_from(Notification).where(
        Notification.event == "workflow_budget_warning")) == 1
    # 20 more -> 105 tokens, exceeds 100 -> paused with budget_exceeded.
    run2, before = usage.record(s, agent, task, {"input_tokens": 20})
    usage.enforce(s, run2, before)
    assert run2.status == "paused" and run2.pause_reason == "budget_exceeded" and run2.used_tokens == 105
    assert s.scalar(select(func.count()).select_from(TaskUsage)) == 3


def test_resume_after_budget_exceeded_requires_higher_budget(graph):
    s, owner, agent, template, steps = graph
    run = wf.start(s, owner, template, budget_tokens=100, budget_cents=50)
    task = wf._latest(s, run)[1].task
    run2, before = usage.record(s, agent, task, {"input_tokens": 120, "cost_cents": 60})
    usage.enforce(s, run2, before)
    assert run2.status == "paused" and run2.pause_reason == "budget_exceeded"
    with pytest.raises(Conflict):
        wf.resume(s, owner, run2)
    with pytest.raises(Conflict):  # Tokens raised, but cents still at/over the limit.
        wf.resume(s, owner, run2, budget_tokens=200)
    wf.resume(s, owner, run2, budget_tokens=200, budget_cents=100)
    assert run2.status == "running" and run2.budget_tokens == 200 and run2.budget_cents == 100


def test_activation_stops_at_budget_exceeded(graph):
    s, owner, agent, template, steps = graph
    run = wf.start(s, owner, template, budget_tokens=50)
    task = wf._latest(s, run)[1].task
    # Exceed the budget without the enforcement pause (e.g. the budget was lowered after the report): the next
    # agent step must not activate.
    run.used_tokens = 60
    sm.transition_to(s, task, "done", owner, override=True)
    assert run.status == "paused" and run.pause_reason == "budget_exceeded"
    assert wf._latest(s, run)[2].status == "failed" and wf._latest(s, run)[2].error_code == "budget_exceeded"
