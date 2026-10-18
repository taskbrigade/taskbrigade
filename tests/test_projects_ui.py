"""Projects overview (`/projects`) after the redesign: list by default, cards via the toggle, filters in the URL,
real task numbers (test DB + fake Gitea). UI texts are German (conftest `UI_LANGUAGE=de`); project status pills keep
their English labels because `project_status_label` is not connected to the redesign pill macro."""
import re

import pytest
from sqlalchemy import text

from app.db import db
from tests.test_ui_cli import post, ui  # noqa: F401 (Fixtures)
from tests.test_ui_screens import _login, _settle, browser, server  # noqa: F401 (Fixtures)
from tests.test_workflow import env, gitea, mkproject, mktask  # noqa: F401 (Fixtures)

pytestmark = pytest.mark.db


def _options(html: str, name: str) -> set[str]:
    """Values of the <option>s of one <select> (opening tags only)."""
    select = re.search(rf'<select[^>]*name="{name}"[^>]*>(.*?)</select>', html, re.S)
    return set(re.findall(r'<option value="([^"]*)"', select[1])) if select else set()


def _rows(html: str) -> str:
    """Body of the list table (for order assertions)."""
    return html.split("<tbody>")[1].split("</tbody>")[0]


def _row(html: str, key: str) -> str:
    """The table row of one project."""
    return next(row for row in re.findall(r"<tr>.*?</tr>", html, re.S) if f'<a href="/projects/{key}"' in row)


def _cards(html: str) -> list[str]:
    return re.findall(r'<article class="ui-card ui-project-card.*?</article>', html, re.S)


def test_projects_list_is_default_and_cards_via_url(env, ui):  # noqa: F811
    """List view first and by default; "Cards" applies only while `?view=cards` is in the URL (nothing remembered)."""
    mkproject(env, key="KET")
    page = ui.get("/projects").text
    toolbar = re.search(r'<form class="ui-toolbar".*?</form>', page, re.S)[0]
    assert toolbar.index('value="list"') < toolbar.index('value="cards"')  # List first in the toggle.
    assert 'name="view" value="list" aria-pressed="true"' in page
    assert 'class="ui-table ui-table-cards"' in page and "<td" in page
    assert "ui-card-grid" not in page
    r = ui.get("/projects?view=cards")
    assert "ui-card-grid" in r.text and "ui-table-cards" not in r.text
    assert "tm_projects_view" not in r.cookies  # No longer remembered.
    assert "ui-table-cards" in ui.get("/projects").text  # Next visit without a parameter is the list again.
    assert "ui-table-cards" in ui.get("/projects?view=quatsch").text  # Invalid value → list.
    # Cards view: key badge, status pill, repo link, progress bar, counts and the quick links.
    card = _cards(r.text)[0]
    assert "KET" in card and "Projekt KET" in card
    assert "ui-st-active" in card  # Status pill of the project.
    assert "/taskbrigade/ket" in card  # Repo link (↗ Gitea).
    assert 'class="ui-progress" role="img" aria-label="noch keine Tasks"' in card
    assert "ui-progress-done" not in card  # Empty track, no filled segments.
    assert re.search(r"Offen <strong>0</strong>", card)
    assert "0 Agenten" in card and "· –" in card  # No tasks → no last activity (em dash).
    assert 'href="/kanban?project=KET"' in card and 'href="/backlog?project=KET"' in card
    assert "+ Neues Projekt" in r.text  # The dashed final card.


def test_projects_ignores_and_deletes_stale_view_cookie(env, ui):  # noqa: F811
    """An old `tm_projects_view=cards` cookie does not force the card view; the list stays and the cookie is deleted."""
    mkproject(env, key="KET")
    ui.cookies.set("tm_projects_view", "cards")
    r = ui.get("/projects")
    assert "ui-table-cards" in r.text and "ui-card-grid" not in r.text  # List, not cards.
    assert "tm_projects_view" not in r.cookies  # Deleted in the response.
    # The stale value is ignored even with an explicit (invalid) `?view` in the URL.
    r = ui.get("/projects?view=quatsch")
    assert "ui-table-cards" in r.text and "tm_projects_view" not in r.cookies


def test_projects_filters_and_sort_are_in_the_url(env, ui):  # noqa: F811
    """Mine/All, search, status and sort come from the URL (GET form) and filter/sort the rows."""
    mkproject(env, key="KET")
    mkproject(env, key="ZWEI", name="Zweites Projekt")
    mktask(env, title="Offen", prio=2)
    with db.session() as s:  # Archive ZWEI so the status filter has something to filter.
        s.execute(text("UPDATE projects SET status='archived' WHERE project_key='ZWEI'"))
        s.commit()
    page = ui.get("/projects").text
    assert "/projects/KET" in page and "/projects/ZWEI" not in page  # Default status "active".
    assert "1 aktives Projekt" in page  # Subtitle counts the active projects.
    page = ui.get("/projects?status=all").text
    assert "/projects/KET" in page and "/projects/ZWEI" in page
    assert 'name="status"' in page and _options(page, "status") == {"active", "paused", "archived", "all"}
    assert _options(page, "sort") == {"activity", "key", "open"}
    for value, label in (("active", "Aktiv"), ("paused", "Pausiert"), ("archived", "Archiviert"),
                         ("all", "Alle")):
        assert f'value="{value}"' in page and f">{label}</option>" in page  # German labels.
    # Search matches key, name and repo (case-insensitive).
    assert "Zweites Projekt" in ui.get("/projects?status=all&q=zwei").text
    assert "Zweites Projekt" in ui.get("/projects?status=all&q=ZWEI").text
    assert "Zweites Projekt" in ui.get("/projects?status=all&q=TASKBRIGADE/ZWEI").text
    assert "Zweites Projekt" not in ui.get("/projects?status=all&q=taskbrigade/ket").text
    # Sort by key ascending; "Offene Tasks" sorts by the open count.
    keys = re.findall(r'<span class="ui-pill ui-pill-mono">([A-Z]+)</span>',
                      _rows(ui.get("/projects?status=all&sort=key").text))
    assert keys == ["KET", "ZWEI"]
    ket = _row(ui.get("/projects?status=all&sort=open").text, "KET")
    assert re.search(r'class="ui-num" data-label="Offen">1</td>', ket)


def test_projects_numbers_are_real_and_aggregated(env, ui):  # noqa: F811
    """Counts, last activity and the agent count are real values from one aggregated query per fact."""
    mkproject(env, key="KET")
    mktask(env, title="Offen")
    mktask(env, title="In Arbeit")
    done = mktask(env, title="Fertig")
    nr = done["task_nr"]
    with db.session() as s:  # Status counts are the point here; the state machine is tested elsewhere.
        s.execute(text("UPDATE tasks SET status='done', done_at=NOW() WHERE task_nr=:nr"), {"nr": nr})
        s.commit()
    with db.session() as s:
        from app.services import projects

        pid = s.scalar(text("SELECT id FROM projects WHERE project_key='KET'"))
        s.execute(text("INSERT INTO project_members (project_id, actor_id) "
                       "SELECT :p, id FROM actors WHERE name='claude-code'"), {"p": pid})
        s.commit()
        entry = projects.overview(s, [pid])[pid]
    assert entry["counts"] == {"defined": 2, "done": 1}
    assert entry["last_activity"] is not None
    assert entry["agents"] == 1
    page = ui.get("/projects").text
    row = _row(page, "KET")
    assert re.search(r'data-label="Offen">2</td>', row)  # Draft/defined in progress count as open.
    assert re.search(r'data-label="Review">0</td>', row)
    assert "heute" in row  # Last activity is relative, not a placeholder.
    assert "ui-progress-done" in row and "width: 33.3" in row  # 1 of 3 tasks done.
    assert re.search(r'aria-label="Fortschritt: 1 erledigt, 0 im Review, 0 in Arbeit von 3 Tasks"', row)


def test_projects_card_counts_and_agents(env, ui):  # noqa: F811
    """Cards view: real counts, agent count and last activity in the footer (no placeholders)."""
    mkproject(env, key="KET")
    mktask(env, title="Offen")
    with db.session() as s:
        pid = s.scalar(text("SELECT id FROM projects WHERE project_key='KET'"))
        s.execute(text("INSERT INTO project_members (project_id, actor_id) "
                       "SELECT :p, id FROM actors WHERE name IN ('claude-code', 'codex')"), {"p": pid})
        s.commit()
    card = _cards(ui.get("/projects?view=cards").text)[0]
    assert re.search(r"Offen <strong>1</strong>", card)
    assert re.search(r"In Arbeit <strong>0</strong>", card)
    assert re.search(r"Review <strong>0</strong>", card)
    assert re.search(r"Erledigt <strong>0</strong>", card)
    assert re.search(r">2 Agenten · heute<", card)


def test_projects_current_project_and_mobile_list(env, ui):  # noqa: F811
    """The quick capture project is marked once ("Aktuell") and leads the card view; the phone keeps the list."""
    mkproject(env, key="KET")
    mkproject(env, key="ZWEI", name="Zweites Projekt")
    page = ui.get("/projects?status=all").text
    rows = _rows(page)
    assert rows.count(">Aktuell</span>") == 1
    assert _options(page, "project") == {"KET", "ZWEI"}  # Quick capture choice list.
    assert "Aktuell" in _row(page, "KET")  # First capture project (KET < ZWEI).
    cards = _cards(ui.get("/projects?status=all&view=cards").text)
    assert len(cards) == 2  # No project is shown twice.
    assert "ui-card-current" in cards[0] and "/projects/KET" in cards[0]  # Current project first.
    assert "Zweites Projekt" in cards[1]
    # Phone: the list view stays the table (rows become cards via CSS), the toggle is hidden.
    assert '<span class="ui-only-desktop ui-toolbar-view">' in page
    assert re.search(r'<span class="ui-only-desktop ui-toolbar-view">.*?name="view"', page, re.S)


def test_projects_mine_scope_filters_for_all_projects_users(env, ui):  # noqa: F811
    """`?mine=mine` filters by owner even for a user whose profile switch "Show all projects" is on (AC Mine/All)."""
    mkproject(env, key="KET")  # Admin owns KET; ZWEI belongs to another human.
    mkproject(env, key="ZWEI", name="Zweites Projekt")
    with db.session() as s:
        s.execute(text("UPDATE actors SET show_all_projects=1 WHERE name='admin'"))
        s.execute(text("UPDATE projects SET owner_id=(SELECT id FROM actors WHERE name='claude-code') "
                       "WHERE project_key='ZWEI'"))
        s.commit()
    page = ui.get("/projects?status=all").text  # All projects (default with the switch on).
    assert "/projects/KET" in page and "/projects/ZWEI" in page
    assert 'name="mine" value="all" aria-pressed="true"' in page
    page = ui.get("/projects?status=all&mine=mine").text
    assert "/projects/KET" in page and "/projects/ZWEI" not in page  # Filtered by owner, not just the segment.
    assert 'name="mine" value="mine" aria-pressed="true"' in page
    assert "1 aktives Projekt" in page and "Owner admin" in page  # Subtitle names the owner of the scope.
    assert "ui-table-cards" in page  # List is the default view.


def test_projects_toolbar_keeps_filter_state(env, ui):  # noqa: F811
    """The toolbar carries the state the clicked control does not: clicking a segment keeps mine and view.

    The selects submit on change (`onchange="this.submit()"`); the segments submit their own value, so the
    other filter must travel as a hidden input. `mine` needs one too: for a user with "Show all projects" on,
    every submit that does not click the mine button (view/status/sort/search) would otherwise fall back to all.
    """
    mkproject(env, key="KET")
    # Cards view + mine: the scope segment keeps the view, the view segment keeps mine.
    with db.session() as s:  # "Show all projects" on: only then can the user be in the mine scope of their own choice.
        s.execute(text("UPDATE actors SET show_all_projects=1 WHERE name='admin'"))
        s.commit()
    page = ui.get("/projects?view=cards&mine=mine").text
    toolbar = re.search(r'<form class="ui-toolbar".*?</form>', page, re.S)[0]
    assert 'onchange="this.submit()"' in toolbar
    assert '<input type="hidden" name="view" value="cards">' in toolbar  # Kept while clicking mine/view.
    assert '<input type="hidden" name="mine" value="mine">' in toolbar  # Kept while clicking view/status/sort/search.
    assert 'name="mine" value="mine" aria-pressed="true"' in toolbar
    # The hidden mine input disappears once the scope is all (nothing to carry) and while own-only pins it.
    assert '<input type="hidden" name="mine"' not in ui.get("/projects?view=cards").text
    with db.session() as s:
        s.execute(text("UPDATE actors SET show_all_projects=0 WHERE name='admin'"))
        s.commit()
    assert '<input type="hidden" name="mine"' not in ui.get("/projects?status=all").text
    # The search field, the status and the sort selects submit their value on change.
    for name in ("q", "status", "sort"):
        assert f'name="{name}"' in toolbar
    assert _options(page, "sort") == {"activity", "key", "open"}


def test_projects_list_keeps_sort_with_current_project(env, ui):  # noqa: F811
    """The list view keeps the chosen sort even when the quick capture project is first alphabetically.

    Only the cards view leads with the "Current" project; the list used to be reordered too and so ignored `sort`.
    """
    mkproject(env, key="AAA")  # First key – also the quick capture default, i.e. the "Current" project.
    mkproject(env, key="ZZZ", name="Letztes Projekt")
    page = ui.get("/projects?status=all&sort=key").text
    keys = re.findall(r'<span class="ui-pill ui-pill-mono">([A-Z]+)</span>', _rows(page))
    assert keys == ["AAA", "ZZZ"]
    assert "Aktuell" in _row(page, "AAA")
    keys = re.findall(r'<span class="ui-pill ui-pill-mono">([A-Z]+)</span>',
                      _rows(ui.get("/projects?status=all&sort=key&mine=mine").text))
    assert keys == ["AAA", "ZZZ"]  # Mine scope: same order, the current project is not moved up.
    cards = _cards(ui.get("/projects?status=all&view=cards").text)
    assert "ui-card-current" in cards[0] and "/projects/AAA" in cards[0]  # Cards still lead with "Current".


def test_projects_status_filter_and_owner_scope(env, ui):  # noqa: F811
    """Status filter (paused/archived) and the profile switch "Show all projects" off (always own projects)."""
    mkproject(env, key="KET")
    mkproject(env, key="ZWEI", name="Zweites Projekt", status="paused")
    assert "/projects/ZWEI" not in ui.get("/projects").text
    assert "/projects/ZWEI" in ui.get("/projects?status=paused").text
    assert "/projects/KET" not in ui.get("/projects?status=paused").text
    assert "/projects/KET" not in ui.get("/projects?status=archived").text
    with db.session() as s:  # Profile switch "Show all projects" off: pin to Mine, hide "All projects".
        s.execute(text("UPDATE actors SET show_all_projects=0 WHERE name='admin'"))
        s.commit()
    page = ui.get("/projects?status=all&mine=all").text  # ?mine=all is ignored while the switch is off.
    assert "/projects/KET" in page and "/projects/ZWEI" in page  # Admin owns both.
    assert 'name="mine" value="mine" aria-pressed="true"' in page


def test_projects_page_without_projects(env, ui):  # noqa: F811
    """Empty state with the action button; no table and no card grid."""
    page = ui.get("/projects?status=archived").text
    assert "Keine Projekte gefunden." in page and "ui-empty" in page
    assert "ui-table-cards" not in page and "ui-card-grid" not in page
    assert "+ Neues Projekt" in page


@pytest.mark.parametrize("lang,notice,link", [
    ("en", "You are not the owner of any project yet, and only your own projects are shown.",
     "Turn on “Show all projects” in your profile"),
    ("de", "Du bist noch Owner keines Projekts, und angezeigt werden nur deine eigenen Projekte.",
     "Im Profil „Alle Projekte anzeigen“ einschalten"),
])
def test_projects_owner_notice_uses_unfiltered_ownership(env, ui, lang, notice, link):  # noqa: F811
    ui.cookies.set("tm_lang", lang)
    page = ui.get("/projects").text
    assert notice in page
    assert f'<a href="/profile#display">{link}</a>' in page
    # Owning even an archived project suppresses the notice, despite empty status/search results.
    mkproject(env, key="KET", status="archived")
    assert notice not in ui.get("/projects").text
    assert notice not in ui.get("/projects?status=all&q=missing").text
    assert notice not in ui.get("/projects?status=all").text
    with db.session() as s:
        s.execute(text("UPDATE projects SET owner_id=(SELECT id FROM actors WHERE name='claude-code')"))
        s.commit()
    assert notice in ui.get("/projects").text
    with db.session() as s:
        s.execute(text("UPDATE actors SET show_all_projects=1 WHERE name='admin'"))
        s.commit()
    assert notice not in ui.get("/projects?mine=mine").text


@pytest.mark.parametrize("lang,sort_label", [
    ("en", "Sort: last activity"), ("de", "Sortieren: letzte Aktivität"),
])
def test_projects_toolbar_has_accessible_inline_selects(env, ui, lang, sort_label):  # noqa: F811
    ui.cookies.set("tm_lang", lang)
    page = ui.get("/projects").text
    toolbar = re.search(r'<form class="ui-toolbar".*?</form>', page, re.S)[0]
    assert "ui-label" not in toolbar and "ui-field" not in toolbar
    for name in ("status", "sort"):
        assert re.search(rf'<select[^>]*name="{name}"[^>]*aria-label="[^"]+"', toolbar)
    assert f">{sort_label}</option>" in toolbar


@pytest.mark.parametrize("view", ["list", "cards"])
@pytest.mark.parametrize("statuses,widths", [
    ([], None), (["cancelled"], None), (["done"], [100, 0, 0]),
    (["done", "for_review", "in_progress", "defined", "cancelled"], [25, 25, 25]),
])
def test_projects_progress_segments(env, ui, view, statuses, widths):  # noqa: F811
    mkproject(env, key="KET")
    for status in statuses:
        task = mktask(env, title="Progress sample")
        with db.session() as s:
            s.execute(text("UPDATE tasks SET status=:status WHERE task_nr=:nr"),
                      {"status": status, "nr": task["task_nr"]})
            s.commit()
    page = ui.get(f"/projects?view={view}").text
    project = _row(page, "KET") if view == "list" else _cards(page)[0]
    assert 'class="ui-progress" role="img" aria-label=' in project
    segments = re.findall(r'class="ui-progress-(done|review|progress)" style="width: ([\d.]+)%"', project)
    if widths is None:
        assert segments == []
        assert 'aria-label="noch keine Tasks"' in project
    else:
        assert [name for name, _ in segments] == ["done", "review", "progress"]
        assert [float(width) for _, width in segments] == widths


@pytest.mark.parametrize("lang", ["de", "en"])
@pytest.mark.parametrize("width", [390, 768, 1440])
def test_projects_toolbar_layout(env, ui, server, browser, lang, width):  # noqa: F811
    mkproject(env, key="KET")
    context = browser.new_context(viewport={"width": width, "height": 900})
    try:
        context.add_cookies([{"name": "tm_lang", "value": lang, "url": server}])
        page = context.new_page()
        _login(page, server)
        page.goto(server + "/projects")
        _settle(page)
        controls = page.locator(".ui-toolbar .ui-seg, .ui-toolbar .ui-search, .ui-toolbar select")
        boxes = [control.bounding_box() for control in controls.all() if control.is_visible()]
        expected_height = 44 if width < 640 else 40
        assert all(abs(box["height"] - expected_height) < 1 for box in boxes), boxes
        if width == 1440:
            assert max(box["y"] for box in boxes) - min(box["y"] for box in boxes) < 1, boxes
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        assert page.locator(".ui-progress").evaluate("el => el.children.length") == 0
        assert page.locator(".ui-progress").evaluate("el => getComputedStyle(el).backgroundColor") == "rgb(232, 235, 239)"
    finally:
        context.close()


def test_projects_mobile_forces_list_even_with_view_cards(env, ui, server, browser):  # noqa: F811
    """On the phone (< 640 px) the list is always shown, even with a `?view=cards` URL; the toggle stays hidden."""
    mkproject(env, key="KET")
    context = browser.new_context(viewport={"width": 390, "height": 844})
    try:
        page = context.new_page()
        _login(page, server)
        page.goto(server + "/projects?view=cards")
        page.wait_for_url(server + "/projects", timeout=5000)  # The narrow-screen script drops `view=cards`.
        _settle(page)
        assert "ui-table-cards" in page.content() and "ui-card-grid" not in page.content()
        assert not page.locator(".ui-toolbar-view").is_visible()  # Toggle hidden on the phone.
        # On a desktop viewport the same URL still shows the card view.
        wide = browser.new_context(viewport={"width": 1440, "height": 900})
        try:
            wide.add_cookies([{"name": "tm_lang", "value": "en", "url": server}])
            wpage = wide.new_page()
            _login(wpage, server)
            wpage.goto(server + "/projects?view=cards")
            _settle(wpage)
            assert "ui-card-grid" in wpage.content() and "ui-table-cards" not in wpage.content()
        finally:
            wide.close()
    finally:
        context.close()


def test_projects_toggle_does_not_duplicate_view_param(env, ui, server, browser):  # noqa: F811
    """Switching back to the list from the cards view submits a clean `view` (no carried-over duplicate)."""
    mkproject(env, key="KET")
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    try:
        page = context.new_page()
        _login(page, server)
        page.goto(server + "/projects?view=cards")
        _settle(page)
        assert "ui-card-grid" in page.content()
        page.click("form.ui-toolbar button[name=view][value=list]")
        page.wait_for_url(lambda url: "view=cards" not in url, timeout=5000)
        assert "view=list" in page.url and "view=cards" not in page.url
        assert "ui-table-cards" in page.content()
    finally:
        context.close()
