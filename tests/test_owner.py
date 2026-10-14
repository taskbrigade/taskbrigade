"""Project owner and 'member' role: multiple humans working with TaskBrigade."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import db
from app.main import app
from app.models import Actor, Task
from app.services.state_machine import RULES, check_allowed
from tests.conftest import TEST_DB_URL, bearer, login, make_token, requires_db
from tests.gitea_fake import SERVICE_USER
from tests.test_workflow import SHA, claim, env, gitea, mkproject, mktask  # noqa: F401 (Fixtures)

pytestmark = pytest.mark.db


def human(env, name, role="member", gitea_username=None, password=None):
    r = env["c"].post("/api/v1/admin/actors", headers=env["admin"], json={
        "name": name, "display_name": name.title(), "actor_type": "human", "role": role,
        **({"password": password} if password else {})})
    assert r.status_code == 201, r.text
    if gitea_username:
        with db.session() as s:
            s.query(Actor).filter_by(name=name).one().gitea_username = gitea_username
            s.commit()
    return bearer(make_token(name))


def submitted_task(env, project="KET"):
    c, g = env["c"], env["gitea"]
    nr = mktask(env, project=project, assigned_to="claude-code")["task_nr"]
    branch = claim(env).json()["branch_name"]
    g.push_branch(SERVICE_USER, project.lower(), branch)
    r = c.post(f"/api/v1/tasks/{nr}/submit", headers=env["claude"],
               json={"branch_name": branch, "commit_sha": SHA, "summary": "fertig"})
    assert r.status_code == 200, r.text
    return nr


# --- State machine permissions without a DB ---------------------------------------------------

def test_owner_has_admin_rights_in_own_project_only():
    owner = Actor(id=7, name="anna", actor_type="human", role="member")
    other = Actor(id=8, name="bob", actor_type="human", role="member")
    task = Task(status="for_review", assigned_to=3, created_by=1)
    check_allowed(RULES["approve"], task, owner, owner_id=7)
    check_allowed(RULES["reject_conflict"], task, owner, owner_id=7)
    with pytest.raises(Exception, match="nur Admins"):
        check_allowed(RULES["approve"], task, other, owner_id=7)
    check_allowed(RULES["reopen"], Task(status="aborted"), owner, owner_id=7)  # Otherwise human admins only.
    with pytest.raises(Exception):
        check_allowed(RULES["submit"], Task(status="in_progress", assigned_to=3), owner, owner_id=7)  # Assignee only.
    agent = Actor(id=7, name="x", actor_type="agent", role="worker")  # Agents never gain owner rights.
    with pytest.raises(Exception):
        check_allowed(RULES["approve"], task, agent, owner_id=7)


# --- Migration: existing projects get DEFAULT_PROJECT_OWNER or their creator ----------

def _migrate_old_projects(monkeypatch, default_owner: str) -> dict[str, str]:
    """Create schema through 0004, insert old projects, upgrade to head → {project: owner}."""
    from alembic import command
    from sqlalchemy import create_engine, inspect

    from app.config import get_settings
    from app.db import alembic_config
    from tests import conftest

    monkeypatch.setattr(get_settings(), "default_project_owner", default_owner)
    engine = create_engine(TEST_DB_URL)
    with engine.begin() as conn:
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for table in inspect(conn).get_table_names():
            conn.execute(text(f"DROP TABLE `{table}`"))
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
    conftest._schema_ready = False
    cfg = alembic_config()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0004")
        admin = conn.execute(text("SELECT id FROM actors WHERE name='admin'")).scalar()
        conn.execute(text("INSERT INTO actors (name, display_name, actor_type, role, runtime) "
                          "VALUES ('alex', 'Alex', 'human', 'admin', 'local')"))
        conn.execute(text("INSERT INTO actors (name, display_name, actor_type, agent_kind, role, runtime) "
                          "VALUES ('codex', 'codex', 'agent', 'codex', 'worker', 'cloud')"))
        codex = conn.execute(text("SELECT id FROM actors WHERE name='codex'")).scalar()
        conn.execute(text(f"INSERT INTO projects (project_key, name, created_by) VALUES "
                          f"('ALT', 'alt', {admin}), ('BOT', 'bot', {codex})"))
        command.upgrade(cfg, "head")
        owners = dict(conn.execute(text("SELECT p.project_key, a.name FROM projects p "
                                        "JOIN actors a ON a.id = p.owner_id")).all())
        role_type = conn.execute(text("SELECT COLUMN_TYPE FROM information_schema.columns WHERE "
                                      "table_schema = DATABASE() AND table_name='actors' AND column_name='role'")).scalar()
    engine.dispose()
    assert "member" in role_type
    return owners


@requires_db
def test_migration_sets_configured_owner(monkeypatch):
    assert _migrate_old_projects(monkeypatch, "alex") == {"ALT": "alex", "BOT": "alex"}


@requires_db
def test_migration_falls_back_to_creator(monkeypatch):
    assert _migrate_old_projects(monkeypatch, "") == {"ALT": "admin", "BOT": "codex"}


# --- API: projects, owner transfer, permissions ------------------------------------------------------

def test_member_creates_own_project_and_manages_only_that(env):
    c = env["c"]
    anna = human(env, "anna")
    bob = human(env, "bob")
    mkproject(env)  # KET belongs to admin.
    assert c.get("/api/v1/projects/KET", headers=env["admin"]).json()["owner"] == "admin"

    r = c.post("/api/v1/projects", headers=anna, json={"key": "ANNA", "name": "Annas Projekt",
                                                       "status": "idea", "gitea_mode": "none"})
    assert r.status_code == 201 and r.json()["owner"] == "anna"
    r = c.post("/api/v1/projects", headers=anna, json={"key": "X1", "name": "x", "owner": "bob"})
    assert r.status_code == 403  # Members create projects only for themselves.
    assert c.post("/api/v1/projects", headers=env["hermes"], json={"key": "X2", "name": "x"}).status_code == 403

    # Tasks: own project only.
    r = c.post("/api/v1/tasks", headers=anna, json={"project": "ANNA", "title": "Idee", "status": "draft"})
    assert r.status_code == 201
    assert c.post("/api/v1/tasks", headers=anna, json={"project": "KET", "title": "x",
                                                       "status": "draft"}).status_code == 403
    # Project edits: owner yes, other members no.
    assert c.patch("/api/v1/projects/ANNA", headers=anna, json={"name": "Neu"}).status_code == 200
    assert c.patch("/api/v1/projects/ANNA", headers=bob, json={"name": "Bob"}).status_code == 403
    assert c.patch("/api/v1/projects/KET", headers=anna, json={"name": "y"}).status_code == 403
    # Owner must be an active human.
    r = c.patch("/api/v1/projects/ANNA", headers=anna, json={"owner": "hermes"})
    assert r.status_code == 422 and "Mensch" in r.json()["error"]["message"]
    # Member role is for humans only.
    r = c.post("/api/v1/admin/actors", headers=env["admin"], json={
        "name": "botx", "display_name": "x", "actor_type": "agent", "agent_kind": "other", "role": "member"})
    assert r.status_code == 422


def test_owner_reviews_and_answers_in_own_project(env):
    c, g = env["c"], env["gitea"]
    anna = human(env, "anna", gitea_username="anna-git")
    bob = human(env, "bob")
    mkproject(env)
    nr = submitted_task(env)
    assert c.post(f"/api/v1/tasks/{nr}/review", headers=anna, json={"decision": "approve"}).status_code == 403

    # Admin transfers the project; its new owner becomes a collaborator and can approve.
    r = c.patch("/api/v1/projects/KET", headers=env["admin"], json={"owner": "anna"})
    assert r.status_code == 200 and r.json()["owner"] == "anna"
    assert g.repo(SERVICE_USER, "ket").collaborators.get("anna-git") == "write"
    assert c.post(f"/api/v1/tasks/{nr}/review", headers=bob, json={"decision": "approve"}).status_code == 403
    r = c.post(f"/api/v1/tasks/{nr}/review", headers=anna, json={"decision": "approve"})
    assert r.status_code == 200 and r.json()["status"] == "done"

    # Answer question and edit/reassign task: owner yes, other member no.
    nr2 = mktask(env, assigned_to="claude-code")["task_nr"]
    claim(env)
    c.post(f"/api/v1/tasks/{nr2}/ask", headers=env["claude"], json={"body": "Wie?"})
    assert c.post(f"/api/v1/tasks/{nr2}/answer", headers=bob, json={"body": "so"}).status_code == 403
    assert c.post(f"/api/v1/tasks/{nr2}/answer", headers=anna, json={"body": "so"}).status_code == 200
    row = c.get(f"/api/v1/tasks/{nr2}", headers=anna).json()["row_version"]
    assert c.patch(f"/api/v1/tasks/{nr2}", headers={**bob, "If-Match": str(row)},
                   json={"prio": 1}).status_code == 403
    r = c.patch(f"/api/v1/tasks/{nr2}", headers={**anna, "If-Match": str(row)}, json={"prio": 1})
    assert r.status_code == 200 and r.json()["prio"] == 1
    # The current owner transfers ownership again.
    assert c.patch("/api/v1/projects/KET", headers=anna, json={"owner": "bob"}).json()["owner"] == "bob"
    assert c.patch("/api/v1/projects/KET", headers=anna, json={"name": "z"}).status_code == 403


def test_owner_filter_in_api(env):
    c = env["c"]
    anna = human(env, "anna")
    mkproject(env)
    mkproject(env, key="ZWEI")
    c.patch("/api/v1/projects/ZWEI", headers=env["admin"], json={"owner": "anna"})
    mktask(env, title="KET-Task")
    mktask(env, project="ZWEI", title="Zwei-Task")
    titles = lambda r: {t["title"] for t in r.json()["items"]}  # noqa: E731
    assert titles(c.get("/api/v1/tasks", headers=anna, params={"owner": "me"})) == {"Zwei-Task"}
    assert titles(c.get("/api/v1/tasks", headers=env["admin"], params={"owner": "anna"})) == {"Zwei-Task"}
    assert titles(c.get("/api/v1/tasks", headers=env["admin"], params={"owner": "all"})) == {"KET-Task", "Zwei-Task"}


# --- UI: profile switch "Show all projects" (off by default, also for admins) -------------------

def _ui_login(name: str, password: str) -> TestClient:
    ui = TestClient(app)
    r = ui.get("/login")
    csrf = r.text.split('name="csrf_token" value="')[1].split('"')[0]
    assert ui.post("/login", data={"username": name, "password": password, "csrf_token": csrf},
                   follow_redirects=False).status_code == 303
    ui.csrf = ui.get("/profile").text.split('"X-CSRF-Token": "')[1].split('"')[0]
    return ui


def _show_all(ui: TestClient, on: bool) -> None:
    r = ui.post("/profile/display", data={"csrf_token": ui.csrf, **({"show_all_projects": "1"} if on else {})})
    assert r.status_code == 200 and "Gespeichert." in r.text


def _options(html: str, name: str) -> set[str]:
    """Option values of the last <select name=…> on the page (the first `project` one is the quick capture)."""
    part = html.rsplit(f'<select name="{name}"', 1)[1].split("</select>", 1)[0]
    return {v.split('"', 1)[0] for v in part.split('<option value="')[1:]} - {""}


def _two_owners(env):
    c = env["c"]
    human(env, "anna", password="anna-passwort-123")
    mkproject(env)
    mkproject(env, key="ZWEI")
    c.patch("/api/v1/projects/ZWEI", headers=env["admin"], json={"owner": "anna"})
    ket = mktask(env, title="KET-Task")["task_nr"]
    zwei = mktask(env, project="ZWEI", title="Zwei-Task", assigned_to="claude-code")["task_nr"]
    c.post("/api/v1/projects/ZWEI/sprints", headers=env["admin"], json={"name": "Zwei-Sprint"})
    return ket, zwei


def test_migration_and_new_humans_default_to_own_projects(env):
    human(env, "anna")
    with db.session() as s:
        assert s.query(Actor).filter_by(name="admin").one().show_all_projects is False
        assert s.query(Actor).filter_by(name="anna").one().show_all_projects is False


def test_ui_own_projects_only_by_default_also_for_admins(env):
    ket, zwei = _two_owners(env)
    with TestClient(app) as ui:
        assert login(ui).status_code == 303  # Bootstrap admin = actor admin, owner of KET.
        r = ui.get("/backlog")
        assert "KET-Task" in r.text and "Zwei-Task" not in r.text
        assert 'name="owner"' not in r.text and "Alle meine Projekte" in r.text  # Owner filter hidden.
        assert _options(r.text, "project") == {"KET"}
        assert "Zwei-Sprint" not in r.text  # Sprint filter: own projects only.
        r = ui.get("/backlog?owner=all")  # A stale link or cookie cannot widen the view.
        assert "Zwei-Task" not in r.text and "tm_owner" not in r.cookies
        assert "Zwei-Task" not in ui.get("/backlog?q=Zwei").text  # Search too.
        kanban = ui.get("/kanban").text
        assert "KET-Task" in kanban and "Zwei-Task" not in kanban and _options(kanban, "project") == {"KET"}
        projects = ui.get("/projects").text
        assert "/projects/KET" in projects and "/projects/ZWEI" not in projects
        assert "Angezeigt werden nur deine eigenen Projekte." in projects
        assert _options(ui.get("/tasks/new").text, "project") == {"KET"}
        assert _options(ui.get("/").text, "project") == {"KET"}  # Quick capture "+".
        assert _options(ui.get("/sprints/new").text, "project") == {"KET"}
        # Explicitly opened for a project (link on its page): that project stays selectable.
        assert _options(ui.get("/tasks/new?project=ZWEI").text, "project") == {"KET", "ZWEI"}
        # No access restriction: other people's pages stay reachable, the admin area stays complete.
        assert ui.get("/projects/ZWEI").status_code == 200
        assert "Zwei-Task" in ui.get(f"/tasks/{zwei}").text
        assert "anna" in ui.get("/admin/users").text


def test_ui_inbox_shows_own_projects_only(env):
    c = env["c"]
    ket, zwei = _two_owners(env)
    claim(env)
    assert c.post(f"/api/v1/tasks/{zwei}/ask", headers=env["claude"], json={"body": "Frage zu Zwei?"}).status_code == 200
    with TestClient(app) as ui:
        login(ui)
        assert "Frage zu Zwei?" not in ui.get("/").text and "Zwei-Task" not in ui.get("/").text
    anna = _ui_login("anna", "anna-passwort-123")
    inbox = anna.get("/").text
    assert "Zwei-Task" in inbox and "Frage zu Zwei?" in inbox
    assert "(1)" in inbox.split("inbox-group act", 1)[1].split("</h3>", 1)[0]  # Counter.


def test_ui_show_all_projects_switch_persists_and_restores_owner_filter(env):
    ket, zwei = _two_owners(env)
    anna = _ui_login("anna", "anna-passwort-123")
    r = anna.get("/backlog")
    assert "Zwei-Task" in r.text and "KET-Task" not in r.text  # A member sees only own projects too.
    assert "/projects/KET" not in anna.get("/projects").text
    assert _options(anna.get("/tasks/new").text, "project") == {"ZWEI"}

    _show_all(anna, True)
    with db.session() as s:
        assert s.query(Actor).filter_by(name="anna").one().show_all_projects is True
    r = anna.get("/backlog")  # Owner filter back, default "all owners".
    assert "KET-Task" in r.text and "Zwei-Task" in r.text and 'name="owner"' in r.text
    assert '<option value="all" selected' in r.text
    r = anna.get("/backlog?owner=me")
    assert "KET-Task" not in r.text and r.cookies.get("tm_owner") == "me"
    assert "KET-Task" not in anna.get("/kanban").text  # Choice remembered in the cookie.
    assert "KET-Task" in anna.get("/kanban?owner=all").text
    assert {"KET", "ZWEI"} <= _options(anna.get("/tasks/new").text, "project")
    assert "/projects/KET" in anna.get("/projects").text

    # The setting lives in the database: a new login (new browser, no cookie) keeps it.
    anna.post("/logout", data={"csrf_token": anna.csrf})
    anna = _ui_login("anna", "anna-passwort-123")
    assert "KET-Task" in anna.get("/backlog").text
    _show_all(anna, False)
    assert "KET-Task" not in anna.get("/backlog").text


def test_ui_hint_when_owning_no_projects(env):
    c = env["c"]
    mkproject(env)
    human(env, "anna")
    c.patch("/api/v1/projects/KET", headers=env["admin"], json={"owner": "anna"})
    with TestClient(app) as ui:
        login(ui)
        ui.csrf = ui.get("/profile").text.split('"X-CSRF-Token": "')[1].split('"')[0]
        for page in ("/", "/backlog", "/kanban", "/projects", "/tasks/new", "/sprints/new"):
            r = ui.get(page)
            assert "Du bist noch Owner keines Projekts, und angezeigt werden nur" in r.text, page
            assert 'href="/profile#display"' in r.text and 'href="/projects/new"' in r.text
        _show_all(ui, True)
        assert "Du bist noch Owner keines Projekts – deshalb" in ui.get("/?owner=me").text
        assert "Du bist noch Owner keines Projekts" not in ui.get("/?owner=all").text
