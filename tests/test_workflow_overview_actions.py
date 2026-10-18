"""Overview actions use the engine permissions and preserve references and run history."""
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth import hash_password
from app.main import app
from app.models import (Actor, ProjectMember, WorkflowParam, WorkflowRun, WorkflowSchedule,
                        WorkflowTemplate, WorkflowTrigger)
from app.services.workflows import api, ui
from tests.test_workflow_engine import graph  # noqa: F401
from tests.test_workflows_ui import PASSWORD, _login, fresh, post, web  # noqa: F401

pytestmark = pytest.mark.db
BASE = "/workflows/DEMO/content"


def test_rename_keeps_key_and_validates_name(web):
    client, s, owner, agent, template, steps = web
    for name in ("", "   ", "x" * 121):
        assert post(client, BASE + "/manage/rename", {"name": name}).status_code == 422
    assert post(client, BASE + "/manage/rename", {"name": "New display name"}).status_code == 200
    fresh(s)
    assert template.name == "New display name" and template.workflow_key == "content"
    assert template.current_version.steps[0].id == steps[0].id


def test_duplicate_latest_draft_losslessly_without_automation(web):
    client, s, owner, agent, template, steps = web
    from app.api.workflows import Draft, ParamDraft, StepDraft

    api.save_draft(s, owner, template.project, "content", Draft(name="Content", steps=[StepDraft(
        step_key="changed", step_type="notify", title_tpl="Changed draft", retry_backoff_s=0,
        condition_tpl="{{kind}}", config={"to": ["owner"], "text": "Hello"})], params=[ParamDraft(
            name="kind", param_type="enum", label="Kind", enum_values=["one,two", "three"], default_expr="three")]))
    s.add(WorkflowSchedule(template_id=template.id, name="Daily", kind="cron", cron_expr="0 0 * * *",
                           created_by=owner.id))
    s.add(WorkflowTrigger(template_id=template.id, kind="dataset_ready", filter_json="{}", created_by=owner.id))
    s.commit()
    for key in ("contentCopy", "contentCopy2"):
        r = post(client, BASE + "/manage/duplicate")
        assert r.status_code == 200 and str(r.url).endswith(f"/workflows/DEMO/{key}/edit")
    fresh(s)
    copy = s.scalar(select(WorkflowTemplate).where(WorkflowTemplate.workflow_key == "contentCopy"))
    assert copy.current_version_id is None and copy.project_id == template.project_id
    assert copy.name == "Kopie von Content"
    draft = ui.draft_of(s, copy)
    assert draft.steps[0].title_tpl == "Changed draft" and draft.steps[0].retry_backoff_s == 0
    assert draft.steps[0].condition_tpl == "{{kind}}"
    assert json.loads(draft.params[0].enum_values) == ["one,two", "three"]
    assert s.scalar(select(WorkflowSchedule).where(WorkflowSchedule.template_id == copy.id)) is None
    assert s.scalar(select(WorkflowTrigger).where(WorkflowTrigger.template_id == copy.id)) is None
    assert template.current_version.steps[0].title_tpl == "Write report"


def test_overview_start_budgets_refresh_and_archive_preserves_history(web):
    client, s, owner, agent, template, steps = web
    schedule = WorkflowSchedule(template_id=template.id, name="Daily", kind="cron", cron_expr="0 0 * * *",
                                created_by=owner.id)
    trigger = WorkflowTrigger(template_id=template.id, kind="dataset_ready", filter_json="{}", created_by=owner.id)
    s.add_all([schedule, trigger])
    s.commit()
    r = post(client, BASE + "/start", {"overview": "1", "budget_tokens": "5000", "budget_cents": "250"})
    assert r.status_code == 200 and "/workflows?started=" in str(r.url) and "Lauf gestartet" in r.text
    assert 'ui-chip-count">1<' in r.text and "Lauf ansehen" in r.text
    fresh(s)
    run = s.scalar(select(WorkflowRun))
    assert run.budget_tokens == 5000 and run.budget_cents == 250
    run_id = run.id
    s.commit()
    assert post(client, BASE + "/manage/delete").status_code == 409
    post(client, f"/workflow-runs/{run_id}/pause")
    assert post(client, BASE + "/manage/delete").status_code == 409
    post(client, f"/workflow-runs/{run_id}/cancel")
    assert "Workflow gelöscht" in post(client, BASE + "/manage/delete").text
    fresh(s)
    assert template.status == "archived" and not schedule.enabled and schedule.next_fire_at is None
    assert not trigger.enabled and s.get(WorkflowRun, run_id) is not None
    assert ui.tab_counts(s, owner) == {"templates": 0, "runs": 1, "schedules": 0, "triggers": 0}
    assert ui.filter_options(s, owner)[0]["label"] == "DEMO/content"
    s.commit()
    assert BASE not in client.get("/workflows").text
    assert BASE not in client.get("/workflows/schedules").text
    assert BASE not in client.get("/workflows/triggers").text
    assert client.get(f"/workflow-runs/{run_id}").status_code == 200
    assert f"/workflow-runs/{run_id}" in client.get("/workflow-runs").text
    r = post(client, BASE + "/start", follow_redirects=False)
    assert "err=" in r.headers["location"]


def test_required_parameter_dialog_and_unpublished_explanation(web):
    client, s, owner, agent, template, steps = web
    s.add(WorkflowParam(version_id=template.current_version_id, name="topic", param_type="string",
                        label="Topic", required=True))
    s.commit()
    page = client.get("/workflows").text
    assert 'data-wf-open="wf-' in page and 'name="param_topic"' in page and "Jetzt starten" in page
    r = post(client, BASE + "/start", {"overview": "1", "param_topic": "Supplied"})
    assert "Lauf gestartet" in r.text
    fresh(s)
    assert json.loads(s.scalar(select(WorkflowRun)).params_json) == {"topic": "Supplied"}
    s.commit()
    post(client, "/workflows/new", {"project": "DEMO", "key": "draft", "name": "Draft"})
    assert "Noch nicht veröffentlicht – erst veröffentlichen" in client.get("/workflows").text
    assert "err=" in post(client, "/workflows/DEMO/draft/start", follow_redirects=False).headers["location"]


def test_member_cannot_mutate_or_start_and_csrf_is_required(web):
    client, s, owner, agent, template, steps = web
    viewer = Actor(name="viewer", display_name="Viewer", actor_type="human", role="member",
                   password_hash=hash_password(PASSWORD))
    s.add(viewer)
    s.flush()
    s.add(ProjectMember(project_id=template.project_id, actor_id=viewer.id, gitea_permission="read"))
    s.commit()
    assert client.post(BASE + "/manage/delete").status_code == 403
    with TestClient(app) as c:
        _login(c, "viewer", PASSWORD)
        page = c.get("/workflows").text
        assert "/manage/rename" not in page and "/manage/duplicate" not in page and "/start" not in page
        for action in ("rename", "duplicate", "delete"):
            assert post(c, BASE + "/manage/" + action, {"name": "Forbidden"}).status_code == 403
        assert "err=" in post(c, BASE + "/start", follow_redirects=False).headers["location"]


@pytest.mark.parametrize("field,value", [("budget_tokens", "-1"), ("budget_cents", "4294967296"),
                                         ("budget_tokens", "oops")])
def test_invalid_budgets_do_not_start(web, field, value):
    client, s, owner, agent, template, steps = web
    assert "err=" in post(client, BASE + "/start", {field: value}, follow_redirects=False).headers["location"]
    fresh(s)
    assert s.scalar(select(WorkflowRun)) is None
