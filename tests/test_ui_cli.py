"""Step 4: UI pages, Kanban drag and drop, `tm` CLI, and approval workflow (spec section 17)."""
import re

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from app.main import app
from cli import client as client_mod
from cli import tm
from tests.conftest import login
from tests.gitea_fake import SERVICE_USER
from tests.test_workflow import SHA, env, gitea, mkproject, mktask  # noqa: F401 (Fixtures)

pytestmark = pytest.mark.db
runner = CliRunner()


@pytest.fixture
def ui(env):  # noqa: F811
    with TestClient(app) as c:
        assert login(c).status_code == 303  # Bootstrap admin → admin actor.
        c.csrf = c.get("/backlog").text.split('"X-CSRF-Token": "')[1].split('"')[0]
        yield c


@pytest.fixture
def tm_as(monkeypatch):
    """Run `tm` against the in-process app (calls come from localhost)."""
    def fake_http_client(base_url, headers, **_):
        return TestClient(app, base_url=base_url, headers=headers, client=("127.0.0.1", 50000))

    monkeypatch.setattr(client_mod.httpx, "Client", fake_http_client)
    monkeypatch.setenv("TBR_URL", "http://testserver")

    def run(token_header: dict, *args, input=None):
        monkeypatch.setenv("TBR_TOKEN", token_header["Authorization"].split()[1])
        return runner.invoke(tm.app, list(args), input=input)
    return run


def post(ui, url, **data):
    return ui.post(url, data={"csrf_token": ui.csrf, **data}, follow_redirects=True)


# --- Approval workflow ------------------------------------------------------------------------

def test_acceptance_workflow(env, ui, tm_as):  # noqa: F811
    """Create task via API → `tm claim` → `tm ask` → answer in inbox → claim again →
    `tm submit` → PR link in UI → approval merges to main → done with complete history."""
    g = env["gitea"]
    mkproject(env)
    nr = mktask(env, title="Login bauen", assigned_to="claude-code", acceptance_criteria="- Login geht")["task_nr"]

    r = tm_as(env["claude"], "claim")
    assert r.exit_code == 0, r.output
    assert f"# {nr} – Login bauen" in r.stdout and "- Login geht" in r.stdout
    branch = re.search(r"Branch: `([^`]+)`", r.stdout)[1]

    r = tm_as(env["claude"], "ask", nr, "Passwort oder OAuth?")
    assert r.exit_code == 0 and "questions" in r.stdout

    inbox = ui.get("/").text
    assert nr in inbox and "Passwort oder OAuth?" in inbox
    r = ui.post(f"/inbox/{nr}/answer", data={"body": "Passwort reicht."},
                headers={"X-CSRF-Token": ui.csrf, "HX-Request": "true"})
    assert "beantwortet" in r.text

    assert tm_as(env["claude"], "claim").exit_code == 0
    r = tm_as(env["claude"], "show", nr)
    assert "### answer from admin" in r.stdout and "Passwort reicht." in r.stdout

    g.push_branch(SERVICE_USER, "ket", branch)
    r = tm_as(env["claude"], "submit", nr, "--branch", branch, "--sha", SHA, "--summary", "Login implementiert")
    assert r.exit_code == 0, r.output
    detail = ui.get(f"/tasks/{nr}").text
    pr_url = g.repo(SERVICE_USER, "ket").pulls[1]["html_url"]
    assert pr_url in detail and "Login implementiert" in detail

    r = ui.post(f"/inbox/{nr}/approve", data={"body": ""}, headers={"X-CSRF-Token": ui.csrf, "HX-Request": "true"})
    assert "gemerged und erledigt" in r.text
    assert g.repo(SERVICE_USER, "ket").pulls[1]["merged"]

    detail = ui.get(f"/tasks/{nr}").text
    assert 'st-done' in detail
    history = detail.split("Historie")[1]
    for step in ("created", "assigned", "defined → in_progress", "in_progress → questions", "questions → defined",
                 "pr_created", "in_progress → for_review", "merged", "for_review → done"):
        assert step in history, step
    assert tm_as(env["claude"], "claim").exit_code == 3  # Nothing remains.


# --- tm CLI -----------------------------------------------------------------------------------

def test_tm_commands(env, tm_as):  # noqa: F811
    mkproject(env)
    env["c"].patch("/api/v1/admin/actors/hermes", headers=env["admin"], json={"role": "orchestrator"})
    r = tm_as(env["hermes"], "create", "--project", "KET", "--title", "Teil 1", "--assign", "claude-code", "--prio", "2",
              "--description", "Teilaufgabe", "--acceptance", "- erledigt")
    assert r.exit_code == 0 and r.stdout.strip() == "KET-0001"
    r = tm_as(env["claude"], "list")
    assert "KET-0001" in r.stdout and "P2" in r.stdout
    r = tm_as(env["claude"], "claim", "--json")
    assert r.exit_code == 0 and '"status": "in_progress"' in r.stdout
    assert "Lease of KET-0001" in tm_as(env["claude"], "heartbeat", "KET-0001").stdout
    assert tm_as(env["claude"], "comment", "KET-0001", "-", input="Zwischenstand\n").exit_code == 0
    r = tm_as(env["claude"], "release", "KET-0001", "--reason", "blockiert")
    assert "defined" in r.stdout
    r = tm_as(env["claude"], "show", "KET-0001", "--json")
    assert '"Zwischenstand' in r.stdout
    r = tm_as(env["hermes"], "claim", "--task", "KET-0001")
    assert r.exit_code == 1 and "403" in r.output
    r = tm_as(env["claude"], "submit", "KET-0001", "--branch", "task/x", "--sha", SHA, "--summary", "x")
    assert r.exit_code == 1 and "invalid_transition" in r.output


def test_tm_orchestrator_due_update_cancel_reset(env, tm_as):  # noqa: F811
    """Due date on creation; tm update/cancel/reset permissions and display in show/list."""
    mkproject(env)
    env["c"].patch("/api/v1/admin/actors/hermes", headers=env["admin"], json={"role": "orchestrator"})
    base = ["--project", "KET", "--description", "Teilaufgabe", "--acceptance", "- erledigt"]

    r = tm_as(env["hermes"], "create", *base, "--title", "Mit Datum", "--assign", "claude-code", "--due", "2026-10-31")
    assert r.exit_code == 0 and r.stdout.strip() == "KET-0001" and "Warnung" not in r.output
    r = tm_as(env["hermes"], "create", *base, "--title", "Ohne Datum", "--assign", "openclaw-agent")
    assert r.exit_code == 0 and "KET-0002" in r.stdout
    assert "Warning" not in r.output  # Due date is informational and does not block claiming.
    assert tm_as(env["hermes"], "create", *base, "--title", "x", "--due", "31.10.2026").exit_code == 2

    r = tm_as(env["claude"], "show", "KET-0001")
    assert "- Due: 2026-10-31" in r.stdout and "- Last status change: created as defined" in r.stdout
    r = tm_as(env["claude"], "list", "--due")
    assert "2026-10-31" in r.stdout

    # Incorrect assignment: reassign instead of copying.
    r = tm_as(env["hermes"], "update", "KET-0002", "--assign", "claude-code", "--due", "2026-11-01", "--prio", "1",
              "--title", "Neu", "--description", "Neu beschrieben", "--acceptance", "- neu")
    assert r.exit_code == 0, r.output
    assert "assigned_to=claude-code" in r.stdout and "due_date=2026-11-01" in r.stdout
    t = env["c"].get("/api/v1/tasks/KET-0002", headers=env["admin"]).json()
    assert (t["assigned_to"], t["due_date"], t["prio"], t["title"], t["description"], t["acceptance_criteria"]) == \
        ("claude-code", "2026-11-01", 1, "Neu", "Neu beschrieben", "- neu")
    assert tm_as(env["hermes"], "update", "KET-0002", "--due", "none").exit_code == 0
    assert env["c"].get("/api/v1/tasks/KET-0002", headers=env["admin"]).json()["due_date"] is None

    r = tm_as(env["hermes"], "update", "KET-0002", "--prio", "2", "--version", "1")
    assert r.exit_code == 1 and "version_conflict" in r.output and "Version conflict" in r.output
    assert tm_as(env["hermes"], "update", "KET-0002").exit_code == 2  # No changes specified.

    r = tm_as(env["hermes"], "create", *base, "--title", "Entwurf", "--draft", "--assign", "codex", "--due",
              "2026-12-01")
    assert r.stdout.strip() == "KET-0003" and "draft" in r.output

    # Worker receives a clear 403 message.
    for args in (["update", "KET-0002", "--prio", "5"], ["cancel", "KET-0002", "--reason", "x"],
                 ["reset", "KET-0003"]):
        r = tm_as(env["claude"], *args)
        assert r.exit_code == 1 and "403" in r.output and "No permission" in r.output, args

    # Orchestrator (not assignee) publishes a draft via reset and cancels it via cancel.
    r = tm_as(env["hermes"], "reset", "KET-0003", "--reason", "freigegeben")
    assert r.exit_code == 0 and "KET-0003: defined" in r.stdout
    r = tm_as(env["hermes"], "reset", "KET-0003")
    assert r.exit_code == 1 and "409" in r.output
    r = tm_as(env["hermes"], "cancel", "KET-0002", "--reason", "doppelt angelegt")
    assert r.exit_code == 0 and "KET-0002: cancelled" in r.stdout
    r = tm_as(env["hermes"], "show", "KET-0002")
    assert "doppelt angelegt" in r.stdout and "Last status change: defined → cancelled" in r.stdout
    r = tm_as(env["hermes"], "cancel", "KET-0002", "--reason", "nochmal")
    assert r.exit_code == 1 and "Not possible in the current state" in r.output


def test_tm_missing_config(monkeypatch, tmp_path):
    monkeypatch.delenv("TBR_URL", raising=False)
    monkeypatch.delenv("TBR_TOKEN", raising=False)
    monkeypatch.setattr(client_mod, "CONFIG_PATH", tmp_path / "fehlt.toml")
    monkeypatch.setattr(client_mod.load_config, "__defaults__", (tmp_path / "fehlt.toml",))
    assert runner.invoke(tm.app, ["claim"]).exit_code == 2


# --- UI pages --------------------------------------------------------------------------------

def test_pages_render(env, ui):  # noqa: F811
    mkproject(env)
    mktask(env, title="Sichtbar", assigned_to="hermes", prio=1)
    for url in ("/", "/backlog", "/kanban", "/projects", "/projects/KET", "/projects/new", "/tasks/new",
                "/tasks/KET-0001", "/admin/users/hermes"):
        r = ui.get(url)
        assert r.status_code == 200, (url, r.text[:300])
    assert "Sichtbar" in ui.get("/kanban").text
    assert "Projektzugriff" in ui.get("/admin/users/hermes").text


def test_backlog_filters_and_htmx_partial(env, ui):  # noqa: F811
    mkproject(env)
    mktask(env, title="Alpha", assigned_to="hermes")
    mktask(env, title="Beta")
    r = ui.get("/backlog?agent=none", headers={"HX-Request": "true", "HX-Target": "backlog-table"})
    assert "<html" not in r.text and "Beta" in r.text and "Alpha" not in r.text
    assert "Alpha" not in ui.get("/backlog?q=Bet").text
    r = ui.get("/backlog?sort=title&desc=1")
    assert r.text.index("Beta") < r.text.index("Alpha")


def test_markdown_is_sanitized(env, ui):  # noqa: F811
    evil = "<script>alert(1)</script> **fett** [x](javascript:alert(1))"
    mkproject(env)
    nr = mktask(env, description=evil)["task_nr"]
    env["c"].post(f"/api/v1/tasks/{nr}/comments", headers=env["hermes"], json={"body": evil})
    html = ui.get(f"/tasks/{nr}").text
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html  # Edit field and thread escaped.
    assert "<strong>fett</strong>" in html and 'href="javascript:' not in html  # Thread rendered.


def test_kanban_move(env, ui):  # noqa: F811
    mkproject(env)
    nr = mktask(env, assigned_to="claude-code")["task_nr"]
    h = {"X-CSRF-Token": ui.csrf}
    r = ui.post("/kanban/move", data={"task_nr": nr, "to": "for_review"}, headers=h)
    assert r.status_code == 409 and r.json()["can_override"]
    r = ui.post("/kanban/move", data={"task_nr": nr, "to": "cancelled"}, headers=h)
    assert r.status_code == 200 and 'data-status="cancelled"' in r.text
    nr2 = mktask(env, assigned_to="claude-code")["task_nr"]
    ui.post("/kanban/move", data={"task_nr": nr2, "to": "questions", "override": "true"}, headers=h)
    r = ui.post("/kanban/move", data={"task_nr": nr2, "to": "defined"}, headers=h)
    assert r.status_code == 422 and r.json()["needs_comment"]
    r = ui.post("/kanban/move", data={"task_nr": nr2, "to": "defined", "comment": "Antwort"}, headers=h)
    assert r.status_code == 200
    # Shared column "Rückfrage / Abgebrochen": its drop target is "aborted" (reason required as before).
    board = ui.get("/kanban").text
    assert "Rückfrage / Abgebrochen" in board and "Abgebrochen (Agent)</span> <small>" not in board
    assert 'data-status="aborted"' in board and 'data-status="questions"' not in board
    nr3 = mktask(env, assigned_to="claude-code")["task_nr"]
    ui.post("/kanban/move", data={"task_nr": nr3, "to": "in_progress", "override": "true"}, headers=h)
    r = ui.post("/kanban/move", data={"task_nr": nr3, "to": "aborted"}, headers=h)
    assert r.status_code == 422 and r.json()["needs_comment"]
    r = ui.post("/kanban/move", data={"task_nr": nr3, "to": "aborted", "comment": "Zu groß"}, headers=h)
    assert r.status_code == 200 and 'card-status st-aborted">Abgebrochen (Agent)<' in r.text
    r = ui.post("/kanban/move", data={"task_nr": nr3, "to": "defined"}, headers=h)  # Out again = reopen.
    assert r.status_code == 200 and 'data-status="defined"' in r.text


def test_task_create_edit_conflict_and_quick_capture(env, ui):  # noqa: F811
    mkproject(env)
    r = post(ui, "/tasks/new", project="KET", title="Über UI", prio="2", assigned_to="hermes",
             description="Text", acceptance_criteria="", due_date="2026-10-31", parent_task_nr="", depends_on="")
    assert r.url.path == "/tasks/KET-0001" and "Über UI" in r.text
    r = post(ui, "/tasks/KET-0001/edit", row_version="1", title="Neu", prio="1", assigned_to="hermes",
             description="", acceptance_criteria="", due_date="")
    assert "Gespeichert" in r.text
    r = post(ui, "/tasks/KET-0001/edit", row_version="1", title="Veraltet", prio="1", assigned_to="",
             description="", acceptance_criteria="", due_date="")
    assert r.status_code == 422 and "zwischenzeitlich geändert" in r.text
    r = post(ui, "/tasks/quick", title="Schnell notiert", project="KET")
    assert r.url.path == "/tasks/KET-0002"
    r = post(ui, "/tasks/KET-0002/comment", body="Hallo **Welt**")
    assert "<strong>Welt</strong>" in r.text
    r = post(ui, "/markdown-preview", description="# Titel")
    assert "<h1>Titel</h1>" in r.text


def test_project_ui_create_and_activate(env, ui):  # noqa: F811
    r = post(ui, "/projects/new", key="web", name="Web", status="active", privacy="cloud_ok", gitea_mode="create",
             gitea_owner=SERVICE_USER, gitea_repo="")
    assert r.url.path == "/projects/WEB" and "Projekt angelegt" in r.text
    assert "brain.md" in env["gitea"].repo(SERVICE_USER, "web").files
    r = post(ui, "/projects/new", key="IDEA", name="Idee", status="active", privacy="local_only", gitea_mode="none")
    assert "Idee aktivieren" in r.text
    r = post(ui, "/projects/IDEA/gitea", mode="create", activate="1", gitea_repo="")
    assert "Gitea-Setup ausgeführt" in r.text and "idea/" not in r.text
    assert ui.get("/projects/repos?q=we").text.count("<option") == 2


def _settings_form(page: str) -> tuple[set[str], dict[str, str]]:
    """Field names of the project settings form and the values the page shows (selected option, checkbox)."""
    form = page.split('action="/projects/KET/edit"')[1].split("</form>")[0]
    # Controls with a form="…" attribute (co-owner add/remove) belong to other forms.
    names = {m[1] for m in re.finditer(r'<(?:input|select|textarea|button)\b[^>]*?name="(\w+)"[^>]*>', form)
             if ' form="' not in m[0]} - {"csrf_token"}
    shown = dict(re.findall(r'name="(\w+)" value="([^"]*)"(?: required)?>', form))
    for name, block in re.findall(r'<select name="(\w+)">(.*?)</select>', form, re.S):
        chosen = [(re.search(r'value="([^"]*)"', attrs) or [None, text.strip()])[1]
                  for attrs, text in re.findall(r"<option([^>]*)>([^<]*)", block) if "selected" in attrs]
        shown[name] = chosen[0] if chosen else ""
    shown["serialize_reviews"] = "1" if re.search(r'name="serialize_reviews" value="1" checked', form) else ""
    shown["description"] = re.search(r'<textarea name="description"[^>]*>([^<]*)</textarea>', form, re.S)[1]
    return names, shown


def test_project_settings_tab_saves_every_field(env, ui):  # noqa: F811
    """Redesigned project page: every setting of the form is posted and stored; the page shows it again."""
    mkproject(env)
    assert env["c"].post("/api/v1/admin/actors", headers=env["admin"], json={
        "name": "anna", "display_name": "Anna", "actor_type": "human", "role": "member"}).status_code == 201
    env["c"].patch("/api/v1/projects/KET", headers=env["admin"], json={"co_owners": ["anna"]})
    page = ui.get("/projects/KET").text
    assert 'data-guard="project"' in page and 'data-tab="settings"' in page and 'id="recipients"' in page
    assert "data-copy=" in page and 'value="link"' in page  # Clone URL copy button, "Repeat setup" form.
    names, shown = _settings_form(page)
    assert names == {"name", "description", "owner", "status", "privacy", "default_reviewer", "serialize_reviews",
                     "data_four_eyes"}  # Co-owners have their own add/remove forms, not part of this form.
    assert shown["name"] == "Projekt KET" and shown["status"] == "active" and shown["data_four_eyes"] == "default"
    assert shown["serialize_reviews"] == "" and shown["default_reviewer"] == ""
    r = post(ui, "/projects/KET/edit", name="Projekt Neu", description="Worum es geht", owner=shown["owner"],
             status="paused", privacy="local_only", default_reviewer="none", serialize_reviews="1",
             data_four_eyes="off")
    assert r.status_code == 200 and "Gespeichert" in r.text
    info = env["c"].get("/api/v1/projects/KET", headers=env["admin"]).json()
    assert (info["name"], info["status"], info["privacy"]) == ("Projekt Neu", "paused", "local_only")
    assert info["co_owners"] == ["anna"]  # Saving the form leaves the co-owners untouched.
    assert info["serialize_reviews"] is True and info["data_four_eyes"] is False
    _names, shown = _settings_form(ui.get("/projects/KET").text)
    assert shown["description"] == "Worum es geht" and shown["status"] == "paused"
    assert shown["privacy"] == "local_only" and shown["serialize_reviews"] == "1" and shown["data_four_eyes"] == "off"
    # Unchecking the checkbox (not sent) switches it off again; four-eyes back to the admin default.
    post(ui, "/projects/KET/edit", name="Projekt Neu", status="active", privacy="cloud_ok", data_four_eyes="default")
    info = env["c"].get("/api/v1/projects/KET", headers=env["admin"]).json()
    assert info["serialize_reviews"] is False and info["data_four_eyes"] is None and info["status"] == "active"


def test_admin_project_access(env, ui):  # noqa: F811
    mkproject(env, privacy="local_only")
    mkproject(env, key="CLD", privacy="cloud_ok")
    r = post(ui, "/admin/users/claude-code/projects", member_CLD="1", perm_CLD="read", member_KET="1")
    assert r.status_code == 422 and "local_only" in r.text  # Forced checkbox rejected server-side.
    assert env["gitea"].repo(SERVICE_USER, "cld").collaborators == {}  # No partial save.
    r = post(ui, "/admin/users/claude-code/projects", member_CLD="1", perm_CLD="read")
    assert "Projektzugriff gespeichert" in r.text
    collab = env["gitea"].repo(SERVICE_USER, "cld").collaborators
    assert collab == {"claude-code-bot": "read"}
    assert env["gitea"].repo(SERVICE_USER, "ket").collaborators == {}  # local_only stays blocked.
    post(ui, "/admin/users/claude-code/projects")
    assert env["gitea"].repo(SERVICE_USER, "cld").collaborators == {}


def test_abort_via_cli_and_ui_reassign(env, ui, tm_as):  # noqa: F811
    mkproject(env)
    nr = mktask(env, assigned_to="claude-code")["task_nr"]
    tm_as(env["claude"], "claim")
    r = tm_as(env["claude"], "abort", nr, "--reason", "Kontextfenster zu klein")
    assert r.exit_code == 0 and "waiting for reassignment" in r.stdout
    assert "Neuzuweisung nötig" in ui.get("/").text and nr in ui.get("/").text
    board = ui.get("/kanban").text
    assert "Rückfrage / Abgebrochen" in board and 'card-status st-aborted">Abgebrochen (Agent)<' in board
    detail = ui.get(f"/tasks/{nr}").text
    assert "Der Agent hat abgebrochen" in detail
    row_version = re.search(r'name="row_version" value="(\d+)"', detail)[1]
    r = post(ui, f"/tasks/{nr}/edit", row_version=row_version, title="Login bauen", prio="3",
             assigned_to="hermes", description="", acceptance_criteria="", due_date="")
    assert "Gespeichert" in r.text and "st-defined" in r.text
    assert tm_as(env["hermes"], "claim").exit_code == 0


def test_draft_in_ui_and_cli(env, ui, tm_as):  # noqa: F811
    mkproject(env)
    r = post(ui, "/tasks/new", project="KET", title="Idee", prio="3", assigned_to="claude-code", description="",
             acceptance_criteria="", due_date="", parent_task_nr="", depends_on="")
    assert "st-draft" in r.text and "Freigeben (→ Definiert)" in r.text  # Form defaults to draft.
    assert "Noch nicht bereit zur Freigabe" in r.text and "Beschreibung ergänzen" in r.text
    assert 'data-rc-button disabled' in r.text  # Released only with description and criteria (state machine).
    nr = r.url.path.rsplit("/", 1)[1]
    assert tm_as(env["claude"], "claim").exit_code == 3  # Draft is not claimed.
    assert "Entwurf" in ui.get("/kanban").text
    r = post(ui, f"/tasks/{nr}/action", action="publish", body="")
    assert r.status_code == 422 and "fehlen" in r.text  # Cannot publish without description/criteria.
    # Save and publish in one step (form guard sends then_publish).
    rv = re.search(r'name="row_version" value="(\d+)"', ui.get(f"/tasks/{nr}").text)[1]
    r = post(ui, f"/tasks/{nr}/edit", row_version=rv, title="Idee", prio="3", assigned_to="claude-code",
             description="Ausgearbeitet", acceptance_criteria="- fertig", due_date="", then_publish="1")
    assert "Gespeichert und freigegeben" in r.text and "st-defined" in r.text
    assert tm_as(env["claude"], "claim").exit_code == 0
    r = post(ui, "/tasks/quick", title="Schnelle Notiz", project="KET")
    assert "st-draft" in r.text  # Quick capture creates drafts.
    env["c"].patch("/api/v1/admin/actors/hermes", headers=env["admin"], json={"role": "orchestrator"})
    r = tm_as(env["hermes"], "create", "--project", "KET", "--title", "Teilaufgabe", "--draft", "--json")
    assert r.exit_code == 0 and '"status": "draft"' in r.stdout


def test_unassigned_released_task_is_flagged(env, ui):  # noqa: F811
    """Published without an actor: show detail notice and inbox list; nobody else claims it."""
    mkproject(env)
    nr = mktask(env, title="Ohne Agent", status="draft")["task_nr"]  # Test default includes description/criteria.
    detail = ui.get(f"/tasks/{nr}").text
    assert "noch niemand zugewiesen" in detail and "Trotzdem freigeben?" in detail
    assert 'data-state="unassigned"' in detail and "data-rc-button disabled" not in detail
    post(ui, f"/tasks/{nr}/action", action="publish", body="")
    assert "wird von keinem Agenten abgeholt" in ui.get(f"/tasks/{nr}").text
    inbox = ui.get("/").text
    assert 'Freigegeben, aber niemandem zugewiesen <span class="count-pill">1</span>' in inbox and nr in inbox
    env["c"].patch(f"/api/v1/tasks/{nr}", headers=env["admin"], json={"assigned_to": "hermes"})
    assert "Freigegeben, aber niemandem zugewiesen" not in ui.get("/").text  # Empty notices are left out.


def test_merge_conflict_return_button_in_ui(env, ui):  # noqa: F811
    c, g = env["c"], env["gitea"]
    mkproject(env)
    nrs = []
    for _ in range(2):
        nr = mktask(env, assigned_to="claude-code")["task_nr"]
        branch = c.post("/api/v1/agents/me/claim", headers=env["claude"]).json()["branch_name"]
        g.push_branch(SERVICE_USER, "ket", branch)
        c.post(f"/api/v1/tasks/{nr}/submit", headers=env["claude"],
               json={"branch_name": branch, "commit_sha": SHA, "summary": "x"})
        nrs.append(nr)
    g.conflict = {"pr_files": ["brain.md"], "base_files": ["brain.md"]}
    # Detail page: failed approval shows a message and button.
    r = post(ui, f"/tasks/{nrs[0]}/action", action="approve", body="")
    assert r.status_code == 422 and "Merge-Konflikt mit main in: brain.md" in r.text
    assert 'value="reject_conflict"' in r.text and "Konflikt an claude-code zurückgeben" in r.text
    r = post(ui, f"/tasks/{nrs[0]}/action", action="reject_conflict", body="")
    assert "Konflikt-Anleitung" in r.text and "st-changes_requested" in r.text
    # Inbox (HTMX): failed approval shows a button in the item.
    r = ui.post(f"/inbox/{nrs[1]}/approve", data={"body": ""}, headers={"X-CSRF-Token": ui.csrf})
    assert r.status_code == 422 and f'hx-post="/inbox/{nrs[1]}/reject_conflict"' in r.text
    r = ui.post(f"/inbox/{nrs[1]}/reject_conflict", data={"body": "bitte lösen"}, headers={"X-CSRF-Token": ui.csrf})
    assert r.status_code == 200 and "Konflikt-Anleitung" in r.text
    t = c.get(f"/api/v1/tasks/{nrs[1]}", headers=env["admin"]).json()
    assert t["status"] == "changes_requested" and "bitte lösen" in t["comments"][-1]["body"]
