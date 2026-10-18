"""Planning board `/planning`: sprint accordion list, the segmented filter, expand/collapse markup, drag and drop
(`sprints.assign`/`sprints.reorder`), sprint actions, dependency hints, quick add and permissions
(test DB + fake Gitea; UI texts in German)."""
import re
from contextlib import contextmanager

import pytest
from sqlalchemy import event, select

from app.db import db
from app.models import Task
from app.services.sprints import prio_from_order
from tests.test_owner import _ui_login, human
from tests.test_sprints_ui import api_task, mksprint
from tests.test_ui_cli import post, ui  # noqa: F401 (Fixture)
from tests.test_workflow import env, gitea, mkproject, mktask  # noqa: F401 (Fixtures)


def test_prio_from_order_matches_the_sprint_editor():
    assert [prio_from_order(i, 3) for i in range(3)] == [1, 2, 3]
    assert [prio_from_order(i, 10) for i in range(10)] == [1, 1, 2, 2, 3, 3, 4, 4, 5, 5]


def move(ui, nr, sprint, order="", csrf=True):  # noqa: F811
    headers = {"X-CSRF-Token": ui.csrf} if csrf else {}
    return ui.post("/planning/move", data={"task_nr": nr, "sprint": str(sprint), "order": order}, headers=headers)


def card(page, sid):
    """HTML of one sprint card (`data-sprint`) including its opening `<section>`. The card's `<ul class="plan-rows">`
    repeats `data-sprint`, so anchor the search on the section tag."""
    m = re.search(r'<section class="plan-sprint[^"]*"\s+data-sprint="%s".*?</section>' % sid, page, re.S)
    return m.group(0) if m else ""


def keys(page):
    """Sprint ids in board order. A closed sprint only appears with the toggle on."""
    return re.findall(r'<section class="plan-sprint[^"]*"\s+data-sprint="(\d+)"', page)


def plan_row(page, nr):
    """HTML of one task row (`data-task`), up to the closing `</li>` of its own list item (the menu's `</li>`
    inside the `<details>` must not cut it short)."""
    part = page.split(f'data-task="{nr}"', 1)[1]
    nxt = part.find('data-task="')
    part = part[:nxt] if nxt >= 0 else part[:part.rfind("</ul>")]
    return part[:part.rfind("</li>")] if "</li>" in part else part


def release(ui, sid, key="KET"):  # noqa: F811
    r = post(ui, f"/projects/{key}/sprints/{sid}/release", back="planning")
    assert r.status_code == 200 and "/planning" in str(r.url), r.text


@contextmanager
def count_queries():
    statements = []

    def before(conn, cursor, statement, *args):
        statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", before)
    try:
        yield statements
    finally:
        event.remove(db.engine, "before_cursor_execute", before)


# --- Board ---------------------------------------------------------------------------------------

@pytest.mark.db
def test_board_is_an_accordion_and_closed_is_hidden(env, ui):  # noqa: F811
    mkproject(env)
    planned = mksprint(ui, name="Wave 7", start_date="2026-11-01")
    released = mksprint(ui, name="Wave 6", goal="Login fertig", start_date="2026-10-20", end_date="2026-10-30")
    closed = mksprint(ui, name="Wave 5")
    mktask(env, title="Ohne Zuordnung")  # Tasks without a sprint stay off the board (no "No sprint" column).
    post(ui, f"/projects/KET/sprints/{released}/batch", lines="Eins | claude-code\nZwei", step="create")
    release(ui, closed)
    post(ui, f"/projects/KET/sprints/{closed}/close", open_tasks="detach")
    release(ui, released)

    page = ui.get("/planning").text
    assert 'href="/planning" aria-current="page">' in page and "Planung" in page
    assert "KET –" in page and "3 Sprints" in page
    assert 'data-sprint="none"' not in page and "Ohne Sprint" not in page and "Ohne Zuordnung" not in page
    # Default filter "All": planned and released sprints, closed hidden (the mockup's pressed default).
    assert keys(page) == [str(released), str(planned)]
    assert "+ Neuer Sprint" in page and 'data-open="dlg-new-sprint"' in page
    head = card(page, released)
    assert 'class="plan-sprint sprint-released' in head and "Freigegeben" in head and "Login fertig" in head
    assert 'aria-expanded="true"' in head and 'class="plan-toggle"' in head
    assert 'style="width: 0%"' in head and "0 / 2 erledigt" in head and "Task" in head
    assert 'aria-expanded="true"' in card(page, planned)  # Planned sprints open by default too.

    assert "Geplant" in ui.get("/planning?filter=planned").text
    assert keys(ui.get("/planning?filter=planned").text) == [str(planned)]
    assert keys(ui.get("/planning?filter=active").text) == [str(released), str(planned)]  # Running = planned+released.
    assert keys(ui.get("/planning?filter=closed").text) == []  # Closed needs the toggle.
    page = ui.get("/planning?filter=all&closed=1").text
    assert keys(page) == [str(released), str(planned), str(closed)]
    assert 'class="plan-sprint sprint-closed is-closed"' in card(page, closed) and 'aria-expanded="false"' in card(page, closed)


@pytest.mark.db
def test_segmented_filter_counts(env, ui):  # noqa: F811
    mkproject(env)
    mksprint(ui, name="Geplant")
    released = mksprint(ui, name="Läuft")
    release(ui, released)
    closed = mksprint(ui, name="Zu")
    release(ui, closed)
    post(ui, f"/projects/KET/sprints/{closed}/close", open_tasks="detach")

    page = ui.get("/planning?filter=all&closed=1").text
    assert "Aktiv 2" in page and "Geplant 1" in page and "Abgeschlossen 1" in page and "Alle 3" in page
    active = page.split("Aktiv 2", 1)[0].rsplit("<a", 1)[1]
    alle = page.split("Alle 3", 1)[0].rsplit("<a", 1)[1]
    assert 'aria-pressed="true"' not in active and 'aria-pressed="true"' in alle  # "All" tab marked.
    assert "Abgeschlossene Sprints zeigen" in page

    # The default (no filter parameter) is "All" with closed hidden: planned and released sprints, no closed one.
    default_page = ui.get("/planning?project=KET").text
    default_tab = default_page.split("Alle 3", 1)[0].rsplit("plan-segment", 1)[1]
    assert 'aria-pressed="true"' in default_tab
    # Closed sprints stay hidden, but the counts still cover every sprint (matching the subtitle "3 Sprints"), so
    # "Alle" and "Abgeschlossen" agree with the total even while the toggle is off.
    assert "3 Sprints" in default_page
    assert "Aktiv 2" in default_page and "Geplant 1" in default_page
    assert "Abgeschlossen 1" in default_page and "Alle 3" in default_page
    assert "sprint-closed" not in default_page


@pytest.mark.db
def test_board_remembers_the_project(env, ui):  # noqa: F811
    mkproject(env)
    mkproject(env, key="ZWEI")
    page = ui.get("/planning?project=ZWEI").text
    assert '<option value="ZWEI" selected>' in page
    assert '<option value="ZWEI" selected>' in ui.get("/planning").text  # Cookie.


@pytest.mark.db
def test_dependency_hint_and_constant_queries(env, ui):  # noqa: F811
    mkproject(env)
    sid = mksprint(ui)
    base = mktask(env, title="Basis", assigned_to="codex", sprint=sid)["task_nr"]  # Assignee names are one query.
    waiting = mktask(env, title="Wartet", sprint=sid, depends_on=[base])["task_nr"]
    page = ui.get("/planning?filter=planned").text
    row = plan_row(page, waiting)
    assert f'href="/tasks/{base}">wartet auf {base}</a>' in row
    assert "plan-waits" in row
    assert "wartet auf" not in plan_row(page, base)

    with count_queries() as few:
        assert ui.get("/planning?filter=planned").status_code == 200
    for i in range(6):
        nr = mktask(env, title=f"Mehr {i}", sprint=sid, assigned_to="hermes" if i % 2 else "claude-code",
                    depends_on=[base, waiting])["task_nr"]
        assert nr
    with count_queries() as many:
        page = ui.get("/planning?filter=planned").text
    assert page.count("wartet auf") == 1 + 6 * 2
    assert len(many) == len(few), (len(few), len(many))


@pytest.mark.db
def test_release_warns_about_late_dependencies(env, ui):  # noqa: F811
    """Release dialog and sprint page name released tasks that wait for a task planned later; release still works."""
    mkproject(env)
    sid = mksprint(ui, name="Jetzt")
    later = mksprint(ui, name="Danach")
    base = mktask(env, title="Basis", sprint=later)["task_nr"]
    waiting = mktask(env, title="Wartet", sprint=sid, depends_on=[base])["task_nr"]
    free = mktask(env, title="Frei", sprint=sid)["task_nr"]

    preview = ui.get(f"/planning/KET/sprints/{sid}/release").text
    warning = preview.split('class="banner warn release-waiting"', 1)[1].split("</div>", 1)[0]
    assert "Wird freigegeben, wartet aber auf einen offenen Task (1)" in warning
    assert f'href="/tasks/{waiting}"' in warning and f'href="/tasks/{base}"' in warning and "Danach, Geplant" in warning
    assert free not in warning
    assert "Sprint und 2 Tasks freigeben? 1 davon wartet auf einen offenen Task." in preview

    page = ui.get(f"/projects/KET/sprints/{sid}").text
    assert "<th>Wartet auf</th>" in page and 'class="banner warn release-waiting"' in page
    row = page.split(f'href="/tasks/{waiting}">{waiting}</a></td>', 1)[1].split("</tr>", 1)[0]
    assert f'href="/tasks/{base}"' in row
    assert f'href="/tasks/{base}"' not in page.split(f'href="/tasks/{free}">{free}</a></td>', 1)[1].split("</tr>")[0]

    release(ui, sid)
    assert api_task(env, waiting)["status"] == "defined"
    page = ui.get(f"/projects/KET/sprints/{sid}").text  # The hint stays while the dependency is open.
    assert f'href="/tasks/{base}"' in page.split(f'href="/tasks/{waiting}">{waiting}</a></td>', 1)[1].split("</tr>")[0]


@pytest.mark.db
def test_released_sprint_with_drafts_can_be_released_again(env, ui):  # noqa: F811
    """A released sprint that collects drafts again (e.g. through the new "+ Task" dialog) keeps its "Release"
    button, so drafts completed since the last release can be published (regression from the accordion redesign)."""
    mkproject(env)
    sid = mksprint(ui, name="Läuft")
    nr = mktask(env, title="Erster", sprint=sid)["task_nr"]  # Defined straight away, not a draft.
    release(ui, sid)
    assert api_task(env, nr)["status"] == "defined"

    # A draft added to the released sprint (here via the "+ Task" dialog) brings back the release button and its
    # dialog; completing it and releasing again publishes it ("releasing again later publishes drafts").
    post(ui, "/planning/KET/quick-add", sprint=str(sid), lines="Zweiter | claude-code")
    page = ui.get("/planning?filter=all&closed=0").text
    assert f'data-open="dlg-release-{sid}"' in card(page, sid)
    assert f'<dialog id="dlg-release-{sid}"' in page

    draft = [t["task_nr"] for t in env["c"].get(f"/api/v1/projects/KET/sprints/{sid}",
                                              headers=env["admin"]).json()["tasks"] if t["status"] == "draft"][0]
    t = api_task(env, draft)
    env["c"].patch(f"/api/v1/tasks/{draft}", headers={**env["admin"], "If-Match": str(t["row_version"])},
                    json={"description": "x", "acceptance_criteria": "- y"})
    release(ui, sid)
    assert api_task(env, draft)["status"] == "defined"


# --- Drag and drop -------------------------------------------------------------------------------

@pytest.mark.db
def test_move_between_cards_and_rejections(env, ui):  # noqa: F811
    mkproject(env)
    planned = mksprint(ui, name="Geplant")
    released = mksprint(ui, name="Läuft")
    release(ui, released)
    closed = mksprint(ui, name="Zu")
    release(ui, closed)
    post(ui, f"/projects/KET/sprints/{closed}/close", open_tasks="detach")
    nr = mktask(env, title="Bereit")["task_nr"]
    assert api_task(env, nr)["status"] == "defined"

    r = move(ui, nr, planned)
    assert r.status_code == 200 and f'data-task="{nr}"' in r.text and 'data-status="draft"' in r.text
    assert api_task(env, nr)["sprint"] == planned and api_task(env, nr)["status"] == "draft"  # to_draft
    assert move(ui, nr, released).status_code == 200 and api_task(env, nr)["sprint"] == released
    assert move(ui, nr, "none").status_code == 200 and api_task(env, nr)["sprint"] is None

    r = move(ui, nr, closed)
    assert r.status_code == 409 and "abgeschlossen" in r.json()["message"]
    assert move(ui, nr, 99999).status_code == 422
    assert move(ui, nr, planned, csrf=False).status_code == 403
    assert api_task(env, nr)["sprint"] is None

    with db.session() as s:
        s.scalar(select(Task).where(Task.task_nr == nr)).status = "done"
        s.commit()
    r = move(ui, nr, released)
    assert r.status_code == 409 and "bleibt in seinem Sprint" in r.json()["message"]


@pytest.mark.db
def test_reorder_planned_sprint_sets_rank_and_draft_prio(env, ui):  # noqa: F811
    mkproject(env)
    sid = mksprint(ui)
    post(ui, f"/projects/KET/sprints/{sid}/batch", lines="A | | 5\nB | | 5\nC | | 5", step="create")
    a, b, c = (t["task_nr"] for t in env["c"].get(f"/api/v1/projects/KET/sprints/{sid}",
                                                   headers=env["admin"]).json()["tasks"])
    r = move(ui, b, sid, order=f"{c} {b} {a}")
    assert r.status_code == 200
    assert [api_task(env, nr)["prio"] for nr in (c, b, a)] == [1, 2, 3]
    page = ui.get("/planning?filter=planned").text
    col = card(page, sid)
    assert col.index(c) < col.index(b) < col.index(a) and 'data-sortable="1"' in col
    assert ui.get(f"/projects/KET/sprints/{sid}").text.index(c) < ui.get(f"/projects/KET/sprints/{sid}").text.index(a)
    # Moving a new task into the planned sprint at a position ranks it there.
    d = mktask(env, title="D")["task_nr"]
    assert move(ui, d, sid, order=f"{c} {d} {b} {a}").status_code == 200
    assert [api_task(env, nr)["prio"] for nr in (c, d, b, a)] == [1, 2, 3, 4]


# --- Card actions and quick add --------------------------------------------------------------------

@pytest.mark.db
def test_sprint_actions_come_back_to_the_board(env, ui):  # noqa: F811
    mkproject(env)
    r = post(ui, "/projects/KET/sprints/new", name="Wave 1", goal="Ziel", back="planning")
    # The board's default filter ("all", closed hidden) shows a newly planned sprint without extra parameters.
    assert r.status_code == 200 and str(r.url).endswith(
        "/planning?project=KET&filter=all&closed=0&msg=Sprint+angelegt")
    sid = int(re.search(r'data-sprint="(\d+)"', r.text)[1])
    r = post(ui, "/projects/KET/sprints/new", name="Wave 1", back="planning")
    assert "/planning" in str(r.url) and "bereits einen Sprint" in r.text

    r = post(ui, f"/projects/KET/sprints/{sid}/edit", name="Wave 1b", goal="Neu", start_date="2026-11-02",
             back="planning")
    assert "/planning" in str(r.url) and "Wave 1b" in r.text and 'title="Neu"' in r.text

    post(ui, "/planning/KET/quick-add", sprint=str(sid), lines="Fertig")
    nr = env["c"].get(f"/api/v1/projects/KET/sprints/{sid}", headers=env["admin"]).json()["tasks"][0]["task_nr"]
    t = api_task(env, nr)
    env["c"].patch(f"/api/v1/tasks/{nr}", headers={**env["admin"], "If-Match": str(t["row_version"])},
                   json={"description": "x", "acceptance_criteria": "- y"})
    post(ui, "/planning/KET/quick-add", sprint=str(sid), lines="Halb")
    preview = ui.get(f"/planning/KET/sprints/{sid}/release").text
    assert "Werden freigegeben (1)" in preview and "Bleibt Entwurf (1)" in preview
    assert 'name="back" value="planning"' in preview
    release(ui, sid)
    assert api_task(env, nr)["status"] == "defined"

    other = mksprint(ui, name="Wave 2")
    r = post(ui, f"/projects/KET/sprints/{sid}/close", open_tasks="move", move_to=str(other), back="planning")
    assert "/planning" in str(r.url) and "nach &#39;Wave 2&#39; verschoben" in r.text
    assert api_task(env, nr)["sprint"] == other
    r = post(ui, f"/projects/KET/sprints/{sid}/close", open_tasks="detach", back="planning")
    assert "/planning" in str(r.url) and 'class="banner error"' in r.text  # Already closed.

    # Reopen from the board: the closed column carries the button and the action comes back with a message.
    board = ui.get("/planning?project=KET&closed=1").text
    assert f'data-open="dlg-reopen-{sid}"' in board and f'<dialog id="dlg-reopen-{sid}">' in board
    assert 'name="restore_tasks" value="1" checked' in board
    r = post(ui, f"/projects/KET/sprints/{sid}/reopen", restore_tasks="1", back="planning")
    assert "/planning" in str(r.url) and "Sprint+wieder+ge" in str(r.url), r.url
    assert api_task(env, nr)["sprint"] == sid and api_task(env, nr)["status"] == "draft"  # Status unchanged.


@pytest.mark.db
def test_quick_add_one_draft_per_line(env, ui):  # noqa: F811
    mkproject(env)
    sid = mksprint(ui)
    r = post(ui, "/planning/KET/quick-add", sprint=str(sid), lines="Eins\n\nZwei | hermes | 2\n")
    assert r.status_code == 200 and "2 Entwürfe angelegt" in r.text and f"#sprint-{sid}" in str(r.url)
    tasks = env["c"].get(f"/api/v1/projects/KET/sprints/{sid}", headers=env["admin"]).json()["tasks"]
    assert sorted((t["title"], t["status"], t["assigned_to"], t["prio"]) for t in tasks) == [
        ("Eins", "draft", None, 3), ("Zwei", "draft", "hermes", 2)]
    r = post(ui, "/planning/KET/quick-add", sprint="none", lines="Lose")
    assert "1 Entwurf angelegt" in r.text
    loose = [t for t in env["c"].get("/api/v1/tasks?project=KET&sprint=none", headers=env["admin"]).json()["items"]]
    assert [(t["title"], t["status"]) for t in loose] == [("Lose", "draft")]
    r = post(ui, "/planning/KET/quick-add", sprint="none", lines="Gut\nSchlecht | niemand")
    assert "niemand" in r.text and 'class="banner error"' in r.text
    assert len(env["c"].get("/api/v1/tasks?project=KET&sprint=none", headers=env["admin"]).json()["items"]) == 1


# --- Permissions ---------------------------------------------------------------------------------

@pytest.mark.db
def test_permissions_viewer_planner_owner(env, ui):  # noqa: F811
    mkproject(env)
    sid = mksprint(ui)
    nr = mktask(env, title="Fremd")["task_nr"]
    # bob: member without a project, no planning rights; olga: plans, but cannot release.
    human(env, "bob", password="bob-passwort-1234")  # gitleaks:allow (test fixture)
    human(env, "olga", role="orchestrator", password="olga-passwort-1234")  # gitleaks:allow (test fixture)
    human(env, "anna", password="anna-passwort-1234")  # gitleaks:allow (test fixture)
    mkproject(env, key="ANNA")
    env["c"].patch("/api/v1/projects/ANNA", headers=env["admin"], json={"owner": "anna"})

    bob = _ui_login("bob", "bob-passwort-1234")
    assert 'href="/planning"' not in bob.get("/backlog").text
    assert bob.get("/planning").status_code == 403
    r = bob.post("/planning/move", data={"task_nr": nr, "sprint": str(sid)}, headers={"X-CSRF-Token": bob.csrf})
    assert r.status_code == 403 and api_task(env, nr)["sprint"] is None
    assert bob.get(f"/planning/KET/sprints/{sid}/release").status_code == 403

    olga = _ui_login("olga", "olga-passwort-1234")
    page = olga.get("/planning?project=KET&filter=planned").text
    assert 'href="/planning"' in page and f'data-sprint="{sid}"' in page
    head = card(page, sid)
    assert "disabled" in head and "Nur Owner/Admins können freigeben" in head
    assert f'id="dlg-release-{sid}"' not in page
    assert olga.get(f"/planning/KET/sprints/{sid}/release").status_code == 403
    r = olga.post("/planning/move", data={"task_nr": nr, "sprint": str(sid)}, headers={"X-CSRF-Token": olga.csrf})
    assert r.status_code == 200 and api_task(env, nr)["sprint"] == sid

    anna = _ui_login("anna", "anna-passwort-1234")  # Owner of ANNA only: the board offers just her project.
    page = anna.get("/planning").text
    assert 'href="/planning"' in page and re.findall(r'<option value="([A-Z]+)"', page.split(
        'action="/planning"', 1)[1].split("</select>", 1)[0]) == ["ANNA"]
    r = anna.post("/planning/move", data={"task_nr": nr, "sprint": "none"}, headers={"X-CSRF-Token": anna.csrf})
    assert r.status_code == 403 and api_task(env, nr)["sprint"] == sid
    asid = mksprint(anna, name="Annas Sprint", key="ANNA")
    page = anna.get("/planning?project=ANNA&filter=planned").text
    assert f'data-open="dlg-release-{asid}"' in page and f'id="dlg-release-{asid}"' in page
