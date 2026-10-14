"""New-task form: layout order, folded sections, both languages, inputs kept after a validation error."""
import re

import pytest

from app.errors import NotFound, Unprocessable
from app.i18n import use_language
from app.web.common import templates
from app.web.tasks_ui import _back_url, _error_section
from tests.test_i18n import _contexts
from tests.test_ui_cli import post, ui  # noqa: F401 (Fixtures)
from tests.test_workflow import env, gitea, mkproject  # noqa: F401 (Fixtures)


def _render(lang: str, **ctx) -> str:
    base = _contexts("task_new.html")[0]
    with use_language(lang):
        return templates.get_template("task_new.html").render({**base, **ctx})


def _form(**values) -> dict:
    return {"status": "draft", "prio": 3, **values}


def _pos(page: str, needle: str) -> int:
    assert needle in page, needle
    return page.index(needle)


def test_content_first_then_planning():
    page = _render("en", form=_form())
    page = page[page.index('id="new-task-form"'):]  # Without the quick capture in the header.
    order = ['name="project"', 'name="status"', 'name="title"', 'name="description"', 'name="acceptance_criteria"',
             "Planning & assignment", 'name="prio"', 'name="sprint"', 'name="assigned_to"', 'name="reviewer"',
             'name="due_date"', 'name="model_tier"', 'name="parent_task_nr"', 'name="attachments"',
             'name="create_spec_file"', "Create task"]
    positions = [_pos(page, n) for n in order]
    assert positions == sorted(positions)
    # Project and status share the first row; status appears only once.
    row = page[page.index('class="form-row cols-2-1"'):page.index('name="title"')]
    assert 'name="project"' in row and 'name="status"' in row
    assert page.count('name="status"') == 1
    assert re.search(r'<a href="/backlog" role="button" class="secondary">Cancel</a>\s*<button type="submit">', page)


def test_folded_sections_closed_on_an_empty_form():
    page = _render("en", form=_form())
    assert re.search(r'<details class="form-fold" id="new-task-model"\s*>', page)
    assert re.search(r'<details class="form-fold" id="new-task-links"\s*>', page)
    assert "Agent default" in page and "Parent task and dependencies" in page


@pytest.mark.parametrize("section,ctx", [
    ("model", {"error_section": "model"}),
    ("links", {"error_section": "links"}),
    ("links", {"form": _form(parent_task_nr="DEMO-0001")}),
    ("model", {"form": _form(model_tier="complex")}),
])
def test_folded_section_opens_for_errors_and_values(section, ctx):
    page = _render("en", **{"form": _form(), **ctx})
    assert re.search(rf'<details class="form-fold" id="new-task-{section}"\s*open>', page)


def test_model_summary_shows_the_choice():
    page = _render("en", form=_form(model_tier="complex", model="opus"))
    summary = re.search(r"data-model-summary[^>]*>([^<]*)<", page)[1]
    assert "opus" in summary and "·" in summary


@pytest.mark.parametrize("lang,texts", [
    ("en", ["New task", "Describe the task and plan its execution", "Project", "Status", "Title",
            'placeholder="What needs to be done?"', "Description", "Acceptance criteria", ">Edit<", ">Preview<",
            "Planning & assignment", "Priority", "3 – normal", "Model & execution", "Task links",
            "Attachments", "Cancel", "Create task"]),
    ("de", ["Neuer Task", "Aufgabe beschreiben und Ausführung planen", "Projekt", "Status", "Titel",
            'placeholder="Was soll erledigt werden?"', "Beschreibung", "Akzeptanzkriterien", ">Bearbeiten<",
            ">Vorschau<", "Planung & Zuständigkeit", "Priorität", "3 – normal", "Modell & Ausführung",
            "Standard des Agenten", "Verknüpfungen", "Anhänge", "Abbrechen", "Task anlegen",
            "Ziel, Kontext und gewünschtes Ergebnis beschreiben …", "- [ ] Überprüfbares Ergebnis definieren"]),
])
def test_both_languages(lang, texts):
    page = _render(lang, form=_form())
    missing = [t for t in texts if t not in page]
    assert not missing


def test_markdown_tabs_keep_the_textarea():
    page = _render("en", form=_form(description="**keep me**"))
    assert re.search(r'<textarea name="description" id="md-description"[^>]*>\*\*keep me\*\*</textarea>', page)
    assert 'hx-include="#md-description" hx-params="description"' in page
    assert 'role="tab"' in page and 'aria-controls="md-description-preview"' in page


def test_back_url():
    assert _back_url("http://tb.example.internal/kanban?agent=x") == "/kanban?agent=x"
    assert _back_url("http://tb.example.internal/tasks/new?project=KET", {"project": "KET"}) == "/projects/KET"
    assert _back_url(None, {"project": "KET", "parent": "KET-0001"}) == "/tasks/KET-0001"
    assert _back_url(None) == "/backlog"
    assert _back_url("/backlog") == "/backlog"
    assert _back_url("javascript:alert(1)") == "/backlog"


def test_error_section():
    assert _error_section(Unprocessable("x", code="model_not_allowed"), "", "") == "model"
    assert _error_section(NotFound("Task 'KET-0009' not found"), "", "ket-0009") == "links"
    assert _error_section(NotFound("Task 'KET-0009' not found"), "", "") is None
    assert _error_section(Unprocessable("Title is required"), "KET-0001", "") is None


@pytest.mark.db
def test_validation_error_keeps_input_and_opens_section(env, ui):  # noqa: F811
    mkproject(env)
    r = post(ui, "/tasks/new", project="KET", title="Behalten", description="Beschreibung bleibt",
             acceptance_criteria="- [ ] bleibt", prio="2", depends_on="KET-0999", create_spec_file="1",
             back="/kanban")
    assert r.status_code == 422
    page = r.text
    assert 'value="Behalten"' in page and "Beschreibung bleibt</textarea>" in page and "- [ ] bleibt</textarea>" in page
    assert re.search(r'<option value="2" selected>', page) and 'value="KET-0999"' in page
    assert re.search(r'name="create_spec_file" value="1" checked', page)
    assert re.search(r'id="new-task-links"\s*open>', page) and not re.search(r'id="new-task-model"\s*open>', page)
    assert 'href="/kanban" role="button"' in page and 'name="back" value="/kanban"' in page

    r = post(ui, "/tasks/new", project="KET", title="Modell", assigned_to="claude-code", model="haiku")
    assert r.status_code == 422 and re.search(r'id="new-task-model"\s*open>', r.text)

    r = post(ui, "/tasks/new", project="KET", title="Geht", prio="3", status="draft")
    assert r.status_code == 200 and re.search(r"/tasks/KET-\d+$", str(r.url))
