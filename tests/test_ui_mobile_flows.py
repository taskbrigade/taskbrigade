"""Phone flows of the UI redesign at 390 px (Playwright): sheets and bars that the static page checks of
`test_ui_screens.py` cannot see because they open only on a tap.

Same setup as `test_ui_screens.py` (demo data, uvicorn on a free port, `TBR_PLAYWRIGHT_WS` for a remote browser).
"""
import pytest

from tests.test_ui_cli import ui  # noqa: F401 (Fixture)
from tests.test_ui_screens import VIEWPORTS, _context, _login, _seed, _settle, browser, server  # noqa: F401
from tests.test_workflow import env, gitea  # noqa: F401 (Fixtures)

pytestmark = pytest.mark.db

# Box of an element in the viewport, or None when it is not shown.
BOX_JS = """sel => { const el = document.querySelector(sel); if (!el) return null;
  const r = el.getBoundingClientRect(), s = getComputedStyle(el);
  if (!r.width || !r.height || s.display === "none" || s.visibility === "hidden") return null;
  return {top: r.top, bottom: r.bottom, left: r.left, right: r.right, position: s.position}; }"""

# Is the centre of every visible control in `sel` the topmost element there (nothing, e.g. the tab bar, covers it)?
COVERED_JS = """sel => [...document.querySelectorAll(sel + " :is(a, button, input, select, textarea)")].filter(el => {
  const r = el.getBoundingClientRect(); if (!r.width || !r.height) return false;
  const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
  return !(hit && (hit === el || el.contains(hit) || hit.contains(el))); }).map(el => el.outerHTML.slice(0, 80))"""


@pytest.fixture
def phone(browser, env, ui, server):  # noqa: F811
    _seed(env, ui)
    ctx = _context(browser, server, VIEWPORTS["phone"], True)
    page = ctx.new_page()
    _login(page, server)
    yield page, server
    ctx.close()


def _tabs_top(page) -> float:
    box = page.evaluate(BOX_JS, ".bottom-tabs")
    assert box and box["position"] == "fixed", "bottom tab bar not shown on the phone"
    return box["top"]


def test_more_sheet_and_quick_capture(phone):
    page, base = phone
    page.goto(base + "/backlog")
    _settle(page)
    tabs_top = _tabs_top(page)

    page.click(".bottom-tabs button[aria-controls=more-menu]")
    sheet = page.evaluate(BOX_JS, "#more-menu")
    assert sheet and sheet["position"] == "fixed" and sheet["bottom"] <= tabs_top + 1
    assert page.get_attribute(".bottom-tabs button[aria-controls=more-menu]", "aria-expanded") == "true"
    links = page.locator("#more-menu a").all_inner_texts()
    assert {"Projects", "Admin", "Profile"} <= set(links)
    assert not page.evaluate(COVERED_JS, "#more-menu")
    page.click("#more-menu .sheet-close")
    assert page.evaluate(BOX_JS, "#more-menu") is None

    page.click(".qc-toggle")
    sheet = page.evaluate(BOX_JS, "#quick-capture")
    assert sheet and sheet["position"] == "fixed" and sheet["bottom"] <= tabs_top + 1
    assert not page.evaluate(COVERED_JS, "#quick-capture")
    # Rows: title, labelled project select (full width, full "KEY – name"), create button; no sideways scroll.
    title = page.evaluate(BOX_JS, "#quick-capture input[name=title]")
    label = page.evaluate(BOX_JS, "#quick-capture .qc-project-label")
    select = page.evaluate(BOX_JS, "#quick-capture select[name=project]")
    submit = page.evaluate(BOX_JS, "#quick-capture .qc-submit")
    assert page.inner_text("#quick-capture .qc-project-label") == "Project"
    assert title["bottom"] <= label["top"] and label["bottom"] <= select["top"] and select["bottom"] <= submit["top"]
    assert abs(select["left"] - title["left"]) < 1 and abs(select["right"] - title["right"]) < 1
    assert page.evaluate(BOX_JS, "#quick-capture .qc-project-key") is None
    assert page.evaluate("document.documentElement.scrollWidth") == 390
    page.fill("#quick-capture input[name=title]", "Captured on the phone")
    page.click("#quick-capture .qc-submit")
    page.wait_for_load_state("networkidle")
    page.goto(base + "/backlog")
    assert page.locator("text=Captured on the phone").count() >= 1


def test_kanban_status_tabs_and_move_sheet(phone):
    page, base = phone
    page.goto(base + "/kanban")
    _settle(page)
    # First visit: the board opens on the first column ("Draft"), which the tabs mark as current.
    current = page.get_attribute(".kanban-tabs button[aria-current=true]", "data-column")
    assert current == "draft"
    col = page.evaluate(BOX_JS, f'.kanban-col[data-column="{current}"]')
    box = page.evaluate(BOX_JS, ".kanban-scroll")
    assert col and abs(col["left"] - box["left"]) < 2, f"column {current} not in view: {col} vs {box}"

    page.click('.kanban-tabs button[data-column="draft"]')
    page.wait_for_function("""() => { const c = document.querySelector('.kanban-col[data-column="draft"]');
      return Math.abs(c.getBoundingClientRect().left - document.querySelector('.kanban-scroll').getBoundingClientRect().left) < 2; }""")
    page.wait_for_selector('.kanban-tabs button[data-column="draft"][aria-current=true]')

    summary = page.locator('.kanban-col[data-column="draft"] details.card-move > summary').first
    assert summary.get_attribute("aria-label") and summary.inner_text().strip() == ""  # icon only, no stray chars
    summary.click()
    page.wait_for_selector(".card-move-list [data-move-to]")
    assert page.locator("details.card-move[open] .card-move-open").count() == 1
    sheet = page.evaluate(BOX_JS, "details.card-move[open] .card-move-list")
    assert sheet and sheet["position"] == "fixed"
    assert not page.evaluate(COVERED_JS, "details.card-move[open] .card-move-list"), "move sheet covered"


def test_kanban_opens_on_col_parameter_and_planning_segments_scroll(phone):
    page, base = phone
    page.goto(base + "/kanban?col=for_review")
    _settle(page)
    assert page.get_attribute(".kanban-tabs button[aria-current=true]", "data-column") == "for_review"
    col = page.evaluate(BOX_JS, '.kanban-col[data-column="for_review"]')
    assert abs(col["left"] - page.evaluate(BOX_JS, ".kanban-scroll")["left"]) < 2

    page.goto(base + "/planning")
    _settle(page)
    if page.locator(".plan-segments").count():
        tops = page.evaluate("[...document.querySelectorAll('.plan-segments > a')].map(a => a.getBoundingClientRect().top)")
        assert len(set(round(t) for t in tops)) == 1, f"segmented filter wraps: {tops}"


def test_save_bars_sit_above_the_tab_bar(phone):
    page, base = phone
    page.set_viewport_size({"width": 390, "height": 560})  # short screen: the form is taller, the bar sticks
    page.goto(base + "/admin/settings")
    _settle(page)
    tabs_top = _tabs_top(page)
    page.evaluate("window.scrollTo(0, 0)")
    assert page.evaluate("document.documentElement.scrollHeight > innerHeight + 100"), "settings page too short"
    bar = page.evaluate(BOX_JS, "[data-savebar]")
    assert bar and bar["bottom"] <= tabs_top + 1, f"settings save bar {bar} under the tab bar at {tabs_top}"

    page.goto(base + "/profile")
    _settle(page)
    bar = page.evaluate(BOX_JS, ".pf-actions")
    assert bar and bar["bottom"] <= _tabs_top(page) + 1


def test_user_panel_is_a_full_screen_sheet(phone):
    page, base = phone
    page.goto(base + "/admin/users")
    _settle(page)
    assert page.evaluate("getComputedStyle(document.body).overflow") != "hidden", "page scroll locked without panel"
    page.goto(base + "/admin/users?detail=codex")
    _settle(page)
    panel = page.evaluate(BOX_JS, "#user-panel")
    assert panel and panel["position"] == "fixed" and panel["top"] <= 0.5 and panel["right"] >= 389.5
    small = page.evaluate("""() => [...document.querySelectorAll('#user-panel :is(input:not([type=hidden]):not([type=checkbox]):not([type=radio]), select, textarea)')]
      .filter(el => el.offsetParent && parseFloat(getComputedStyle(el).fontSize) < 16).map(el => el.name || el.id)""")
    assert not small, f"inputs under 16 px in the user panel: {small}"


@pytest.mark.parametrize("label", ["tablet", "desktop"])
def test_header_and_inbox_answer_on_wider_screens(browser, env, ui, server, label):  # noqa: F811
    _seed(env, ui)
    ctx = _context(browser, server, VIEWPORTS[label], False)
    try:
        page = ctx.new_page()
        _login(page, server)
        _settle(page)
        # The capture bar is always shown here; its phone toggle and sheet close button are not.
        assert page.evaluate(BOX_JS, "#quick-capture")
        assert page.evaluate(BOX_JS, ".qc-toggle") is None and page.evaluate(BOX_JS, "#quick-capture .sheet-close") is None
        # Question card: the answer field takes the row, "Send" stays inside the card.
        card = page.evaluate(BOX_JS, ".action-card.question")
        field = page.evaluate(BOX_JS, ".action-card.question textarea")
        send = page.evaluate(BOX_JS, ".action-card.question button[type=submit]")
        assert field["right"] - field["left"] > 200, f"answer field squeezed: {field}"
        assert send["right"] <= card["right"], f"Send button {send} sticks out of the card {card}"
    finally:
        ctx.close()


def test_quick_capture_project_select_on_desktop(browser, env, ui, server):  # noqa: F811
    from tests.test_workflow import mkproject

    _seed(env, ui)
    mkproject(env, key="WEB", mode="none", status="idea")
    ctx = _context(browser, server, VIEWPORTS["desktop"], False)
    try:
        page = ctx.new_page()
        _login(page, server)
        page.goto(server + "/backlog")
        _settle(page)
        # Own control next to the title field: border, radius, 36 px, key shown in mono, name in the list.
        field = page.evaluate(BOX_JS, "#quick-capture .qc-input")
        select = page.evaluate(BOX_JS, "#quick-capture select[name=project]")
        assert field and select and select["left"] - field["right"] >= 5, f"select not separate: {field} {select}"
        style = page.evaluate("""() => { const s = getComputedStyle(document.querySelector('#quick-capture select'));
          return [s.borderTopWidth, s.borderTopColor, s.borderTopLeftRadius, s.height, s.backgroundColor]; }""")
        assert style == ["1px", "rgb(214, 218, 224)", "8px", "36px", "rgb(255, 255, 255)"], style
        assert page.inner_text("#quick-capture .qc-project-key") == "KET"
        assert "mono" in page.evaluate("getComputedStyle(document.querySelector('.qc-project-key')).fontFamily").lower()
        assert page.locator("#quick-capture option[value=WEB]").inner_text() == "WEB – Projekt WEB"
        # Keyboard: N focuses the title, Tab reaches the select; a new project updates key and tooltip.
        page.keyboard.press("n")
        assert page.evaluate("document.activeElement.name") == "title"
        page.keyboard.press("Tab")
        assert page.evaluate("document.activeElement.name") == "project"
        page.select_option("#quick-capture select[name=project]", "WEB")
        assert page.inner_text("#quick-capture .qc-project-key") == "WEB"
        assert page.get_attribute("#quick-capture .qc-project", "title") == "Projekt WEB"
        # Enter in the title field creates the task in the chosen project.
        page.fill("#quick-capture input[name=title]", "Captured for WEB")
        with page.expect_navigation():
            page.press("#quick-capture input[name=title]", "Enter")
        assert page.url.startswith(server + "/tasks/WEB-"), page.url
    finally:
        ctx.close()
