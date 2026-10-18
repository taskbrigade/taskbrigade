"""Workflow row actions with real desktop and phone dialogs, keyboard focus and navigation."""
import threading
import time

import pytest

from app.main import app
from app.models import WorkflowParam
from tests.test_ui_screens import _context, _free_port, _login, browser  # noqa: F401
from tests.test_workflow_engine import graph  # noqa: F401
from tests.test_workflows_ui import post, web  # noqa: F401

pytestmark = pytest.mark.db


@pytest.fixture
def overview_server(web):
    import uvicorn

    client, s, owner, agent, template, steps = web
    s.add(WorkflowParam(version_id=template.current_version_id, name="topic", param_type="string",
                        label="Topic", required=True))
    s.commit()
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", client
    server.should_exit = True
    thread.join(5)


@pytest.mark.parametrize("width", [390, 768, 1440])
def test_overview_actions_in_browser(browser, overview_server, width):  # noqa: F811
    base, client = overview_server
    ctx = _context(browser, base, (width, 844), width < 640)
    try:
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        _login(page, base)
        page.goto(base + "/workflows")
        row = page.locator(".wf-list tbody tr").first
        start = row.get_by_role("button", name="Start workflow", exact=True)
        more = row.get_by_role("button", name="More actions")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        if width < 640:
            assert start.bounding_box()["y"] > row.locator(".task-title").bounding_box()["y"]
            assert start.bounding_box()["height"] >= 44
        more.click()
        menu = page.locator(".wf-action-menu[open]")
        assert menu.is_visible()
        box = menu.bounding_box()
        assert 0 <= box["x"] and box["x"] + box["width"] <= width + 1
        if width < 640:
            assert abs(box["y"] + box["height"] - 844) < 2
        page.keyboard.press("Escape")
        assert more.evaluate("el => el === document.activeElement")
        more.click()
        menu.get_by_role("button", name="Rename", exact=True).click()
        dialog = page.locator(".wf-action-dialog[open]")
        dialog.get_by_label("Name", exact=True).fill("Renamed in browser")
        dialog.get_by_role("button", name="Save", exact=True).click()
        page.wait_for_url("**/workflows?msg=**")
        assert row.get_by_role("link", name="Renamed in browser", exact=True).is_visible()
        start.click()
        dialog = page.locator(".wf-action-dialog[open]")
        assert dialog.get_by_label("Topic", exact=True).get_attribute("required") is not None
        dialog.get_by_label("Topic", exact=True).fill("Browser topic")
        dialog.get_by_label("Token budget (optional)", exact=True).fill("5000")
        dialog.get_by_role("button", name="Start now", exact=True).click()
        page.wait_for_url("**/workflows?started=**")
        run_link = page.get_by_role("link", name="View run", exact=True)
        assert run_link.is_visible()
        run_path = run_link.get_attribute("href")
        more.click()
        page.locator(".wf-action-menu[open]").get_by_role("button", name="Delete", exact=True).click()
        dialog = page.locator(".wf-action-dialog[open]")
        assert dialog.get_by_text("Cancel active runs first", exact=True).is_visible()
        assert dialog.get_by_role("button", name="Delete", exact=True).is_disabled()
        page.keyboard.press("Escape")
        post(client, run_path + "/cancel")
        more.click()
        page.locator(".wf-action-menu[open]").get_by_role("button", name="Duplicate", exact=True).click()
        page.wait_for_url("**/workflows/DEMO/contentCopy/edit")
        page.goto(base + "/workflows")
        original = page.locator(".wf-list tbody tr").filter(has=page.get_by_role("link", name="Renamed in browser", exact=True))
        original.get_by_role("button", name="More actions").click()
        page.locator(".wf-action-menu[open]").get_by_role("button", name="Delete", exact=True).click()
        dialog = page.locator(".wf-action-dialog[open]")
        dialog.get_by_role("button", name="Delete", exact=True).click()
        page.wait_for_url("**/workflows?msg=**")
        assert page.get_by_text("Workflow deleted", exact=True).is_visible()
        assert page.get_by_role("link", name="Renamed in browser", exact=True).count() == 0
        assert page.get_by_role("link", name="Copy of Renamed in browser", exact=True).is_visible()
        page.goto(base + run_path)
        assert page.get_by_text("Browser topic", exact=False).count()
        assert not errors
    finally:
        ctx.close()
