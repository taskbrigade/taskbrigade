"""Menu "Workflows" (WF 5): template list, editor with checks/publish/diff, run history, run detail, controls,
and run tasks on kanban, backlog, inbox and the task page (test DB; UI texts in German)."""
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.auth import hash_password
from app.main import app
from app.models import Actor, ProjectMember, Task, WorkflowRun, WorkflowTemplateVersion
from app.services import state_machine as sm
from app.services.workflows import ui
from tests.test_workflow_engine import graph  # noqa: F401

pytestmark = pytest.mark.db
PASSWORD = "viewer-test-password"


def _login(client, username, password):
    page = client.get("/login").text
    csrf = page.split('name="csrf_token" value="')[1].split('"')[0]
    r = client.post("/login", data={"username": username, "password": password, "csrf_token": csrf},
                    follow_redirects=False)
    assert r.status_code == 303, r.text
    client.csrf = client.get("/workflows").text.split('"X-CSRF-Token": "')[1].split('"')[0]
    return client


@pytest.fixture
def web(graph):  # noqa: F811
    s, owner, agent, other, template, steps = graph
    from tests.conftest import BOOTSTRAP_PW

    s.commit()
    with TestClient(app) as client:
        _login(client, "admin", BOOTSTRAP_PW)
        yield client, s, owner, agent, template, steps


def fresh(s):
    """New snapshot after the UI committed in its own session (expire_on_commit is off)."""
    s.commit()
    s.expire_all()


def first_task(s, run_id, position=1):
    """Task of a run step without locking (the engine's helpers lock rows the UI needs next)."""
    from app.models import WorkflowRunStep

    task = s.scalar(select(Task).join(WorkflowRunStep, WorkflowRunStep.task_id == Task.id).where(
        WorkflowRunStep.run_id == run_id, WorkflowRunStep.position == position))
    s.commit()
    return task


def post(client, url, data=None, **kw):
    return client.post(url, data={"csrf_token": client.csrf, **(data or {})}, **kw)


def editor_form(**overrides):
    """Form of the editor with the three steps of `graph` (write, approve, inform), `approve` moved last."""
    rows = [("write", "agent", "Write {{topic}}", "writer", ""), ("inform", "notify", "Report released", "", ""),
            ("approve", "approval", "Release report", "", "1")]
    data = {"name": "Content", "description": "", "change_note": "Reordered", "action": "save",
            "p_name": ["topic"], "p_type": ["string"], "p_required": ["1"], "p_default": ["Workflows"],
            "p_values": [""], "p_label": ["Topic"], "p_help": [""]}
    fields = {"key": 0, "type": 1, "title": 2, "role": 3}
    for name in ("key", "type", "title", "description", "acceptance", "assignee", "role", "review", "reviewer",
                 "data_class", "model_tier", "timeout", "retries", "optional", "outputs", "inputs", "config"):
        data[f"s_{name}"] = [r[fields[name]] if name in fields else "" for r in rows]
    data["s_review"] = ["manual"] * 3
    data["s_retries"] = ["0"] * 3
    data["s_acceptance"] = ["Report exists", "", ""]
    data["s_outputs"] = ['{"report": {"type": "text", "required": true}}', "", ""]
    data.update(overrides)
    return data


def _fix_approver(data, owner_id):
    data["s_assignee"] = ["", "", str(owner_id)]
    return data


# --- Template list, template page, editor -------------------------------------------------------------------------

def test_menu_list_and_template_page(web):
    client, s, owner, agent, template, steps = web
    page = client.get("/workflows").text
    assert 'href="/workflows" aria-current="page"' in page  # Menu entry and sub-navigation.
    assert "/workflows/DEMO/content" in page and "Content" in page and "v1" in page and "noch keine Läufe" in page
    assert "/workflows/new" in page
    detail = client.get("/workflows/DEMO/content").text
    assert "Write report" in detail and "Lauf starten" in detail and "/workflows/DEMO/content/edit" in detail
    assert client.get("/workflows?project=NOPE").status_code == 200
    assert client.get("/workflows/DEMO/missing").status_code == 404


def test_editor_checks_save_reorder_publish_and_diff(web):
    client, s, owner, agent, template, steps = web
    page = client.get("/workflows/DEMO/content/edit").text
    assert 'name="s_key" value="write"' in page and "Prüfungen" in page and "wf-step-template" in page
    assert page.index('value="write"') < page.index('value="approve"') < page.index('value="inform"')

    # Checks on the unsaved form: unknown placeholder and duplicate keys are reported, nothing is stored.
    bad = editor_form(s_title=["Write {{nope}}", "Report released", "Release report"])
    r = post(client, "/workflows/DEMO/content/check", _fix_approver(bad, owner.id), headers={"HX-Request": "true"})
    assert r.status_code == 200 and "Unbekannter Platzhalter" in r.text and "nope" in r.text
    r = post(client, "/workflows/DEMO/content/check", _fix_approver(editor_form(), owner.id))
    assert "Alle Prüfungen bestanden" in r.text, r.text
    fresh(s)
    assert s.scalar(select(func.count()).select_from(WorkflowTemplateVersion)) == 1

    # Invalid JSON keeps the input and shows the error.
    r = post(client, "/workflows/DEMO/content/edit", _fix_approver(editor_form(
        s_outputs=["{broken", "", ""]), owner.id))
    assert r.status_code == 422 and "kein gültiges JSON" in r.text and "{broken" in r.text

    # Save → draft v2 in the new order; the published v1 stays current.
    r = post(client, "/workflows/DEMO/content/edit", _fix_approver(editor_form(), owner.id))
    assert r.status_code == 200 and "Entwurf gespeichert" in r.text and "Entwurf v2" in r.text
    fresh(s)
    draft = ui.draft_of(s, template)
    assert [st.step_key for st in draft.steps] == ["write", "inform", "approve"]
    assert s.get(type(template), template.id).current_version.version == 1
    diff = client.get("/workflows/DEMO/content/diff").text
    assert "v1 → v2 (Entwurf)" in diff and "wf-diff-add" in diff and "Write {{topic}}" in diff

    # Publish → v2 is current; the diff of v2 against v1 is reachable from the template page.
    data = _fix_approver(editor_form(), owner.id)
    data["action"] = "publish"
    r = post(client, "/workflows/DEMO/content/edit", data)
    assert r.status_code == 200 and "Version 2 veröffentlicht" in r.text, r.text
    fresh(s)
    assert s.get(type(template), template.id).current_version.version == 2
    assert "/workflows/DEMO/content/diff?target=2" in r.text
    assert "wf-diff-del" in client.get("/workflows/DEMO/content/diff?target=2").text


def test_publish_failure_keeps_draft_and_discard(web):
    client, s, owner, agent, template, steps = web
    data = editor_form(s_role=["", "", ""])  # Agent step without assignee or role: blocks publishing.
    data["action"] = "publish"
    r = post(client, "/workflows/DEMO/content/edit", _fix_approver(data, owner.id))
    assert r.status_code == 422 and "nicht veröffentlicht" in r.text
    fresh(s)
    assert ui.draft_of(s, template) is not None
    r = post(client, "/workflows/DEMO/content/discard")
    assert r.status_code == 200 and "Entwurf verworfen" in r.text
    fresh(s)
    assert ui.draft_of(s, template) is None and template.current_version.version == 1


def test_new_workflow(web):
    client, s, owner, agent, template, steps = web
    assert 'value="DEMO"' in client.get("/workflows/new").text
    r = post(client, "/workflows/new", {"project": "DEMO", "key": "bad key", "name": "x"})
    assert r.status_code == 422
    r = post(client, "/workflows/new", {"project": "DEMO", "key": "content", "name": "x"})
    assert r.status_code == 422 and "existiert bereits" in r.text
    r = post(client, "/workflows/new", {"project": "DEMO", "key": "weekly", "name": "Weekly"})
    assert r.status_code == 200 and str(r.url).endswith("/workflows/DEMO/weekly/edit")
    assert "Mindestens einen Schritt" in r.text and "neuer Entwurf" not in r.text
    assert "/workflows/DEMO/weekly" in client.get("/workflows").text
    assert "nicht veröffentlicht" in client.get("/workflows").text


def test_viewer_reads_but_cannot_edit(web):
    client, s, owner, agent, template, steps = web
    viewer = Actor(name="viewer", display_name="Viewer", actor_type="human", role="member",
                   password_hash=hash_password(PASSWORD))
    s.add(viewer)
    s.flush()
    s.add(ProjectMember(project_id=template.project_id, actor_id=viewer.id, gitea_permission="read"))
    outsider = Actor(name="outsider", display_name="Out", actor_type="human", role="member",
                     password_hash=hash_password(PASSWORD))
    s.add(outsider)
    s.commit()
    with TestClient(app) as c:
        _login(c, "viewer", PASSWORD)
        assert "Write report" in c.get("/workflows/DEMO/content").text
        assert "/workflows/DEMO/content/edit" not in c.get("/workflows/DEMO/content").text
        assert c.get("/workflows/DEMO/content/edit").status_code == 403
        assert post(c, "/workflows/DEMO/content/check", editor_form()).status_code == 403
        assert c.get("/workflows/new").status_code == 403
    with TestClient(app) as c:
        _login(c, "outsider", PASSWORD)
        assert c.get("/workflows/DEMO/content").status_code == 403
        assert "DEMO/content" not in c.get("/workflows").text


# --- Runs -----------------------------------------------------------------------------------------------------------

def _start(client):
    r = post(client, "/workflows/DEMO/content/start", {})
    assert r.status_code == 200 and "Lauf gestartet" in r.text, r.text
    return int(re.search(r"/workflow-runs/(\d+)", str(r.url))[1]), r.text


def test_start_detail_history_and_controls(web):
    client, s, owner, agent, template, steps = web
    run_id, page = _start(client)
    fresh(s)
    first = first_task(s, run_id)
    assert "wf-chain" in page and first.task_nr in page and "write" in page and "approve" in page
    assert "Ausstehend" in page  # Later steps are pending.

    # Pause, resume, cancel from the run page.
    assert "Pausiert" in post(client, f"/workflow-runs/{run_id}/pause", {"reason": "Check"}).text
    r = post(client, f"/workflow-runs/{run_id}/resume", {})
    assert "Lauf fortgesetzt" in r.text
    r = post(client, f"/workflow-runs/{run_id}/resume", {})
    assert "banner error" in r.text  # Not paused.

    # Finish the first step: the approval task appears, history shows the pipeline cells.
    fresh(s)
    sm.transition_to(s, s.get(Task, first.id), "done", s.get(Actor, owner.id), override=True)
    fresh(s)
    history = client.get("/workflows/DEMO/content/runs").text
    assert f"/workflow-runs/{run_id}" in history and "wf-cell wf-succeeded" in history
    assert f'href="/tasks/{first.task_nr}"' in history and "wf-cell wf-active" in history
    assert "wf-cell" in client.get("/workflows/DEMO/content/runs?status=running").text
    assert "Keine Läufe" in client.get("/workflows/DEMO/content/runs?status=failed").text
    runs = client.get("/workflow-runs").text
    assert f"/workflow-runs/{run_id}" in runs and "1 von 3 Schritten" in runs

    r = post(client, f"/workflow-runs/{run_id}/cancel", {})
    assert "Lauf abgebrochen" in r.text
    fresh(s)
    assert s.get(WorkflowRun, run_id).status == "cancelled"


def test_run_tasks_on_kanban_backlog_inbox_and_task_page(web):
    client, s, owner, agent, template, steps = web
    run_id, _page = _start(client)
    fresh(s)
    first = first_task(s, run_id)
    nr = first.task_nr

    board = client.get("/kanban")
    assert nr not in board.text and "1 Workflow-Task ausgeblendet" in board.text
    shown = client.get("/kanban?workflow=all")
    assert nr in shown.text and "wf-badge" in shown.text and "content #1" in shown.text
    assert shown.cookies.get("tm_workflow") == "all"
    assert nr in client.get("/kanban").text  # Remembered in the cookie.
    assert nr in client.get(f"/kanban?workflow={template.id}").text
    assert nr not in client.get(f"/kanban?workflow={template.id + 1000}").text
    assert nr in client.get("/kanban?workflow=only").text
    assert nr not in client.get("/backlog?workflow=hide").text
    assert nr in client.get("/backlog?workflow=only").text
    client.cookies.set("tm_workflow", "bogus")
    assert nr not in client.get("/kanban").text  # Unknown cookie value → default "hide".

    task_page = client.get(f"/tasks/{nr}").text
    assert "Schritt 1/3" in task_page and f"/workflow-runs/{run_id}" in task_page and "this-task" in task_page

    # The approval step's task is an action card with "Approve" instead of "merge".
    sm.transition_to(s, s.get(Task, first.id), "done", s.get(Actor, owner.id), override=True)
    fresh(s)
    approval = s.scalar(select(Task).where(Task.task_kind == "approval"))
    inbox = client.get("/").text
    assert approval.task_nr in inbox and "Abnehmen</button>" in inbox
    r = client.post(f"/inbox/{approval.task_nr}/approve", headers={"X-CSRF-Token": client.csrf, "HX-Request": "true"})
    assert r.status_code == 200, r.text
    fresh(s)
    assert s.get(WorkflowRun, run_id).status == "succeeded"


def test_filter_and_group_helpers_without_runs(web):
    client, s, owner, agent, template, steps = web
    data = {"questions": [{"task_nr": "A", "workflow": {"run_id": 1}}, {"task_nr": "B", "workflow": None}],
            "for_review": [], "aborted": []}
    from app.web.tasks_ui import workflow_group

    workflow_group(data)
    assert [t["task_nr"] for t in data["workflow_questions"]] == ["A"]
    assert [t["task_nr"] for t in data["questions"]] == ["B"]
    assert ui.filter_options(s, s.get(Actor, owner.id))[0]["label"] == "DEMO/content"
    assert ui.diff(None, {"name": "X", "params": [], "steps": []})[-1] == {"kind": "add", "text": "name: X"}
