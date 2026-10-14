"""Duplicating tasks into a new draft: service rules, API, UI, `tbr duplicate`, MCP `duplicate_task`."""
import logging

import pytest

from app.services import tasks as svc
from tests.gitea_fake import SERVICE_USER
from tests.test_model_choice import configure
from tests.test_owner import human
from tests.test_sprint_editor import login_as
from tests.test_sprints import mksprint, senv, sprint_url, task  # noqa: F401 (Fixtures)
from tests.test_sprints_cli import mcp_as  # noqa: F401 (Fixture)
from tests.test_ui_cli import post, tm_as, ui  # noqa: F401 (Fixtures)
from tests.test_workflow import SHA, claim, env, events, gitea, mkproject, mktask  # noqa: F401 (Fixtures)


def dup(e, nr, who="admin", **body):
    return e["c"].post(f"/api/v1/tasks/{nr}/duplicate", headers=e[who], json=body)


# --- Title (no DB) --------------------------------------------------------------------------------

@pytest.mark.parametrize("title,expected", [
    ("Login bauen", "(dupliziert) Login bauen"),
    ("(dupliziert) Login bauen", "(dupliziert) Login bauen"),
    ("(duplicate) Login bauen", "(dupliziert) Login bauen"),
    ("(dupliziert) (duplicate)  Login bauen", "(dupliziert) Login bauen"),
])
def test_duplicate_title_is_not_stacked(title, expected):
    assert svc.duplicate_title(title) == expected  # Tests run with UI_LANGUAGE=de.


def test_duplicate_title_english_and_length():
    from app import i18n

    with i18n.use_language("en"):
        assert svc.duplicate_title("(dupliziert) Login") == "(duplicate) Login"
    assert len(svc.duplicate_title("x" * 200)) == 200


# --- API ------------------------------------------------------------------------------------------

@pytest.mark.db
def test_duplicate_copies_definition_not_history(env):  # noqa: F811
    mkproject(env)
    configure(env)
    parent = mktask(env, title="Epic")["task_nr"]
    dep = mktask(env, title="Basis")["task_nr"]
    nr = mktask(env, title="Login bauen", assigned_to="claude-code", prio=2, due_date="2026-12-01",
                parent_task_nr=parent, depends_on=[dep], model_tier="complex", model="opus")["task_nr"]
    env["c"].post(f"/api/v1/tasks/{nr}/comments", headers=env["admin"], json={"body": "Ein Kommentar"})

    r = dup(env, nr)
    assert r.status_code == 201, r.text
    t = r.json()
    assert t["task_nr"] != nr and t["project"] == "KET" and t["status"] == "draft"
    assert t["title"] == "(dupliziert) Login bauen" and t["assigned_to"] is None
    assert (t["description"], t["acceptance_criteria"], t["prio"], str(t["due_date"])) == (
        "Testbeschreibung", "- Test erfüllt", 2, "2026-12-01")
    assert (t["model_tier"], t["model"]) == ("complex", "opus")  # Kept without an assignee.
    assert t["parent_task_nr"] == parent and [d["task_nr"] for d in t["dependencies"]] == [dep]
    assert t["duplicate_of"] == nr and t["follow_up_of"] is None
    assert t["comments"] == [] and t["branch_name"] is None and t["pr_url"] is None
    assert t["lease_expires_at"] is None and t["result_summary"] is None and t["open_points"] == []
    assert ("duplicated_from", None, nr) in events(env, t["task_nr"])
    origin = env["c"].get(f"/api/v1/tasks/{nr}", headers=env["admin"]).json()
    assert [d["task_nr"] for d in origin["duplicates"]] == [t["task_nr"]]
    assert origin["comments"][-1]["type"] == "system"
    assert origin["comments"][-1]["body"] == f"Dupliziert als {t['task_nr']}"

    # A duplicate of the duplicate: no second prefix.
    again = dup(env, t["task_nr"]).json()
    assert again["title"] == "(dupliziert) Login bauen" and again["duplicate_of"] == t["task_nr"]


@pytest.mark.db
def test_duplicate_works_in_every_status(env):  # noqa: F811
    mkproject(env)
    nr = mktask(env, assigned_to="claude-code")["task_nr"]
    branch = claim(env).json()["branch_name"]
    env["gitea"].push_branch(SERVICE_USER, "ket", branch)
    env["c"].post(f"/api/v1/tasks/{nr}/submit", headers=env["claude"],
                  json={"branch_name": branch, "commit_sha": SHA, "summary": "fertig", "open_points": ["Rest"]})
    t = dup(env, nr).json()  # for_review
    assert t["status"] == "draft" and t["branch_name"] is None and t["pr_url"] is None and t["open_points"] == []
    assert t["result_commit_sha"] is None and t["submitted_at"] is None
    env["c"].post(f"/api/v1/tasks/{nr}/transition", headers=env["admin"], json={"to": "cancelled", "comment": "x"})
    assert dup(env, nr).status_code == 201  # cancelled
    draft = mktask(env, status="draft")["task_nr"]
    assert dup(env, draft).json()["status"] == "draft"


@pytest.mark.db
def test_duplicate_assign_and_title(env, caplog):  # noqa: F811
    mkproject(env)
    configure(env, name="codex", models_allowed=["gpt-5.6-sol"])
    configure(env)
    nr = mktask(env, assigned_to="claude-code", model="opus")["task_nr"]
    with caplog.at_level(logging.INFO, logger="admin_audit"):
        r = dup(env, nr, assign="codex", title="Zweitmeinung: Login")
    t = r.json()
    assert r.status_code == 201 and t["assigned_to"] == "codex" and t["title"] == "Zweitmeinung: Login"
    assert t["status"] == "draft" and t["model"] is None  # Not on codex's allow list → dropped with an event.
    assert ("model_dropped", "opus", None) in events(env, t["task_nr"])
    audit = [r for r in caplog.records if getattr(r, "audit_action", None) == "task_duplicated"]
    assert audit and audit[0].task == nr and audit[0].duplicate == t["task_nr"]
    assert dup(env, nr, assign="claude-code").json()["model"] == "opus"
    assert dup(env, nr, assign="nobody-here").status_code == 422
    assert dup(env, "KET-9999").status_code == 404


@pytest.mark.db
def test_duplicate_permissions(senv):  # noqa: F811
    e = senv
    nr = mktask(e)["task_nr"]
    for who in ("claude", "hermes"):  # Workers
        r = dup(e, nr, who=who)
        assert r.status_code == 403, r.text
    assert dup(e, nr, who="alex").status_code == 403  # Owner of another project only.
    assert dup(e, nr, who="orch").status_code == 201
    own = mktask(e, project="OWN", who="alex")["task_nr"]
    assert dup(e, own, who="alex").status_code == 201  # Owner of the project.
    assert dup(e, own).status_code == 201  # Admin


@pytest.mark.db
def test_duplicate_local_only_rules(env):  # noqa: F811
    mkproject(env, key="LOC", privacy="local_only")
    nr = mktask(env, project="LOC", assigned_to="hermes")["task_nr"]
    r = dup(env, nr, assign="codex")  # Cloud agent: refused as on create.
    assert r.status_code in (403, 422), r.text
    assert dup(env, nr, assign="hermes").json()["assigned_to"] == "hermes"


@pytest.mark.db
def test_duplicate_keeps_open_sprint_only(senv):  # noqa: F811
    e = senv
    sp = mksprint(e)
    nr = mktask(e, sprint=sp["id"])["task_nr"]
    t = dup(e, nr).json()
    assert t["sprint"] == sp["id"] and t["status"] == "draft"
    closed = mksprint(e, name="Sprint 2")
    done = mktask(e, sprint=closed["id"])["task_nr"]
    assert e["c"].post(sprint_url("KET", closed["id"], "/release"), headers=e["admin"]).status_code == 200
    e["c"].post(f"/api/v1/tasks/{done}/transition", headers=e["admin"], json={"to": "cancelled", "comment": "x"})
    r = e["c"].post(sprint_url("KET", closed["id"], "/close"), headers=e["admin"], json={"detach": True})
    assert r.status_code == 200, r.text
    assert task(e, done)["sprint_status"] == "closed"
    assert dup(e, done).json()["sprint"] is None


# --- UI -------------------------------------------------------------------------------------------

@pytest.mark.db
def test_duplicate_from_detail_page_opens_edit_view(env, ui):  # noqa: F811
    mkproject(env)
    nr = mktask(env, title="Login bauen", assigned_to="claude-code")["task_nr"]
    page = ui.get(f"/tasks/{nr}").text
    assert f'action="/tasks/{nr}/duplicate"' in page and "Duplizieren" in page
    r = post(ui, f"/tasks/{nr}/duplicate", assign="codex")
    assert r.status_code == 200 and f"Dupliziert von {nr}" in r.text
    new_nr = str(r.url).rsplit("/", 1)[1].split("?")[0]
    assert r.url.fragment == "edit" and 'id="edit"' in r.text
    assert 'value="(dupliziert) Login bauen"' in r.text and 'name="parent_task_nr"' in r.text
    assert f'href="/tasks/{nr}"' in r.text and "Dupliziert von" in r.text
    t = env["c"].get(f"/api/v1/tasks/{new_nr}", headers=env["admin"]).json()
    assert t["assigned_to"] == "codex" and t["status"] == "draft"
    origin = ui.get(f"/tasks/{nr}").text
    assert "Duplikate" in origin and f'href="/tasks/{new_nr}"' in origin and f"Dupliziert als {new_nr}" in origin


@pytest.mark.db
def test_duplicate_button_hidden_without_rights(env):  # noqa: F811
    mkproject(env)
    human(env, "anna", password="anna-passwort-123")  # Member, owns no project.
    nr = mktask(env)["task_nr"]
    c = login_as("anna", "anna-passwort-123")
    try:
        assert f"/tasks/{nr}/duplicate" not in c.get(f"/tasks/{nr}").text
        r = post(c, f"/tasks/{nr}/duplicate")
        assert r.status_code == 422 and "duplizieren" in r.text
    finally:
        c.__exit__(None, None, None)
    assert task(env, nr)["duplicates"] == []


@pytest.mark.db
def test_draft_fields_editable_in_ui(env, ui):  # noqa: F811
    mkproject(env)
    a, b = mktask(env)["task_nr"], mktask(env)["task_nr"]
    nr = mktask(env, status="draft", title="Alt")["task_nr"]
    t = env["c"].get(f"/api/v1/tasks/{nr}", headers=env["admin"]).json()
    r = post(ui, f"/tasks/{nr}/edit", row_version=str(t["row_version"]), title="Neu", prio="1", description="D",
             acceptance_criteria="K", due_date="2026-11-30", structure="1", parent_task_nr=a, depends_on=f"{b}")
    assert r.status_code == 200, r.text
    t = env["c"].get(f"/api/v1/tasks/{nr}", headers=env["admin"]).json()
    assert (t["title"], t["prio"], t["description"], t["acceptance_criteria"]) == ("Neu", 1, "D", "K")
    assert t["parent_task_nr"] == a and [d["task_nr"] for d in t["dependencies"]] == [b]
    # Without the structure marker (e.g. a form of a non-draft task), parent and dependencies stay.
    r = post(ui, f"/tasks/{nr}/edit", row_version=str(t["row_version"]), title="Neu", prio="1")
    t = env["c"].get(f"/api/v1/tasks/{nr}", headers=env["admin"]).json()
    assert t["parent_task_nr"] == a and [d["task_nr"] for d in t["dependencies"]] == [b]
    # Clearing both; self references are refused.
    r = post(ui, f"/tasks/{nr}/edit", row_version=str(t["row_version"]), title="Neu", prio="1", structure="1",
             parent_task_nr="", depends_on="")
    t = env["c"].get(f"/api/v1/tasks/{nr}", headers=env["admin"]).json()
    assert t["parent_task_nr"] is None and t["dependencies"] == []
    r = post(ui, f"/tasks/{nr}/edit", row_version=str(t["row_version"]), title="Neu", prio="1", structure="1",
             depends_on=nr)
    assert r.status_code == 422 and "von sich selbst" in r.text
    r = env["c"].patch(f"/api/v1/tasks/{a}", headers=env["admin"], json={"parent_task_nr": nr})
    assert r.status_code == 200
    r = env["c"].patch(f"/api/v1/tasks/{nr}", headers=env["admin"], json={"parent_task_nr": a})
    assert r.status_code == 422  # a is already a subtask of nr.


# --- CLI and MCP ----------------------------------------------------------------------------------

@pytest.mark.db
def test_tbr_duplicate(senv, tm_as):  # noqa: F811
    e = senv
    nr = mktask(e, title="Login bauen")["task_nr"]
    r = tm_as(e["orch"], "duplicate", nr, "--assign", "codex")
    assert r.exit_code == 0, r.output
    new_nr = r.stdout.strip()
    t = task(e, new_nr)
    assert t["duplicate_of"] == nr and t["assigned_to"] == "codex" and t["title"] == "(dupliziert) Login bauen"
    assert "is a draft" in r.stderr
    r = tm_as(e["orch"], "duplicate", nr.lower(), "--title", "Eigener Titel", "--json")
    assert r.exit_code == 0 and '"title": "Eigener Titel"' in r.stdout
    r = tm_as(e["claude"], "duplicate", nr)
    assert r.exit_code == 1 and "403" in r.stderr


@pytest.mark.db
def test_mcp_duplicate_task(senv, mcp_as):  # noqa: F811
    e = senv
    nr = mktask(e, title="Login bauen")["task_nr"]
    out = mcp_as("orch").duplicate_task(nr, assign="codex")
    assert "as a draft from " + nr in out and "codex" in out and "(dupliziert) Login bauen" in out
    assert mcp_as("claude").duplicate_task(nr).startswith("Failed: 403")
