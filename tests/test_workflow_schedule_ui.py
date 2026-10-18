"""Human schedule editing: CSRF, permissions, nonmutating preview and error recovery."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.main import app
from app.models import WorkflowSchedule
from app.services import settings_store
from tests.conftest import login
from tests.test_workflow_engine import graph  # noqa: F401

pytestmark = pytest.mark.db


def test_editor_preview_save_invalid_and_csrf(graph):
    s, owner, agent, other, template, steps = graph
    settings_store.update(s, {"workflows_enabled": "true"}, owner.id)
    s.commit()
    path = "/workflows/DEMO/content/schedules"
    with TestClient(app) as client:
        assert login(client).status_code == 303
        client.cookies.set("tm_lang", "en")
        page = client.get(path)
        assert page.status_code == 200 and 'Europe/Berlin' in page.text
        csrf = page.text.split('name="csrf_token" value="')[1].split('"')[0]
        values = dict(name="Morning", kind="cron", cron_expr="30 2 * * *", timezone="Europe/Berlin",
                      enabled="true", action="preview")
        assert client.post(path, data=values).status_code == 403
        values["csrf_token"] = csrf
        response = client.post(path, data=values)
        assert response.status_code == 200 and '+0' in response.text
        s.commit()
        assert s.scalar(select(func.count()).select_from(WorkflowSchedule)) == 0
        values["action"] = "save"
        response = client.post(path, data=values, follow_redirects=False)
        assert response.status_code == 303, response.text
        s.commit()
        row = s.scalar(select(WorkflowSchedule))
        assert row.name == "Morning" and row.enabled
        assert client.get(response.headers['location']).status_code == 200
        assert client.get('/workflows/schedules').status_code == 200
        assert 'Morning' in client.get('/workflows/schedules').text
        values.update(schedule_id=row.id, timezone="Bad/Zone")
        response = client.post(path, data=values)
        assert response.status_code == 422 and 'Invalid workflow schedule' in response.text
        s.commit(); s.refresh(row)
        assert row.timezone == "Europe/Berlin"
        values.update(timezone="Europe/Berlin", schedule_id=999999)
        assert client.post(path, data=values).status_code == 404
        assert client.get('/admin/settings').status_code == 200
        values.pop("schedule_id")
        values.update(kind="workday", workday_mode="nth", workday_nth="3", workday_of="month",
                      workday_time="07:00", action="preview", timezone="Europe/Berlin")
        response = client.post(path, data=values)
        assert response.status_code == 200 and '07:00+' in response.text
        s.commit()
        assert s.scalar(select(func.count()).select_from(WorkflowSchedule)) == 1
        values.update(kind="cron", cron_expr="")
        assert client.post(path, data=values).status_code == 422


def test_owner_without_template_edit_privilege_can_manage_schedule(graph):
    s, owner, agent, other, template, steps = graph
    from app.services.workflows.schedules import may_edit
    assert may_edit(owner, template)
    assert not may_edit(agent, template)
    agent.role = "admin"
    assert not may_edit(agent, template)
