"""Template-scoped vs global workflow tabs: on a template's pages the tabs point at the template's own
Runs/Schedules/Event-triggers routes with a link back to the global list; global pages keep the global tabs."""
import pytest
from tests.test_workflow_engine import graph  # noqa: F401
from tests.test_workflows_ui import web  # noqa: F401

pytestmark = pytest.mark.db


def test_template_pages_use_template_scoped_tabs(web):
    client, s, owner, agent, template, steps = web
    detail = client.get("/workflows/DEMO/content").text
    assert 'href="/workflows/DEMO/content" aria-current="page"' in detail  # Overview active
    assert 'href="/workflows/DEMO/content/runs"' in detail
    assert 'href="/workflows/DEMO/content/schedules"' in detail
    assert 'href="/workflows/DEMO/content/triggers"' in detail
    assert 'href="/workflows"' in detail  # breadcrumb back to the global list
    assert 'href="/workflow-runs"' not in detail  # no global tabs

    assert 'href="/workflows/DEMO/content/runs" aria-current="page"' in client.get("/workflows/DEMO/content/runs").text
    assert 'href="/workflows/DEMO/content/schedules" aria-current="page"' in client.get("/workflows/DEMO/content/schedules").text
    assert 'href="/workflows/DEMO/content/triggers" aria-current="page"' in client.get("/workflows/DEMO/content/triggers").text


def test_editor_and_diff_use_template_scoped_tabs(web):
    client, s, owner, agent, template, steps = web
    edit = client.get("/workflows/DEMO/content/edit").text
    assert 'href="/workflows/DEMO/content" aria-current="page"' in edit
    assert 'href="/workflows/DEMO/content/runs"' in edit
    diff = client.get("/workflows/DEMO/content/diff").text
    assert 'href="/workflows/DEMO/content"' in diff
    assert 'href="/workflows/DEMO/content/schedules"' in diff


def test_global_pages_keep_global_tabs(web):
    client, s, owner, agent, template, steps = web
    assert 'href="/workflows" aria-current="page"' in client.get("/workflows").text
    assert 'href="/workflow-runs"' in client.get("/workflows").text
    assert 'href="/workflows/schedules"' in client.get("/workflows").text
    assert 'href="/workflows/triggers"' in client.get("/workflows").text

    assert 'href="/workflow-runs" aria-current="page"' in client.get("/workflow-runs").text
    assert 'href="/workflows/schedules" aria-current="page"' in client.get("/workflows/schedules").text
    assert 'href="/workflows/triggers" aria-current="page"' in client.get("/workflows/triggers").text
