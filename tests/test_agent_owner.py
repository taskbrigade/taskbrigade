"""Agent owner and public agents: migration, assignment rule (API, orchestrator agents), filtered choices, profile
switch "Show all agents" and the admin list filters (test DB + fake Gitea)."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import db
from app.main import app
from app.models import Actor
from tests.conftest import TEST_DB_URL, login, requires_db
from tests.test_owner import human
from tests.test_workflow import env, gitea, mkproject, mktask  # noqa: F401 (Fixtures)

pytestmark = pytest.mark.db


def _agent(env, name: str, **changes) -> None:
    r = env["c"].patch(f"/api/v1/admin/actors/{name}", headers=env["admin"], json=changes)
    assert r.status_code == 200, r.text


@pytest.fixture
def oenv(env):
    """anna (member) owns project ANNA and the private agent hermes; codex is a private agent of admin;
    claude-code stays public."""
    env["anna"] = human(env, "anna", password="anna-passwort-123")
    mkproject(env)
    mkproject(env, key="ANNA")
    assert env["c"].patch("/api/v1/projects/ANNA", headers=env["admin"], json={"owner": "anna"}).status_code == 200
    _agent(env, "hermes", owner="anna", is_public=False)
    _agent(env, "codex", owner="admin", is_public=False)
    return env


def _task(e, who="anna", **kw):
    return e["c"].post("/api/v1/tasks", headers=e[who], json={"project": "ANNA", "title": "Idee", "status": "draft",
                                                              **kw})


# --- Migration ---------------------------------------------------------------------------------------------

@requires_db
def test_migration_sets_creator_or_admin_as_owner_and_keeps_agents_public(monkeypatch):
    from alembic import command
    from sqlalchemy import create_engine, inspect

    from app.config import get_settings
    from app.db import alembic_config
    from tests import conftest

    monkeypatch.setattr(get_settings(), "bootstrap_admin_actor", "admin")
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
        command.upgrade(cfg, "0017")
        conn.execute(text("INSERT INTO actors (name, display_name, actor_type, role) "
                          "VALUES ('alex', 'Alex', 'human', 'member')"))
        for name in ("wizard-bot", "old-bot"):
            conn.execute(text("INSERT INTO actors (name, display_name, actor_type, agent_kind, role) "
                              "VALUES (:n, :n, 'agent', 'codex', 'worker')"), {"n": name})
        conn.execute(text("INSERT INTO join_codes (actor_id, code_hash, created_by, expires_at) SELECT a.id, "
                          "REPEAT('a', 64), h.id, NOW() FROM actors a, actors h "
                          "WHERE a.name = 'wizard-bot' AND h.name = 'alex'"))
        command.upgrade(cfg, "head")
        rows = conn.execute(text("SELECT a.name, o.name, a.is_public, a.show_all_agents FROM actors a "
                                 "LEFT JOIN actors o ON o.id = a.owner_id ORDER BY a.name")).all()
    engine.dispose()
    assert [tuple(r) for r in rows] == [("admin", None, 0, 0), ("alex", None, 0, 0), ("old-bot", "admin", 1, 0),
                                        ("wizard-bot", "alex", 1, 0)]


# --- Assignment rule (server side) ---------------------------------------------------------------------------

def test_humans_assign_only_own_and_public_agents(oenv):
    e = oenv
    r = _task(e, assigned_to="codex")
    assert r.status_code == 403 and r.json()["error"]["code"] == "agent_not_visible", r.text
    assert _task(e, assigned_to="hermes").status_code == 201  # Own private agent.
    nr = _task(e, assigned_to="claude-code").json()["task_nr"]  # Public agent.
    r = e["c"].patch(f"/api/v1/tasks/{nr}", headers=e["anna"], json={"assigned_to": "codex"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "agent_not_visible"
    # Admins may assign any agent; the owner can still edit the task without touching the assignee.
    assert e["c"].patch(f"/api/v1/tasks/{nr}", headers=e["admin"], json={"assigned_to": "codex"}).status_code == 200
    r = e["c"].patch(f"/api/v1/tasks/{nr}", headers=e["anna"], json={"title": "Idee 2", "assigned_to": "codex"})
    assert r.status_code == 200 and r.json()["assigned_to"] == "codex", r.text


def test_reviewer_and_project_default_follow_the_same_rule(oenv):
    e = oenv
    _agent(e, "codex", role="reviewer")
    r = _task(e, reviewer="codex")
    assert r.status_code == 403 and r.json()["error"]["code"] == "agent_not_visible"
    r = e["c"].patch("/api/v1/projects/ANNA", headers=e["anna"], json={"default_reviewer": "codex"})
    assert r.status_code == 403
    # A default chosen by an admin applies to anna's new tasks (the project setting, not anna's choice).
    assert e["c"].patch("/api/v1/projects/ANNA", headers=e["admin"],
                        json={"default_reviewer": "codex"}).status_code == 200
    r = _task(e)
    assert r.status_code == 201 and r.json()["reviewer"] == "codex", r.text


def test_orchestrator_agent_uses_its_owners_and_public_agents(oenv):
    e = oenv
    _agent(e, "openclaw-agent", role="orchestrator", owner="anna", is_public=False)
    assert _task(e, who="openclaw-agent", assigned_to="hermes").status_code == 201
    assert _task(e, who="openclaw-agent", assigned_to="claude-code").status_code == 201
    r = _task(e, who="openclaw-agent", assigned_to="codex")
    assert r.status_code == 403 and r.json()["error"]["code"] == "agent_not_visible"
    names = {a["name"] for a in e["c"].get("/api/v1/actors", headers=e["openclaw-agent"]).json()}
    assert {"hermes", "claude-code", "openclaw-agent", "anna", "admin"} <= names and "codex" not in names
    hermes = next(a for a in e["c"].get("/api/v1/actors", headers=e["anna"]).json() if a["name"] == "hermes")
    assert hermes["owner"] == "anna" and hermes["is_public"] is False


def test_actor_directory_for_admins(oenv):
    e = oenv
    names = {a["name"] for a in e["c"].get("/api/v1/actors", headers=e["admin"]).json()}
    assert "codex" in names and "hermes" not in names  # Own and public by default …
    names = {a["name"] for a in e["c"].get("/api/v1/actors?all=true", headers=e["admin"]).json()}
    assert "hermes" in names  # … all on request.
    names = {a["name"] for a in e["c"].get("/api/v1/actors?all=true", headers=e["anna"]).json()}
    assert "codex" not in names  # `all` is for admins only.


def test_new_agents_are_private_and_owned_by_their_creator(oenv):
    e = oenv
    r = e["c"].post("/api/v1/admin/actors", headers=e["admin"], json={
        "name": "new-bot", "display_name": "New", "actor_type": "agent", "agent_kind": "codex"})
    assert r.status_code == 201 and r.json()["actor"]["owner"] == "admin" and not r.json()["actor"]["is_public"]
    r = e["c"].post("/api/v1/admin/actors", headers=e["admin"], json={
        "name": "anna-bot", "display_name": "Anna", "actor_type": "agent", "agent_kind": "codex", "owner": "anna",
        "is_public": True})
    assert r.json()["actor"]["owner"] == "anna" and r.json()["actor"]["is_public"]
    r = e["c"].patch("/api/v1/admin/actors/anna-bot", headers=e["admin"], json={"owner": "hermes"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_owner"
    r = e["c"].patch("/api/v1/admin/actors/anna", headers=e["admin"], json={"is_public": True})
    assert r.status_code == 422


# --- Web UI ------------------------------------------------------------------------------------------------------

def _agent_options(html: str, name: str = "agent") -> set[str]:
    part = html.rsplit(f'<select name="{name}"', 1)[1].split("</select>", 1)[0]
    return {o.split(">", 1)[1].split("<", 1)[0] for o in part.split("<option")[1:]}


def _ui(env) -> TestClient:
    ui = TestClient(app)
    assert login(ui).status_code == 303
    ui.csrf = ui.get("/profile").text.split('"X-CSRF-Token": "')[1].split('"')[0]
    return ui


def test_ui_choices_show_own_and_public_agents_and_keep_the_assignee(oenv):
    e = oenv
    nr = mktask(e, project="ANNA", assigned_to="hermes")["task_nr"]
    with _ui(e) as ui:
        assert 'name="show_all_agents"' in ui.get("/profile").text
        options = _agent_options(ui.get("/backlog").text)
        assert {"codex", "claude-code", "anna"} <= options and "hermes" not in options
        assert "hermes" in _agent_options(ui.get("/backlog?agent=hermes").text)  # Selected filter stays.
        assert "hermes" not in _agent_options(ui.get("/tasks/new").text, "assigned_to")
        detail = ui.get(f"/tasks/{nr}").text
        assert "<option selected>hermes</option>" in detail  # Already assigned: still shown.
        r = ui.post("/profile/display", data={"csrf_token": ui.csrf, "show_all_agents": "1"})
        assert r.status_code == 200 and "Gespeichert." in r.text
        assert "hermes" in _agent_options(ui.get("/kanban").text)
    with db.session() as s:
        assert s.query(Actor).filter_by(name="admin").one().show_all_agents is True


def test_ui_switch_is_for_admins_only(oenv):
    with db.session() as s:
        s.query(Actor).filter_by(name="anna").one().show_all_agents = False
        s.commit()
    ui = TestClient(app)
    r = ui.get("/login")
    csrf = r.text.split('name="csrf_token" value="')[1].split('"')[0]
    ui.post("/login", data={"username": "anna", "password": "anna-passwort-123", "csrf_token": csrf})
    page = ui.get("/profile").text
    assert 'name="show_all_agents"' not in page
    csrf = page.split('"X-CSRF-Token": "')[1].split('"')[0]
    ui.post("/profile/display", data={"csrf_token": csrf, "show_all_agents": "1"})
    with db.session() as s:
        assert s.query(Actor).filter_by(name="anna").one().show_all_agents is False
    assert "codex" not in _agent_options(ui.get("/backlog").text)


def test_admin_list_shows_all_agents_with_filters(oenv):
    e = oenv
    with _ui(e) as ui:
        page = ui.get("/admin/users").text
        assert "/admin/users/hermes" in page and "/admin/users/codex" in page  # Always all, switch off.
        page = ui.get("/admin/users?owner=anna").text
        assert "/admin/users/hermes" in page and "/admin/users/codex" not in page and "1 von " in page
        page = ui.get("/admin/users?kind=codex&role=worker").text
        assert "/admin/users/codex" in page and "/admin/users/hermes" not in page
        page = ui.get("/admin/users?owner=none").text
        assert "/admin/users/codex" not in page and "/admin/users/claude-code" in page
        page = ui.get("/admin/users?role=bogus").text  # Unknown values are ignored.
        assert "/admin/users/hermes" in page and "Filter zurücksetzen" not in page


def test_admin_form_changes_owner_and_public(oenv):
    e = oenv
    with _ui(e) as ui:
        page = ui.get("/admin/users/hermes").text
        assert 'name="is_public"' in page and "<option selected>anna</option>" in page
        r = ui.post("/admin/users/hermes", data={
            "csrf_token": ui.csrf, "display_name": "Hermes", "role": "worker", "runtime": "local",
            "agent_kind": "hermes", "lease_minutes": "120", "max_parallel_tasks": "1", "owner": "admin",
            "is_public": "1"})
        assert r.status_code == 200 and "Gespeichert." in r.text, r.text
    with db.session() as s:
        hermes = s.query(Actor).filter_by(name="hermes").one()
        assert hermes.is_public and hermes.owner_name == "admin"


def test_wizard_sets_creator_as_owner(oenv):
    e = oenv
    with _ui(e) as ui:
        page = ui.get("/admin/agents/new").text
        assert "<option selected>admin</option>" in page
        r = ui.post("/admin/agents/new", data={"csrf_token": ui.csrf, "name": "wiz-bot", "agent_kind": "codex",
                                               "runtime": "local", "role": "worker", "owner": "admin"})
        assert r.status_code == 200, r.text
    with db.session() as s:
        bot = s.query(Actor).filter_by(name="wiz-bot").one()
        assert bot.owner_name == "admin" and bot.is_public is False


def test_batch_refuses_hidden_agents_for_members(oenv):
    e = oenv
    sprint = e["c"].post("/api/v1/projects/ANNA/sprints", headers=e["anna"], json={"name": "S1"})
    assert sprint.status_code == 201, sprint.text
    sid = sprint.json()["id"]
    ui = TestClient(app)
    r = ui.get("/login")
    csrf = r.text.split('name="csrf_token" value="')[1].split('"')[0]
    ui.post("/login", data={"username": "anna", "password": "anna-passwort-123", "csrf_token": csrf})
    csrf = ui.get("/profile").text.split('"X-CSRF-Token": "')[1].split('"')[0]
    r = ui.post(f"/projects/ANNA/sprints/{sid}/batch", data={"csrf_token": csrf, "lines": "Eins | codex\nZwei | hermes",
                                                             "step": "preview"})
    assert "ist weder öffentlich noch deiner" in r.text and r.text.count("ist weder öffentlich") == 1

