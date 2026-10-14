"""Mobile UI (compact header): app shell, compact inbox, backlog cards and filters, kanban "Move" menu and column
picker, and kept input after errors on the task page."""
import re

import pytest

from tests.test_owner import _ui_login, human
from tests.test_ui_cli import post, ui  # noqa: F401 (Fixtures)
from tests.test_workflow import claim, env, gitea, mkproject, mktask  # noqa: F401 (Fixtures)

pytestmark = pytest.mark.db


def _moves(ui, nr) -> list[str]:  # noqa: F811
    r = ui.get(f"/kanban/{nr}/moves")
    assert r.status_code == 200, r.text
    return re.findall(r'data-move-to="(\w+)"', r.text)


# --- App shell --------------------------------------------------------------------------------------------------

def test_shell_has_menu_button_quick_capture_sheet_and_rights(env, ui):  # noqa: F811
    mkproject(env)
    html = ui.get("/backlog").text
    assert 'class="secondary menu-toggle" aria-expanded="false" aria-controls="main-nav"' in html
    assert '<ul id="main-nav" class="collapsible">' in html and 'href="/admin/users"' in html
    # "+ Task" opens the same quick capture form (same action and fields), it never submits by itself.
    assert 'type="button" class="small qc-toggle" aria-expanded="false" aria-controls="quick-capture"' in html
    assert 'class="quick-capture collapsible" id="quick-capture" method="post" action="/tasks/quick"' in html
    assert '<script src="/static/shell.js' in html
    # A member sees the menu but no admin entry (same rights as before).
    human(env, "mia", password="mia-password-123")
    member = _ui_login("mia", "mia-password-123").get("/").text
    assert 'aria-controls="main-nav"' in member and 'href="/admin/users"' not in member


def test_login_page_keeps_inline_navigation(env):  # noqa: F811
    from fastapi.testclient import TestClient

    from app.main import app
    html = TestClient(app).get("/login").text
    assert "menu-toggle" not in html and '<ul id="main-nav">' in html


# --- Inbox ------------------------------------------------------------------------------------------------------

def test_inbox_empty_is_compact_and_notices_stay_findable(env, ui):  # noqa: F811
    mkproject(env)
    html = ui.get("/").text
    assert "Alles erledigt" in html and "keine Rückfragen oder Tasks zur Prüfung." in html
    assert "Keine Hinweise." in html and "<h4>Rückfragen" not in html and 'class="notice' not in html
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
    assert 'Rückfragen <span class="count-pill">1</span>' in html and "Welche Farbe?" in html
    assert "Alles erledigt" not in html


# --- Backlog ----------------------------------------------------------------------------------------------------

def test_backlog_search_visible_filters_folded_with_count_and_reset(env, ui):  # noqa: F811
    mkproject(env)
    mktask(env, title="Ein sehr langer Titel " * 8, prio=1)
    html = ui.get("/backlog?agent=none&prio=1&q=langer").text
    search = html.index('type="search" name="q"')
    assert search < html.index('aria-controls="backlog-more"') < html.index('<div id="backlog-more" class="filter-more collapsible">')
    assert 'Filter <span class="count">2</span>' in html and "2 Filter aktiv" in html
    assert '<a href="/backlog" class="small">Filter zurücksetzen</a>' in html
    # Cards and table come from the same rows; the sort fields belong to the filter form.
    assert '<table class="cards backlog">' in html and "Ein sehr langer Titel" in html
    assert '<select id="backlog-sort" name="sort" form="backlog-filter">' in html
    plain = ui.get("/backlog").text
    assert "Filter aktiv" not in plain and "Filter zurücksetzen" not in plain


def test_backlog_sort_fields_follow_table_updates(env, ui):  # noqa: F811
    mkproject(env)
    r = ui.get("/backlog?sort=title&desc=1", headers={"HX-Request": "true", "HX-Target": "backlog-table"})
    assert "<html" not in r.text  # Partial only.
    assert '<option value="title" selected>' in r.text and '<option value="1" selected>absteigend' in r.text


# --- Kanban -----------------------------------------------------------------------------------------------------

def test_kanban_column_picker_and_col_parameter(env, ui):  # noqa: F811
    mkproject(env)
    mktask(env)
    html = ui.get("/kanban").text
    assert '<label for="kanban-col-picker">Statusspalte</label>' in html
    assert re.search(r'<option value="defined" data-label="Definiert" >Definiert \(1\)</option>', html)
    assert 'data-column="in_progress" data-current' in html  # Default column on phones.
    assert '<input type="hidden" name="col" value="in_progress">' in html
    html = ui.get("/kanban?col=done&project=KET").text
    assert 'data-column="done" data-current' in html and html.count("data-current") == 1
    assert 'Filter <span class="count">1</span>' in html and 'href="/kanban?col=done"' in html
    assert 'data-column="in_progress" data-current' in ui.get("/kanban?col=bogus").text


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
    assert 'value="cancel"' in more and 'value="override"' in more and "data-follow-up" not in more
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
