"""Sprint editor: backlog entry, create/edit a planned sprint with its drafts in one transaction, save and release,
read-only tasks past draft and rights (test DB + fake Gitea; UI texts in German)."""
import json
import re

import pytest
from fastapi.testclient import TestClient

from app.errors import AppError
from app.main import app
from app.web.sprint_editor import MAX_ENTRIES, parse_entries, parse_removed
from tests.test_owner import human
from tests.test_ui_cli import post, ui  # noqa: F401 (Fixture)
from tests.test_workflow import claim, env, gitea, mkproject, mktask  # noqa: F401 (Fixtures)


def entry(title, nr=None, version=None, **kw):
    return {"task_nr": nr, "row_version": version, "status": "draft", "title": title, "description": "",
            "acceptance_criteria": "", "assigned_to": "", "prio": 3, "due_date": "", **kw}


def save(ui, url, entries, removed=(), action="save", **form):  # noqa: F811
    return post(ui, url, entries=json.dumps(entries), removed=json.dumps(list(removed)), action=action,
                **{"name": "Sprint 1", "goal": "", "start_date": "", "end_date": "", **form})


def sprint_tasks(env, sid, key="KET"):  # noqa: F811
    return env["c"].get(f"/api/v1/projects/{key}/sprints/{sid}", headers=env["admin"]).json()["tasks"]


def task(env, nr):  # noqa: F811
    return env["c"].get(f"/api/v1/tasks/{nr}", headers=env["admin"]).json()


def sprints_of(env, key="KET"):  # noqa: F811
    return env["c"].get(f"/api/v1/projects/{key}/sprints", headers=env["admin"]).json()


def login_as(name, password):
    c = TestClient(app)
    c.__enter__()
    csrf = c.get("/login").text.split('name="csrf_token" value="')[1].split('"')[0]
    assert c.post("/login", data={"username": name, "password": password, "csrf_token": csrf},
                  follow_redirects=False).status_code == 303
    c.csrf = c.get("/backlog").text.split('"X-CSRF-Token": "')[1].split('"')[0]
    return c


# --- Parsing (no DB) ----------------------------------------------------------------------------------

def test_parse_entries():
    rows = parse_entries(json.dumps([{"title": " A ", "prio": "2", "row_version": 4, "task_nr": "KET-0001"},
                                     {"title": "B", "prio": "x", "assigned_to": None, "status": "in_progress"}]))
    assert rows[0] == {
        "task_nr": "KET-0001", "row_version": 4, "status": "draft", "title": " A ", "description": "",
        "acceptance_criteria": "", "assigned_to": "", "prio": 2, "due_date": ""}
    assert (rows[1]["prio"], rows[1]["assigned_to"], rows[1]["task_nr"], rows[1]["status"]) == (
        3, "", None, "in_progress")
    assert parse_entries("") == [] and parse_removed("") == [] and parse_removed('["KET-0001", ""]') == ["KET-0001"]
    for bad in ("{", '{"a": 1}', "[1]", json.dumps([{}] * (MAX_ENTRIES + 1))):
        with pytest.raises(AppError):
            parse_entries(bad)
    with pytest.raises(AppError):
        parse_removed('"x"')


# --- Full flow --------------------------------------------------------------------------------------------


@pytest.mark.db
def test_editor_create_edit_and_release(env, ui):  # noqa: F811
    mkproject(env)
    backlog = ui.get("/backlog?owner=all").text
    assert 'href="/sprints/new"' in backlog
    assert backlog.index("Neuer Sprint") < backlog.index("Neuer Task")  # Left of "New task".
    assert 'href="/sprints/new?project=KET"' in ui.get("/backlog?owner=all&project=KET").text

    page = ui.get("/sprints/new?project=KET").text
    assert '<option value="KET" selected>' in page and 'data-guard="sprint"' in page
    assert "Speichern und Sprint freigeben" in page and "Prio aus Reihenfolge" in page
    assert re.search(r'id="se-release"\s+onclick', page)  # Admin may release: button not hidden.

    # An invalid entry rolls back everything: no sprint, no drafts.
    entries = [entry("Login", description="Formular", acceptance_criteria="- geht", assigned_to="hermes", prio=2),
               entry("Logout", assigned_to="niemand"), entry("Doku", due_date="2026-11-30")]
    r = save(ui, "/sprints/new", entries, project="KET", goal="Login fertig", start_date="2026-10-12")
    assert r.status_code == 422 and "Eintrag 2:" in r.text and "niemand" in r.text
    assert sprints_of(env) == []
    assert "Login fertig" in r.text and "Logout" in r.text  # Input is kept.
    r = save(ui, "/sprints/new", [entry("x", due_date="morgen")], project="KET")
    assert r.status_code == 422 and "Eintrag 1: Datum im Format JJJJ-MM-TT" in r.text

    entries[1]["assigned_to"] = ""
    r = save(ui, "/sprints/new", entries, project="KET", goal="Login fertig", start_date="2026-10-12")
    assert r.status_code == 200 and "Sprint gespeichert · 3 Entwürfe angelegt" in r.text, r.text
    sid = int(re.search(r"/projects/KET/sprints/(\d+)/editor", str(r.url))[1])
    tasks = sprint_tasks(env, sid)
    assert [(t["title"], t["status"], t["assigned_to"], t["prio"]) for t in tasks] == [
        ("Login", "draft", "hermes", 2), ("Logout", "draft", None, 3), ("Doku", "draft", None, 3)]
    login, logout, doku = (t["task_nr"] for t in tasks)
    assert task(env, doku)["due_date"] == "2026-11-30"
    assert sprints_of(env)[0]["goal"] == "Login fertig" and sprints_of(env)[0]["status"] == "planned"
    page = r.text
    assert page.count('class="se-ready ok"') == 1 and "fehlt: Beschreibung, Akzeptanzkriterien" in page
    assert f"&#34;task_nr&#34;: &#34;{login}&#34;" in page  # Entries JSON for the script.

    # Edit: reverse the order, complete Doku, remove Logout, add one; the sprint page shows the new order.
    v = {t["task_nr"]: t["row_version"] for t in (task(env, nr) for nr in (login, doku))}
    edited = [entry("Doku", doku, v[doku], description="Text", acceptance_criteria="- da", due_date="2026-11-30"),
              entry("Neu"),
              entry("Login", login, v[login], description="Formular", acceptance_criteria="- geht",
                    assigned_to="hermes", prio=2)]
    url = f"/projects/KET/sprints/{sid}/editor"
    r = save(ui, url, edited, removed=[logout], goal="Login fertig", start_date="2026-10-12", name="Sprint 1a")
    assert r.status_code == 200 and "1 Entwurf angelegt · 1 Entwurf aktualisiert · 1 Entwurf abgebrochen" in r.text
    assert task(env, logout)["status"] == "cancelled" and task(env, login)["row_version"] == v[login]  # Unchanged.
    assert task(env, doku)["acceptance_criteria"] == "- da"
    assert sprints_of(env)[0]["name"] == "Sprint 1a"
    sprint_page = ui.get(f"/projects/KET/sprints/{sid}").text
    assert f'href="{url}"' in sprint_page and "Im Editor bearbeiten" in sprint_page
    rows = sprint_page.split("<tbody>")[1]
    assert rows.index(doku) < rows.index("Neu") < rows.index(login)
    assert logout not in r.text  # Cancelled tasks are not listed in the editor.

    # A stale row version is refused, nothing else of that save is kept.
    r = save(ui, url, [entry("Doku alt", doku, v[doku]), entry("Noch einer")], name="Sprint 1a")
    assert r.status_code == 422 and "Eintrag 1: Task wurde zwischenzeitlich geändert" in r.text
    assert task(env, doku)["title"] == "Doku" and len(sprint_tasks(env, sid)) == 4

    # Save and release: complete tasks become defined, the rest stays draft and is listed on the sprint page.
    current = [entry(t["title"], t["task_nr"], t["row_version"], description=t["description"] or "",
                     acceptance_criteria=t["acceptance_criteria"] or "", assigned_to=t["assigned_to"] or "",
                     prio=t["prio"], due_date=t["due_date"] or "")
               for t in (task(env, x["task_nr"]) for x in sprint_tasks(env, sid)) if t["status"] == "draft"]
    r = save(ui, url, current, action="release", name="Sprint 1a")
    assert r.status_code == 200 and str(r.url).split("?")[0].endswith(f"/projects/KET/sprints/{sid}")
    assert "Sprint freigegeben: 2 Tasks freigegeben · 1 bleibt Entwurf" in r.text
    assert "fehlt: Beschreibung, Akzeptanzkriterien" in r.text.split('id="release"')[1]
    assert [task(env, nr)["status"] for nr in (login, doku)] == ["defined", "defined"]
    assert sprints_of(env)[0]["status"] == "released"
    r = ui.get(url)
    assert "Im Sprint-Editor lassen sich nur geplante Sprints bearbeiten" in r.text and "/editor" not in str(r.url)
    assert "Im Editor bearbeiten" not in r.text
    r = save(ui, url, current, name="Sprint 1a")
    assert r.status_code == 422 and "nur geplante Sprints" in r.text


@pytest.mark.db
def test_editor_tasks_past_draft_are_read_only(env, ui):  # noqa: F811
    mkproject(env)
    r = save(ui, "/sprints/new", [entry("Entwurf")], project="KET")
    sid = int(re.search(r"/sprints/(\d+)/editor", str(r.url))[1])
    running = mktask(env, title="Läuft", assigned_to="claude-code")["task_nr"]
    assert claim(env, who="claude").status_code == 200
    t = task(env, running)
    assert env["c"].patch(f"/api/v1/tasks/{running}", headers={**env["admin"], "If-Match": str(t["row_version"])},
                          json={"sprint": sid}).status_code == 200
    url = f"/projects/KET/sprints/{sid}/editor"
    page = ui.get(url).text
    item = page.split('id="se-items"')[1].split("</ol>")[0].split("<li")[2]
    assert "Läuft" in item and "In Arbeit" in item and running in item and "se-del" not in item
    assert "Dieser Task ist kein Entwurf mehr" in page

    draft = sprint_tasks(env, sid)[0]
    t = task(env, running)
    r = save(ui, url, [entry("Läuft umbenannt", running, t["row_version"], status="in_progress"),
                       entry("Entwurf", draft["task_nr"], draft["row_version"])])
    assert r.status_code == 200 and task(env, running)["title"] == "Läuft"  # Content of running tasks is ignored.
    assert task(env, running)["status"] == "in_progress"
    assert ui.get(f"/projects/KET/sprints/{sid}").text.split("<tbody>")[1].index("Läuft") < \
        ui.get(f"/projects/KET/sprints/{sid}").text.split("<tbody>")[1].index("Entwurf")
    r = save(ui, url, [], removed=[running])
    assert r.status_code == 422 and "ist kein Entwurf mehr und kann im Sprint-Editor nicht entfernt werden" in r.text


@pytest.mark.db
def test_editor_rights(env):  # noqa: F811
    mkproject(env)
    human(env, "anna", password="anna-passwort-123")
    human(env, "olga", role="orchestrator", password="olga-passwort-123")  # gitleaks:allow (test fixture)
    anna = login_as("anna", "anna-passwort-123")
    try:
        assert 'href="/sprints/new' not in anna.get("/backlog").text
        assert anna.get("/sprints/new").status_code == 403
    finally:
        anna.__exit__(None, None, None)

    olga = login_as("olga", "olga-passwort-123")  # Orchestrator: may plan, but not release.
    try:
        assert 'href="/sprints/new"' in olga.get("/backlog").text
        page = olga.get("/sprints/new?project=KET").text
        assert re.search(r'id="se-release"\s+hidden', page)
        r = save(olga, "/sprints/new", [entry("A", description="d", acceptance_criteria="a")], project="KET",
                 action="release")
        assert r.status_code == 422 and sprints_of(env) == []  # Release refused: nothing saved.
        r = save(olga, "/sprints/new", [entry("A")], project="KET")
        assert r.status_code == 200 and "1 Entwurf angelegt" in r.text
    finally:
        olga.__exit__(None, None, None)
