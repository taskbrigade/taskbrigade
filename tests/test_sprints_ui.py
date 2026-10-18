"""Sprints in the web UI: project page, sprint page (release preview, close dialog, batch creation), task forms
and the sprint filter in backlog, kanban and inbox (test DB + fake Gitea; UI texts in German)."""
import re

import pytest

from app.web.sprints_ui import parse_batch
from tests.test_ui_cli import post, ui  # noqa: F401 (Fixture)
from tests.test_workflow import env, gitea, mkproject, mktask  # noqa: F401 (Fixtures)


def api_task(env, nr):  # noqa: F811
    return env["c"].get(f"/api/v1/tasks/{nr}", headers=env["admin"]).json()


def api_sprint(env, sid, key="KET"):  # noqa: F811
    return env["c"].get(f"/api/v1/projects/{key}/sprints/{sid}", headers=env["admin"]).json()


def mksprint(ui, name="Sprint 1", key="KET", **kw):  # noqa: F811
    r = post(ui, f"/projects/{key}/sprints/new", name=name, **kw)
    assert r.status_code == 200 and "Sprint angelegt" in r.text, r.text
    return int(re.search(rf"/projects/{key}/sprints/(\d+)", str(r.url))[1])


# --- Batch parser (no DB) ------------------------------------------------------------------------

def test_parse_batch():
    rows = parse_batch("Login bauen\n\n  Logout | hermes | 2 \nDoku | | 4\n| hermes\nX | nobody\nY | | 9\na|b|1|c|d\n"
                       "Review | | | hermes\nZ | | | none\nW | | | wer", {"hermes"})
    assert [(r["n"], r["title"], r["assigned_to"], r["prio"]) for r in rows[:3]] == [
        (1, "Login bauen", None, 3), (2, "Logout", "hermes", 2), (3, "Doku", None, 4)]
    assert [r["error"] is None for r in rows] == [True, True, True, False, False, False, False, True, True, False]
    assert "Titel fehlt" in rows[3]["error"] and "nobody" in rows[4]["error"] and "1 und 5" in rows[5]["error"]
    assert "Zu viele Felder" in rows[6]["error"] and "wer" in rows[9]["error"]
    assert [r["reviewer"] for r in rows[7:9]] == ["hermes", "none"] and rows[0]["reviewer"] is None
    assert parse_batch(" \n\n", set()) == []


# --- Full flow ----------------------------------------------------------------------------------------


@pytest.mark.db
def test_sprint_flow_in_ui(env, ui):  # noqa: F811
    """Create a sprint, batch-capture tasks, complete one, release with preview, close with move/detach."""
    mkproject(env)
    page = ui.get("/projects/KET").text
    assert "Sprints" in page and "Noch keine Sprints." in page and 'action="/projects/KET/sprints/new"' not in page
    assert '<a href="/planning?project=KET" class="headline-link">In der Planung öffnen</a>' in page
    sid = mksprint(ui, goal="Login fertig", start_date="2026-10-12", end_date="2026-10-23")
    sid2 = mksprint(ui, name="Sprint 2")
    page = ui.get(f"/projects/KET/sprints/{sid}").text
    assert "Sprint 1" in page and "Login fertig" in page and "2026-10-12" in page and "Geplant" in page
    assert "bleibt Entwurf, bis der Sprint freigegeben ist" in page
    assert 'href="/planning?project=KET">Planungsboard</a>' in page
    listing = ui.get("/projects/KET").text.split('id="sprints"')[1].split("</article>")[0]
    assert f'href="/projects/KET/sprints/{sid}">Sprint 1</a>' in listing and "/sprints/" + str(sid2) in listing
    assert 'class="badge sprint-planned"' in listing and "noch keine Tasks" in listing and "<table" not in listing

    # Batch: the preview marks invalid lines and offers no create button; nothing is created.
    lines = "Login bauen | hermes | 2\nLogout\nDoku | niemand | 9"
    r = post(ui, f"/projects/KET/sprints/{sid}/batch", lines=lines, step="preview")
    assert r.status_code == 200 and "Unbekannter Akteur &#39;niemand&#39;" in r.text
    assert 'value="create"' not in r.text and "Doku | niemand | 9" in r.text
    r = post(ui, f"/projects/KET/sprints/{sid}/batch", lines=lines, step="create")
    assert r.status_code == 422 and "Zuerst die markierten Zeilen korrigieren" in r.text
    assert api_sprint(env, sid)["tasks"] == []
    lines = "Login bauen | hermes | 2\nLogout\nDoku | | 4"
    r = post(ui, f"/projects/KET/sprints/{sid}/batch", lines=lines, step="preview")
    assert "3 Entwürfe anlegen" in r.text
    r = post(ui, f"/projects/KET/sprints/{sid}/batch", lines=lines, step="create")
    assert r.status_code == 200 and "3 Entwürfe angelegt" in r.text
    tasks = api_sprint(env, sid)["tasks"]
    assert [(t["title"], t["status"], t["assigned_to"], t["prio"]) for t in tasks] == [
        ("Login bauen", "draft", "hermes", 2), ("Logout", "draft", None, 3), ("Doku", "draft", None, 4)]
    login, logout, doku = (t["task_nr"] for t in tasks)

    # Complete one task individually on its task page; the sprint field is kept.
    t = api_task(env, login)
    r = post(ui, f"/tasks/{login}/edit", row_version=t["row_version"], title="Login bauen", prio=2,
             assigned_to="hermes", description="Formular", acceptance_criteria="- geht", sprint=str(sid))
    assert r.status_code == 200 and api_task(env, login)["sprint"] == sid
    page = ui.get(f"/tasks/{login}").text
    assert f'href="/projects/KET/sprints/{sid}"' in page and "Teil des geplanten Sprints" in page

    # Release preview: one task will be released, two stay drafts with their missing fields.
    page = ui.get(f"/projects/KET/sprints/{sid}").text
    release = page.split('id="release"')[1].split("</article>")[0]
    assert "Werden freigegeben (1)" in release and login in release
    assert "Bleiben Entwurf (2)" in release and "fehlt: Beschreibung, Akzeptanzkriterien" in release
    assert "Sprint und 1 Task freigeben?" in release
    r = post(ui, f"/projects/KET/sprints/{sid}/release")
    assert r.status_code == 200 and "Sprint freigegeben: 1 Task freigegeben" in r.text
    assert "2 bleiben Entwurf" in r.text
    assert [api_task(env, nr)["status"] for nr in (login, logout, doku)] == ["defined", "draft", "draft"]
    assert api_sprint(env, sid)["status"] == "released"

    # Close: move to another sprint without choosing one is refused; then move the open tasks.
    page = ui.get(f"/projects/KET/sprints/{sid}").text
    assert "3 offene Tasks:" in page and f'<option value="{sid2}">Sprint 2 (Geplant)</option>' in page
    r = post(ui, f"/projects/KET/sprints/{sid}/close", open_tasks="move", move_to="")
    assert r.status_code == 422 and "Sprint wählen" in r.text
    r = post(ui, f"/projects/KET/sprints/{sid}/close", open_tasks="move", move_to=str(sid2))
    assert r.status_code == 200 and "3 offene Tasks nach &#39;Sprint 2&#39; verschoben" in r.text
    assert {api_task(env, nr)["sprint"] for nr in (login, logout, doku)} == {sid2}
    assert api_task(env, login)["status"] == "draft"  # Defined task moved into a planned sprint.
    page = ui.get(f"/projects/KET/sprints/{sid}").text
    assert "Abgeschlossen" in page and 'id="batch"' not in page and 'id="release"' not in page


@pytest.mark.db
def test_close_detaches_open_tasks(env, ui):  # noqa: F811
    mkproject(env)
    sid = mksprint(ui)
    post(ui, f"/projects/KET/sprints/{sid}/batch", lines="Eins\nZwei", step="create")
    post(ui, f"/projects/KET/sprints/{sid}/release")
    page = ui.get(f"/projects/KET/sprints/{sid}").text
    assert 'value="move" disabled' in page  # No other open sprint to move to.
    r = post(ui, f"/projects/KET/sprints/{sid}/close", open_tasks="detach")
    assert "2 offene Tasks aus dem Sprint gelöst" in r.text
    assert all(t["sprint"] is None for t in env["c"].get("/api/v1/tasks?project=KET", headers=env["admin"])
               .json()["items"])


@pytest.mark.db
def test_task_forms_choose_sprint(env, ui):  # noqa: F811
    mkproject(env)
    mkproject(env, key="OTH")
    sid = mksprint(ui)
    other = mksprint(ui, key="OTH", name="Fremd")
    form = ui.get(f"/tasks/new?project=KET&sprint={sid}").text
    assert f'<option value="{sid}" selected>Sprint 1 (Geplant)</option>' in form
    assert 'data-project="OTH"' in form and "bleiben Entwürfe, bis der Sprint freigegeben ist" in form
    r = post(ui, "/tasks/new", project="KET", title="Neu", description="d", acceptance_criteria="a",
             status="defined", sprint=str(sid))
    assert r.status_code == 200 and "der Task wurde als Entwurf angelegt" in r.text
    nr = re.search(r"/tasks/(KET-\d+)", str(r.url))[1]
    assert api_task(env, nr)["status"] == "draft" and api_task(env, nr)["sprint"] == sid
    # A sprint of another project is refused.
    r = post(ui, "/tasks/new", project="KET", title="Falsch", sprint=str(other))
    assert r.status_code == 422 and "gehört zu einem anderen Projekt" in r.text

    # Edit form: only sprints of the task's project; detach and re-assign.
    page = ui.get(f"/tasks/{nr}").text
    edit = page.split('action="/tasks/' + nr + '/edit"')[1].split("</form>")[0]
    sprint_select = edit.split('name="sprint"')[1].split("</select>")[0]
    assert f'<option value="{sid}" selected>' in sprint_select and f'value="{other}"' not in sprint_select
    t = api_task(env, nr)
    post(ui, f"/tasks/{nr}/edit", row_version=t["row_version"], title="Neu", prio=3, sprint="none")
    assert api_task(env, nr)["sprint"] is None
    t = api_task(env, nr)
    post(ui, f"/tasks/{nr}/edit", row_version=t["row_version"], title="Neu", prio=3)  # Field missing: kept.
    assert api_task(env, nr)["sprint"] is None


@pytest.mark.db
def test_sprint_filter_in_backlog_kanban_inbox(env, ui):  # noqa: F811
    mkproject(env)
    sid = mksprint(ui, name="Sprint X")
    post(ui, f"/projects/KET/sprints/{sid}/batch", lines="Im Sprint", step="create")
    in_sprint = env["c"].get(f"/api/v1/tasks?sprint={sid}", headers=env["admin"]).json()["items"][0]["task_nr"]
    loose = mktask(env, title="Ohne Sprint")["task_nr"]  # Defined, not assigned → inbox notice.
    post(ui, f"/projects/KET/sprints/{sid}/release")
    t = api_task(env, in_sprint)
    post(ui, f"/tasks/{in_sprint}/edit", row_version=t["row_version"], title="Im Sprint", prio=3,
         description="d", acceptance_criteria="a", sprint=str(sid), then_publish="1")
    assert api_task(env, in_sprint)["status"] == "defined"

    def shows(url):
        html = ui.get(url).text
        return in_sprint in html, loose in html

    assert shows("/backlog?owner=all") == (True, True)
    assert shows(f"/backlog?owner=all&sprint={sid}") == (True, False)
    assert shows("/backlog?owner=all&sprint=none") == (False, True)
    assert shows(f"/kanban?owner=all&sprint={sid}") == (True, False)
    assert shows("/kanban?owner=all&sprint=none") == (False, True)
    assert shows(f"/?owner=all&sprint={sid}") == (True, False)
    assert shows("/?owner=all&sprint=none") == (False, True)
    assert shows("/?owner=all&sprint=bogus") == (True, True)  # Invalid value = no filter.
    backlog = ui.get(f"/backlog?owner=all&sprint={sid}").text
    assert f'<option value="{sid}" selected>Sprint X</option>' in backlog
    assert f'<a href="/projects/KET/sprints/{sid}">Sprint X</a>' in backlog  # Sprint column.
    assert "◷ Sprint X" in ui.get("/kanban?owner=all").text and "◷ Sprint X" in ui.get("/?owner=all").text


@pytest.mark.db
def test_reopen_closed_sprint_in_ui(env, ui):  # noqa: F811
    """The sprint page shows the reopen button only on a closed sprint; the checkbox restores the moved tasks."""
    mkproject(env)
    sid = mksprint(ui)
    post(ui, f"/projects/KET/sprints/{sid}/batch", lines="Eins\nZwei", step="create")
    nrs = [t["task_nr"] for t in api_sprint(env, sid)["tasks"]]
    post(ui, f"/projects/KET/sprints/{sid}/release")
    post(ui, f"/projects/KET/sprints/{sid}/close", open_tasks="detach")
    page = ui.get(f"/projects/KET/sprints/{sid}").text
    assert 'id="reopen"' in page and "Sprint wieder öffnen" in page and 'id="close"' not in page
    assert 'value="1" checked' in page  # "Move tasks back" is checked by default.
    assert 'name="restore_tasks" value="1" checked' in page
    # Confirmation text (tojson escapes non-ASCII): the count and the wording are there.
    assert 'wieder \\u00f6ffnen? 2 beim Abschlie\\u00dfen herausverschobene Tasks' in page

    r = post(ui, f"/projects/KET/sprints/{sid}/reopen", restore_tasks="1")
    assert r.status_code == 200 and "Sprint wieder geöffnet: 2 Tasks zurückgeholt" in r.text
    assert api_sprint(env, sid)["status"] == "released" and api_sprint(env, sid)["closed_at"] is None
    assert {api_task(env, nr)["sprint"] for nr in nrs} == {sid}
    page = ui.get(f"/projects/KET/sprints/{sid}").text
    assert 'id="reopen"' not in page and 'id="close"' in page


@pytest.mark.db
def test_reopen_without_moving_tasks_back(env, ui):  # noqa: F811
    mkproject(env)
    sid = mksprint(ui)
    post(ui, f"/projects/KET/sprints/{sid}/batch", lines="Eins", step="create")
    post(ui, f"/projects/KET/sprints/{sid}/release")
    post(ui, f"/projects/KET/sprints/{sid}/close", open_tasks="detach")
    r = post(ui, f"/projects/KET/sprints/{sid}/reopen")  # Checkbox off: no restore_tasks field.
    assert r.status_code == 200 and "Sprint wieder geöffnet" in r.text
    assert api_sprint(env, sid)["status"] == "released"
    assert all(t["sprint"] is None for t in env["c"].get("/api/v1/tasks?project=KET", headers=env["admin"])
               .json()["items"])


@pytest.mark.db
def test_reopen_of_open_sprint_shows_error(env, ui):  # noqa: F811
    mkproject(env)
    sid = mksprint(ui)
    r = post(ui, f"/projects/KET/sprints/{sid}/reopen")
    assert r.status_code == 422 and "Nur abgeschlossene Sprints" in r.text
