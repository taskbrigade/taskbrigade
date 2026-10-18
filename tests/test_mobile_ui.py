"""Mobile UI (compact header): app shell, compact inbox, backlog cards and filters, kanban "Move" menu and column
picker, and kept input after errors on the task page."""
import re

import pytest

from tests.gitea_fake import SERVICE_USER
from tests.test_owner import _ui_login, human
from tests.test_sprints_ui import mksprint
from tests.test_ui_cli import post, ui  # noqa: F401 (Fixtures)
from tests.test_workflow import SHA, claim, env, gitea, mkproject, mktask  # noqa: F401 (Fixtures)

pytestmark = pytest.mark.db


def _moves(ui, nr) -> list[str]:  # noqa: F811
    r = ui.get(f"/kanban/{nr}/moves")
    assert r.status_code == 200, r.text
    return re.findall(r'data-move-to="(\w+)"', r.text)


# --- App shell --------------------------------------------------------------------------------------------------

def test_shell_has_menu_button_quick_capture_sheet_and_rights(env, ui):  # noqa: F811
    mkproject(env)
    html = ui.get("/backlog").text
    assert 'class="bottom-tabs"' in html and 'aria-controls="more-menu"' in html
    assert 'id="main-nav" class="desktop-nav"' in html and 'href="/admin/users"' in html
    # The phone capture button opens the same form; it never submits by itself.
    assert 'type="button" class="qc-toggle" aria-expanded="false" aria-controls="quick-capture"' in html
    assert 'class="quick-capture collapsible" id="quick-capture" method="post" action="/tasks/quick"' in html
    assert '<script src="/static/shell.js' in html
    # A member sees the menu but no admin entry (same rights as before).
    human(env, "mia", password="mia-password-123")
    member = _ui_login("mia", "mia-password-123").get("/").text
    assert 'aria-controls="more-menu"' in member and 'href="/admin/users"' not in member


def test_login_page_keeps_inline_navigation(env):  # noqa: F811
    from fastapi.testclient import TestClient

    from app.main import app
    html = TestClient(app).get("/login").text
    assert 'class="bottom-tabs"' not in html and 'aria-controls="account-menu"' not in html


# --- Inbox ------------------------------------------------------------------------------------------------------

def test_inbox_empty_is_compact_and_notices_stay_findable(env, ui):  # noqa: F811
    mkproject(env)
    html = ui.get("/").text
    assert "Alles erledigt" in html and "nichts zu prüfen." in html
    assert "Keine Hinweise." in html and "<h4>Rückfragen" not in html and '<details class="notice ' not in html
    nr = mktask(env, title="Ohne Agent")["task_nr"]  # Released, nobody assigned → notice.
    html = ui.get("/").text
    assert '<details class="notice unassigned" open data-fold-narrow>' in html and nr in html
    assert "Keine Hinweise." not in html and "Alles erledigt" in html


def test_inbox_lists_needed_actions_with_count(env, ui):  # noqa: F811
    mkproject(env)
    nr = mktask(env, assigned_to="claude-code")["task_nr"]
    claim(env)
    env["c"].post(f"/api/v1/tasks/{nr}/ask", headers=env["claude"], json={"body": "Welche Farbe?"})
    html = ui.get("/").text
    assert "Welche Farbe?" in html and "Rückfragen 1" in html
    assert 'class="nav-badge" aria-label="Aktion erforderlich">1</span>' in html
    assert 'class="nav-badge" aria-label="Aktion erforderlich">1</span>' in ui.get('/backlog').text
    assert "Alles erledigt" not in html


def test_inbox_segment_filter_and_mark_all_read(env, ui):  # noqa: F811
    from sqlalchemy import select as sa_select

    from app.db import db
    from app.models import Actor, Notification
    from app.services import notifications as notif

    mkproject(env)
    nr = mktask(env, assigned_to="claude-code")["task_nr"]
    claim(env)
    env["c"].post(f"/api/v1/tasks/{nr}/ask", headers=env["claude"], json={"body": "Welche Farbe?"})
    # Segmented filter: the question shows under "questions", not under "reviews".
    assert nr in ui.get("/").text
    assert nr in ui.get("/?tab=questions").text
    assert nr not in ui.get("/?tab=reviews").text

    # Notices feed: queue a notice for the admin, then "Mark all read" clears the unread state.
    with db.session() as s:
        admin = s.scalar(sa_select(Actor).where(Actor.name == "admin"))
        notif.store_settings_notice(s, admin.id, "hello")
        s.commit()
    from datetime import UTC, datetime
    r = ui.post("/inbox/notices/read", json={"all": True, "until": datetime.now(UTC).isoformat()},
                headers={"X-CSRF-Token": ui.csrf})
    assert r.status_code == 200
    with db.session() as s:
        unread = s.scalar(sa_select(Notification).where(
            Notification.actor_id == admin.id, Notification.read_at.is_(None)))
        assert unread is None


def _submit_for_review(env, ui, title="Review me", **kw):  # noqa: F811
    g = env["gitea"]
    nr = mktask(env, title=title, assigned_to="claude-code", **kw)["task_nr"]
    branch = env["c"].post("/api/v1/agents/me/claim", headers=env["claude"],
                           json={"task_nr": nr}).json()["branch_name"]
    g.push_branch(SERVICE_USER, "ket", branch)
    env["c"].post(f"/api/v1/tasks/{nr}/submit", headers=env["claude"],
                  json={"branch_name": branch, "commit_sha": SHA, "summary": "x"})
    return nr


def test_inbox_inline_review_and_question_actions(env, ui):  # noqa: F811
    mkproject(env)
    nr = _submit_for_review(env, ui)
    h = {"X-CSRF-Token": ui.csrf, "HX-Request": "true"}

    # Request changes with an empty comment is refused, the task stays for_review.
    r = ui.post(f"/inbox/{nr}/reject", data={"body": ""}, headers=h)
    assert r.status_code == 422 and "Pflicht" in r.text
    assert env["c"].get(f"/api/v1/tasks/{nr}", headers=env["admin"]).json()["status"] == "for_review"

    # Request changes with a comment sends the task back to the agent.
    r = ui.post(f"/inbox/{nr}/reject", data={"body": "Bitte anpassen"}, headers=h)
    assert r.status_code == 200
    t = env["c"].get(f"/api/v1/tasks/{nr}", headers=env["admin"]).json()
    assert t["status"] == "changes_requested" and "Bitte anpassen" in t["comments"][-1]["body"]

    # Question: the inline answer posts into the thread and clears the question.
    q = mktask(env, title="Frage", assigned_to="claude-code")["task_nr"]
    env["c"].post("/api/v1/agents/me/claim", headers=env["claude"], json={"task_nr": q})
    env["c"].post(f"/api/v1/tasks/{q}/ask", headers=env["claude"], json={"body": "Welche Farbe?"})
    assert 'hx-post="/inbox/%s/answer"' % q in ui.get("/").text
    r = ui.post(f"/inbox/{q}/answer", data={"body": "Grün."}, headers=h)
    assert r.status_code == 200 and "beantwortet" in r.text


def test_inbox_sprint_and_tab_keep_each_other(env, ui):  # noqa: F811
    mkproject(env)
    sid = env["c"].post("/api/v1/projects/KET/sprints", headers=env["admin"],
                        json={"name": "Welle"}).json()["id"]
    env["c"].post(f"/api/v1/projects/KET/sprints/{sid}/release", headers=env["admin"])
    review = _submit_for_review(env, ui, title="Sprint review", sprint=sid)
    q = mktask(env, title="Sprint frage", assigned_to="claude-code", sprint=sid)["task_nr"]
    env["c"].post("/api/v1/agents/me/claim", headers=env["claude"], json={"task_nr": q})
    env["c"].post(f"/api/v1/tasks/{q}/ask", headers=env["claude"], json={"body": "Farbe?"})

    # Sprint + tab work together: the segment stays active and only the matching card remains.
    html = ui.get(f"/?sprint={sid}&tab=reviews").text
    assert review in html and q not in html
    assert 'aria-pressed="true"' in html and "Reviews" in html
    assert 'name="tab" value="reviews"' in html  # Preserved in the form (hidden input).
    assert "Reviews 1" in html and "Rückfragen 1" in html  # Segment counts stay correct.
    html = ui.get(f"/?sprint={sid}&tab=questions").text
    assert q in html and review not in html


# --- Backlog ----------------------------------------------------------------------------------------------------

def test_backlog_search_visible_filters_folded_with_count_and_reset(env, ui):  # noqa: F811
    mkproject(env)
    mktask(env, title="Ein sehr langer Titel " * 8, prio=1)
    html = ui.get("/backlog?agent=none&prio=1&q=langer").text
    search = html.index('type="search" name="q"')
    assert search < html.index('aria-controls="backlog-more"') < html.index('<div id="backlog-more" class="filter-more collapsible">')
    assert 'Filter <span class="count">2</span>' in html and "2 Filter aktiv" in html
    assert 'href="/backlog?workflow=hide&amp;owner=all"' in html
    # Cards and table come from the same rows; the sort fields belong to the filter form.
    assert '<table class="ui-table backlog">' in html and "Ein sehr langer Titel" in html
    assert '<select id="backlog-sort" name="sort" form="backlog-filter">' in html
    plain = ui.get("/backlog").text
    assert "Filter aktiv" not in plain and "Filter zurücksetzen" in plain


def test_backlog_sort_fields_follow_table_updates(env, ui):  # noqa: F811
    mkproject(env)
    r = ui.get("/backlog?sort=title&desc=1", headers={"HX-Request": "true", "HX-Target": "backlog-table"})
    assert "<html" not in r.text  # Partial only.
    assert '<option value="title" selected>' in r.text and '<option value="1" selected>absteigend' in r.text


# --- Kanban -----------------------------------------------------------------------------------------------------

def test_kanban_status_tabs_and_col_parameter(env, ui):  # noqa: F811
    mkproject(env)
    mktask(env)
    html = ui.get("/kanban").text
    assert 'class="kanban-tabs"' in html and 'aria-label="Statusspalten"' in html
    assert 'data-column="defined" aria-current' not in html  # defined is not the active column
    assert 'data-column="draft" data-current' in html  # Default column on phones: the first one.
    assert '<input type="hidden" name="col" value="draft">' in html
    html = ui.get("/kanban?col=done&project=KET").text
    assert 'data-column="done" data-current' in html and html.count("data-current") == 1
    assert 'Filter <span class="count">1</span>' in html and 'href="/kanban?col=done&amp;workflow=hide"' in html
    assert 'data-column="draft" data-current' in ui.get("/kanban?col=bogus").text


def test_kanban_move_menu_offers_only_allowed_transitions(env, ui):  # noqa: F811
    mkproject(env)
    defined = mktask(env)["task_nr"]
    assert _moves(ui, defined) == ["draft"]  # to_draft; never an override like → done.
    running = mktask(env, assigned_to="claude-code")["task_nr"]
    claim(env, task_nr=running)
    assert _moves(ui, running) == ["aborted"]  # Human admin may abort; release/submit belong to the agent.
    draft = mktask(env, status="draft")["task_nr"]
    assert _moves(ui, draft) == ["defined"]
    # A member who neither owns the project nor manages it gets no moves.
    human(env, "mia", password="mia-password-123")
    member = _ui_login("mia", "mia-password-123")
    r = member.get(f"/kanban/{defined}/moves")
    assert "data-move-to" not in r.text and "kein Statuswechsel erlaubt" in r.text


def test_kanban_move_via_menu_returns_card_with_move_control(env, ui):  # noqa: F811
    mkproject(env)
    nr = mktask(env)["task_nr"]
    h = {"X-CSRF-Token": ui.csrf}
    r = ui.post("/kanban/move", data={"task_nr": nr, "to": "draft"}, headers=h)
    assert r.status_code == 200 and 'data-status="draft"' in r.text and 'class="card-move"' in r.text
    # Not allowed (no rule, no override requested): the status stays, the reason comes back.
    r = ui.post("/kanban/move", data={"task_nr": nr, "to": "done"}, headers=h)
    assert r.status_code == 409 and "nicht erlaubt" in r.json()["message"]
    assert env["c"].get(f"/api/v1/tasks/{nr}", headers=env["admin"]).json()["status"] == "draft"


# --- Task details -----------------------------------------------------------------------------------------------

def test_task_detail_reads_first_with_tabs_and_more_actions(env, ui):  # noqa: F811
    mkproject(env)
    nr = mktask(env, description="**Wichtig**")["task_nr"]
    html = ui.get(f"/tasks/{nr}").text
    assert 'role="tablist" data-tabs' in html and 'id="tab-thread" aria-controls="thread" aria-selected="false"' in html
    assert '<section class="form-card read-view"' in html and "<strong>Wichtig</strong>" in html
    assert 'class="small secondary edit-toggle" aria-controls="edit" aria-expanded="false" data-toggle' in html
    assert 'id="edit" class="collapsible">' in html
    assert '<details class="action-fold more-actions">' in html and "Weitere Aktionen" in html
    more = html.split('<details class="action-fold more-actions">', 1)[1].split('<details class="side-card', 1)[0]
    assert 'value="cancel"' in more and 'value="override"' not in more and "data-follow-up" not in more
    assert 'class="side-card admin-override"' in html and 'value="override"' in html
    assert 'props-card' in html and "Eigenschaften" in html
    # Follow-up and duplicate stay visible above "More actions".
    actions = html.split('id="actions-heading"', 1)[1].split('<details class="action-fold more-actions">', 1)[0]
    assert "data-follow-up" in actions and "data-duplicate" in actions


def test_comment_error_keeps_text_and_opens_thread(env, ui):  # noqa: F811
    mkproject(env)
    nr = mktask(env)["task_nr"]
    r = ui.post(f"/tasks/{nr}/comment", data={"csrf_token": ui.csrf, "body": "   \n  "})
    assert r.status_code == 422 and "Kommentar darf nicht leer sein" in r.text
    assert 'id="tab-thread" aria-controls="thread" aria-selected="true"' in r.text
    assert '<div id="details" class="tab-panel tab-hidden" role="tabpanel" aria-labelledby="tab-details">' in r.text


def test_edit_error_reopens_form_with_entered_values(env, ui):  # noqa: F811
    mkproject(env)
    nr = mktask(env)["task_nr"]
    r = ui.post(f"/tasks/{nr}/edit", data={"csrf_token": ui.csrf, "row_version": "0", "title": "Neuer Titel",
                                          "description": "Mein Entwurf", "prio": "2"})
    assert r.status_code == 422 and 'id="edit" class="collapsible is-open"' in r.text
    assert 'value="Neuer Titel"' in r.text and "Mein Entwurf</textarea>" in r.text
    assert 'aria-expanded="true" data-toggle' in r.text


# --- Planning (sprint accordion) --------------------------------------------------------------------------------

def test_planning_accordion_is_not_a_scrolling_board(env, ui):  # noqa: F811
    """The planning list is an accordion of full-width cards (no horizontal column board); the toggle carries
    aria-expanded and the row menu is the touch/keyboard fallback for drag and drop."""
    mkproject(env)
    sid = mksprint(ui, name="Wave 1")
    post(ui, f"/projects/KET/sprints/{sid}/batch", lines="Erster | claude-code", step="create")
    nr = env["c"].get(f"/api/v1/projects/KET/sprints/{sid}", headers=env["admin"]).json()["tasks"][0]["task_nr"]
    html = ui.get("/planning?filter=planned").text
    assert 'class="kanban planning"' not in html and '<div class="plan-list"' in html
    assert 'class="plan-toggle"' in html and 'aria-expanded="true"' in html and 'aria-controls="sprint-body-' in html
    assert 'class="plan-sprint-meta"' in html and 'class="plan-bar"' in html and "0 / 1 erledigt" in html
    row = html.split(f'data-task="{nr}"', 1)[1].split("</li>", 1)[0]
    assert 'class="plan-row"' in html and 'class="plan-move"' in row and "data-move-sprint=" in row
