"""Workflows page redesign (UI 10): start templates are valid definitions; tabs with counts, empty state, template
cards and the prefilled new-workflow form (the DB tests need a test database; UI texts in German)."""
import pytest
from sqlalchemy import select

from app.api.workflows import Draft
from app.models import WorkflowTemplate
from app.services.workflows import starters
from app.services.workflows.publish import blocking, check_publish
from tests.test_workflow_engine import graph  # noqa: F401
from tests.test_workflows_ui import post, web  # noqa: F401


@pytest.mark.parametrize("starter_id", list(starters.STARTERS))
def test_starter_is_a_publishable_definition(starter_id):
    starter = starters.STARTERS[starter_id]
    body = Draft(**starters.draft_body(starter))
    assert body.steps and len({s.step_key for s in body.steps}) == len(body.steps)
    definition = {"key": starter["key"], "name": "x", "version": None, "project_data_class": "cloud_ok",
                  "params": [], "steps": [{**s.model_dump(), "position": i} for i, s in enumerate(body.steps, 1)]}
    assert not blocking(check_publish(definition, data_enabled=False))


def test_draft_body_copies_steps():
    starter = starters.STARTERS["chain"]
    body = starters.draft_body(starter, "Mine")
    body["steps"][0]["title_tpl"] = "changed"
    assert starter["steps"][0]["title_tpl"] != "changed" and body["name"] == "Mine"
    assert starters.get("nope") is None and starters.get("") is None


@pytest.mark.db
def test_list_page_tabs_search_and_template_cards(web):  # noqa: F811
    client, s, owner, agent, template, steps = web
    page = client.get("/workflows").text
    assert 'class="ui-tabs wf-tabs"' in page and 'ui-chip-count">1<' in page
    assert "/workflows/new?starter=chain" in page and "/workflows/new?starter=schedule" in page
    assert "/workflows/new?starter=event" in page and "Noch keine Workflows" not in page
    assert "wf-run-succeeded" in page  # legend
    assert "/workflows/DEMO/content" in client.get("/workflows?q=CONT").text
    hit = client.get("/workflows?q=zzz").text
    assert "/workflows/DEMO/content" not in hit and "Keine Workflows passen" in hit
    assert 'ui-chip-count' in client.get("/workflow-runs").text


def test_empty_state_and_filtered_empty_state():
    from types import SimpleNamespace

    from starlette.requests import Request

    from app.i18n import use_language
    from app.web.common import templates

    request = Request({"type": "http", "method": "GET", "path": "/workflows", "query_string": b"", "headers": [],
                       "app": SimpleNamespace(state=SimpleNamespace())})
    base = {"request": request, "rows": [], "project": "", "projects": [], "can_create": True, "q": "",
            "filtered": False, "starters": starters.STARTERS, "csrf": "x", "user": None, "static_v": "1",
            "wf_counts": {"templates": 0, "runs": 0, "schedules": 0, "triggers": 0}}
    template = templates.get_template("workflows.html")
    with use_language("de"):
        empty = template.render(base)
        filtered = template.render({**base, "q": "zzz", "filtered": True})
        read_only = template.render({**base, "can_create": False})
    assert "Noch keine Workflows" in empty and "wf-empty" in empty and "Mit einer Vorlage starten" in empty
    assert empty.count("Vorlage verwenden") == 3 and "Aufgabenkette" in empty
    assert "wf-empty" not in filtered and "Keine Workflows passen" in filtered
    assert "Vorlage verwenden" not in read_only and "/workflows/new" not in read_only


@pytest.mark.db
@pytest.mark.parametrize("starter_id", list(starters.STARTERS))
def test_template_prefills_form_and_creates_a_publishable_workflow(web, starter_id):  # noqa: F811
    client, s, owner, agent, template, steps = web
    starter = starters.STARTERS[starter_id]
    form = client.get(f"/workflows/new?starter={starter_id}&project=DEMO").text
    assert f'value="{starter["key"]}"' in form and f'name="starter" value="{starter_id}"' in form
    assert 'name="starter"' not in client.get("/workflows/new").text
    r = post(client, "/workflows/new", {"project": "DEMO", "key": starter["key"], "name": "Aus Vorlage",
                                        "starter": starter_id})
    assert r.status_code == 200 and str(r.url).split("?")[0].endswith(f"/workflows/DEMO/{starter['key']}/edit")
    s.commit()
    s.expire_all()
    created = s.scalar(select(WorkflowTemplate).where(WorkflowTemplate.workflow_key == starter["key"]))
    from app.services.workflows import api, ui

    assert created.name == "Aus Vorlage"
    draft = ui.draft_of(s, created)
    assert [st.step_key for st in draft.steps] == [st["step_key"] for st in starter["steps"]]
    version, _warnings = api.publish(s, owner, created)
    assert version.state == "published"


@pytest.mark.db
def test_unknown_starter_creates_an_empty_draft(web):  # noqa: F811
    client, s, owner, agent, template, steps = web
    assert client.get("/workflows/new?starter=nope").status_code == 200
    r = post(client, "/workflows/new", {"project": "DEMO", "key": "plain", "name": "Plain", "starter": "nope"})
    assert r.status_code == 200 and "Mindestens einen Schritt" in r.text
