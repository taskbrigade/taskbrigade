"""Task detail/edit page (card layout like the new-task form): order of the editor, read-only project/status,
separate forms for saving and commenting, kept input and opened sections after errors, release check."""
import re

import pytest

from tests.test_owner import _ui_login, human
from tests.test_ui_cli import post, ui  # noqa: F401 (Fixtures)
from tests.test_workflow import claim, env, gitea, mkproject, mktask  # noqa: F401 (Fixtures)

pytestmark = pytest.mark.db


def _edit_form(html: str) -> str:
    return html.split('id="edit"', 1)[1].split("</form>", 1)[0]


def test_editor_order_and_read_only_project_and_status(env, ui):  # noqa: F811
    mkproject(env)
    nr = mktask(env, status="draft")["task_nr"]
    html = ui.get(f"/tasks/{nr}").text
    assert 'class="app-main"' in html and "Task bearbeiten" in html and 'aria-label="Brotkrumennavigation"' in html
    form = _edit_form(html)
    # Project and status first (shown, not editable), then title, description, criteria, then planning.
    marks = ['readonly class="static-field"', 'name="title"', 'name="description"', 'name="acceptance_criteria"',
             "Planung & Zuständigkeit", 'name="prio"', 'name="assigned_to"', 'name="due_date"', 'name="reviewer"',
             "Modell & Ausführung", "Verknüpfungen", "Abbrechen", "Änderungen speichern"]
    positions = [form.index(m) for m in marks]
    assert positions == sorted(positions), list(zip(marks, positions))
    assert 'name="project"' not in form and 'name="status"' not in form  # No new way to move or re-status a task.
    assert form.count('readonly class="static-field"') == 2 and "Projekt KET" in form and "Entwurf" in form
    # Markdown fields with "Edit / Preview" tabs (same partial as the new-task form).
    assert 'id="md-description-preview-tab"' in form and 'id="md-acceptance_criteria-preview-tab"' in form
    # "Cancel" only reloads the stored task; cancelling the task stays a separate, confirmed action.
    assert f'<a href="/tasks/{nr}" role="button" class="secondary outline" data-discard>' in form
    assert 'value="cancel"' not in form and 'value="cancel"' in html
    # Draft: parent and dependencies are editable, with the structure marker.
    assert 'name="structure" value="1"' in form and 'name="parent_task_nr"' in form


def test_comment_and_edit_are_separate_forms(env, ui):  # noqa: F811
    mkproject(env)
    nr = mktask(env)["task_nr"]
    html = ui.get(f"/tasks/{nr}").text
    form = _edit_form(html)
    assert f'action="/tasks/{nr}/comment"' not in form and 'name="body"' not in form
    discussion = html.split('id="discussion-heading"', 1)[1]
    assert f'action="/tasks/{nr}/comment"' in discussion.split("</form>", 1)[0]
    assert "Historie" in discussion and "Eintr" in discussion  # History tab follows the thread.
    # Released task: links are read-only (structure marker missing → server keeps them).
    assert 'name="structure"' not in form and "nur im Entwurf ändern" in form


def test_edit_error_keeps_planning_values_and_opens_model_section(env, ui):  # noqa: F811
    mkproject(env)
    nr = mktask(env, status="draft")["task_nr"]
    html = ui.get(f"/tasks/{nr}").text
    rv = re.search(r'name="row_version" value="(\d+)"', html)[1]
    r = post(ui, f"/tasks/{nr}/edit", row_version=rv, title="Neu", prio="2", assigned_to="", due_date="2026-12-01",
             description="Text", acceptance_criteria="- ok", model_tier="gigantic", model="", reviewer="none",
             structure="1", parent_task_nr="", depends_on="")
    assert r.status_code == 422
    form = _edit_form(r.text)
    assert '<details class="form-fold" id="edit-model" open>' in form
    assert '<details class="form-fold" id="edit-links" >' in form
    assert 'value="Neu"' in form and 'value="2026-12-01"' in form and "Text</textarea>" in form
    assert '<option value="2" selected>' in form


def test_edit_error_in_links_opens_links_section(env, ui):  # noqa: F811
    mkproject(env)
    nr = mktask(env, status="draft")["task_nr"]
    rv = re.search(r'name="row_version" value="(\d+)"', ui.get(f"/tasks/{nr}").text)[1]
    r = post(ui, f"/tasks/{nr}/edit", row_version=rv, title="x", prio="3", structure="1",
             parent_task_nr="KET-0999", depends_on="")
    assert r.status_code == 422
    form = _edit_form(r.text)
    assert '<details class="form-fold" id="edit-links" open>' in form and 'value="KET-0999"' in form


def test_release_check_lists_what_is_missing(env, ui):  # noqa: F811
    mkproject(env)
    nr = mktask(env, status="draft", description="", acceptance_criteria="")["task_nr"]
    html = ui.get(f"/tasks/{nr}").text
    check = html.split("data-release-check", 1)[1].split("</div>", 1)[0]
    assert 'data-state="blocked"' in check
    for item in ("assigned_to", "description", "acceptance_criteria"):
        assert f'<li data-missing="{item}">' in check
    assert "data-rc-button disabled" in html
    nr = mktask(env, status="draft", assigned_to="claude-code")["task_nr"]
    html = ui.get(f"/tasks/{nr}").text
    assert 'data-state="ready"' in html and "data-rc-button disabled" not in html and "data-unassigned" not in html


def test_running_task_keeps_assignee_for_managers_without_reassign_right(env, ui):  # noqa: F811
    mkproject(env)
    nr = mktask(env, assigned_to="claude-code")["task_nr"]
    assert claim(env).status_code == 200
    human(env, "olga", role="orchestrator", password="olga-password-123")
    orch = _ui_login("olga", "olga-password-123")
    html = orch.get(f"/tasks/{nr}").text
    form = _edit_form(html)
    assert 'name="assigned_to" disabled' in form and '<input type="hidden" name="assigned_to" value="claude-code">' in form
    rv = re.search(r'name="row_version" value="(\d+)"', html)[1]
    # The disabled select is not sent; the hidden field keeps the assignee, so other edits still save.
    r = post(orch, f"/tasks/{nr}/edit", row_version=rv, title="Umbenannt", prio="3", assigned_to="claude-code",
             description="Testbeschreibung", acceptance_criteria="- Test erfüllt", due_date="")
    assert "Gespeichert" in r.text, r.text[:2000]
    t = env["c"].get(f"/api/v1/tasks/{nr}", headers=env["admin"]).json()
    assert (t["title"], t["assigned_to"], t["status"]) == ("Umbenannt", "claude-code", "in_progress")


def test_read_view_counts_criteria_and_edit_save_bumps_version(env, ui):  # noqa: F811
    mkproject(env)
    nr = mktask(env, status="draft", description="See **this**.",
                acceptance_criteria="- [x] one\n- [ ] two\n- [x] <script>alert(1)</script> **fett**")["task_nr"]
    html = ui.get(f"/tasks/{nr}").text
    assert 'id="edit" class="collapsible">' in html  # Read view first; Edit opens the form.
    assert "<strong>this</strong>" in html and "2 / 3 erfüllt" in html
    criteria = html.split('class="criteria-list"', 1)[1].split("</ul>", 1)[0]
    assert "<script>" not in criteria and "alert(1)" in criteria and "<strong>fett</strong>" in criteria
    rv = int(re.search(r'name="row_version" value="(\d+)"', html)[1])
    r = post(ui, f"/tasks/{nr}/edit", row_version=str(rv), title="Gespeichert", prio="4",
             description="See **this**.", acceptance_criteria="- [x] one", due_date="")
    assert "Gespeichert" in r.text, r.text[:1500]
    t = env["c"].get(f"/api/v1/tasks/{nr}", headers=env["admin"]).json()
    assert (t["title"], t["prio"], t["row_version"]) == ("Gespeichert", 4, rv + 1)
    assert "1 / 1 erfüllt" in r.text


def test_detail_in_english(env, ui):  # noqa: F811
    mkproject(env)
    nr = mktask(env, status="draft", description="")["task_nr"]
    ui.cookies.set("tm_lang", "en")
    html = ui.get(f"/tasks/{nr}").text
    for text in ("Edit task", "Planning & assignment", "Model & execution", "Task links", "Cancel",
                 "Save changes", "Not ready for release yet", "Add a description", "Discussion", "History",
                 "Project & links", "More actions", "Duplicate", "Create follow-up task", "Properties",
                 "Activity", "Attachments"):
        assert text in html, text
