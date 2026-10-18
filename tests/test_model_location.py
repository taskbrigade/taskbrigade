"""Model location of agents (`actors.model_location`, `app/services/zones.py`): zone helper, assignment, review and
claim checks with and without `privacy_enforce_model_location`, admin UI, findings and migration 0020."""
import pytest
from sqlalchemy import inspect, select, text

from app.db import db
from app.models import Actor, Project
from app.services import settings_store, zones
from app.services import tasks as task_svc
from tests.test_reviewers import _review_claim, _role, _submitted  # noqa: F401
from tests.test_ui_cli import post, ui  # noqa: F401 (Fixtures)
from tests.test_workflow import claim, env, gitea, mkproject, mktask  # noqa: F401

# --- Helper (no DB) ----------------------------------------------------------------------------


def _actor(model_location="cloud", runtime="local", actor_type="agent", kind="opencode"):
    return Actor(name="x", actor_type=actor_type, agent_kind=kind if actor_type == "agent" else None,
                 runtime=runtime, model_location=model_location)


@pytest.mark.parametrize("location,runtime,enforce,expected", [
    ("cloud", "local", False, {"cloud_ok", "local_only"}),  # Old rule: runtime only.
    ("cloud", "local", True, {"cloud_ok"}),
    ("local", "local", True, {"cloud_ok", "local_only"}),
    ("local", "cloud", True, {"cloud_ok"}),  # The process in the cloud sees the content anyway.
    ("local", "cloud", False, {"cloud_ok"}),
    ("cloud", "cloud", True, {"cloud_ok"}),
    ("none", "local", False, {"sealed"}),  # Runner: never LLM work, also without enforcement.
    ("none", "local", True, {"sealed"}),
])
def test_allowed_classes(location, runtime, enforce, expected):
    assert zones.allowed_classes(_actor(location, runtime), enforce) == expected


def test_humans_keep_the_runtime_rule():
    # Local humans with the data right may also work on `sealed` tasks (zone R: no LLM).
    assert zones.allowed_classes(_actor(actor_type="human"), True) == {"cloud_ok", "local_only"}
    data_right = _actor(actor_type="human")
    data_right.data_access = True
    assert zones.allowed_classes(data_right, True) == {"cloud_ok", "local_only", "sealed"}
    assert zones.allowed_classes(_actor(actor_type="human", runtime="cloud"), True) == {"cloud_ok"}


def test_effective_class_falls_back_to_project_privacy():
    project = Project(project_key="KET", privacy="local_only")
    assert zones.effective_class(project) == "local_only"
    assert zones.effective_class(project, object()) == "local_only"  # Task without its own class (yet)


def test_check_without_session_uses_the_old_rule():
    project = Project(project_key="KET", privacy="local_only")
    zones.check(project, _actor("cloud", "local"))  # Transient objects: enforcement unknown → runtime rule.
    with pytest.raises(Exception, match="local_only"):
        zones.check(project, _actor("cloud", "cloud"))


@pytest.mark.parametrize("kind,location,ok", [
    ("claude_code", "local", False), ("codex", "local", False), ("claude_code", "cloud", True),
    ("opencode", "local", True), ("hermes", "none", False), ("other", "moon", False)])
def test_model_location_validation(kind, location, ok):
    actor = _actor(location, kind=kind)
    if ok:
        zones.check_model_location(actor)
    else:
        with pytest.raises(Exception):
            zones.check_model_location(actor)


def test_local_needs_confirmation():
    zones.require_confirmation("local", "local", False)  # Unchanged
    zones.require_confirmation("cloud", "local", False)  # Stricter needs no confirmation
    zones.require_confirmation("local", "cloud", True)
    with pytest.raises(Exception) as e:
        zones.require_confirmation("local", "cloud", False)
    assert e.value.code == "confirm_local_model"


# --- With the test DB -----------------------------------------------------------------------------


def _enforce(on: bool) -> None:
    with db.session() as s:
        settings_store.update(s, {zones.SETTING: "true" if on else "false"}, None)
        s.commit()


def _location(name: str, location: str) -> None:
    with db.session() as s:
        s.scalar(select(Actor).where(Actor.name == name)).model_location = location
        s.commit()


@pytest.fixture
def lenv(env):
    """Project KET is local_only; hermes (runtime local) runs a cloud model."""
    mkproject(env, privacy="local_only")
    _location("hermes", "cloud")
    return env


def _assign(e, nr, who):
    return e["c"].patch(f"/api/v1/tasks/{nr}", headers=e["admin"], json={"assigned_to": who})


@pytest.mark.db
def test_new_installation_enforces_by_default(ready_db):
    with db.session() as s:
        assert zones.enforced(s)  # No app_settings row → default on.


@pytest.mark.db
def test_assignment_uses_model_location_when_enforced(lenv):
    nr = mktask(lenv)["task_nr"]
    _enforce(False)
    assert _assign(lenv, nr, "hermes").status_code == 200  # Unchanged without enforcement (runtime local).
    assert _assign(lenv, nr, None).status_code == 200
    _enforce(True)
    r = _assign(lenv, nr, "hermes")
    assert r.status_code == 422 and r.json()["error"]["code"] == "privacy_violation"
    assert "cloud model" in r.json()["error"]["message"] or "Cloud-Modell" in r.json()["error"]["message"]
    assert _assign(lenv, nr, "opencode").status_code == 200  # Local runtime and local model.
    assert _assign(lenv, nr, "claude-code").status_code == 422  # Runtime cloud: refused in both modes.


@pytest.mark.db
def test_project_access_uses_model_location(lenv):
    _enforce(True)
    r = lenv["c"].put("/api/v1/admin/projects/KET/members/hermes", headers=lenv["admin"], json={"permission": "read"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "privacy_violation"


@pytest.mark.db
def test_review_assignment_uses_model_location(lenv):
    _role("hermes", "reviewer")
    _role("opencode", "reviewer")
    _enforce(False)
    nr = mktask(lenv, reviewer="hermes")["task_nr"]
    _enforce(True)
    with db.session() as s:
        task = task_svc.get(s, nr)
        assert task_svc.reviews.effective(s, task) is None  # Entered while off, now not effective.
    r = lenv["c"].post("/api/v1/tasks", headers=lenv["admin"], json={
        "project": "KET", "title": "x", "description": "d", "acceptance_criteria": "a", "reviewer": "hermes"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "privacy_violation"
    assert mktask(lenv, reviewer="opencode")["reviewer_effective"]


@pytest.mark.db
def test_review_jobs_respect_model_location(lenv):
    _role("hermes", "reviewer")
    _enforce(False)
    nr = mktask(lenv, assigned_to="opencode", reviewer="hermes")["task_nr"]
    with db.session() as s:  # Submitted state without the Git round trip.
        task = task_svc.get(s, nr)
        task.status, task.submitted_at = "for_review", task_svc.utcnow()
        s.commit()
    with db.session() as s:
        hermes = s.scalar(select(Actor).where(Actor.name == "hermes"))
        classes = zones.allowed_classes(hermes, False)
        assert s.scalar(task_svc._review_jobs(hermes, classes)).task_nr == nr
        assert s.scalar(task_svc._review_jobs(hermes, zones.allowed_classes(hermes, True))) is None
    _enforce(True)
    assert _review_claim(lenv).status_code == 204  # Not offered any more.
    r = _review_claim(lenv, task_nr=nr)
    assert r.status_code == 403


@pytest.mark.db
def test_claim_uses_model_location(lenv):
    _enforce(False)
    nr = mktask(lenv, assigned_to="hermes")["task_nr"]
    _enforce(True)  # Assigned before enforcement: not claimable any more.
    assert claim(lenv, "hermes").status_code == 204
    r = claim(lenv, "hermes", task_nr=nr)
    assert r.status_code == 422 and r.json()["error"]["code"] == "privacy_violation"
    _enforce(False)  # Off again: the old behavior.
    r = claim(lenv, "hermes")
    assert r.status_code == 200 and r.json()["task_nr"] == nr


@pytest.mark.db
def test_agent_reviewed_sql_matches_helper(lenv):
    _role("hermes", "reviewer")
    _enforce(False)
    nr = mktask(lenv, assigned_to="opencode", reviewer="hermes")["task_nr"]
    with db.session() as s:
        for enforce in (False, True):
            hit = s.scalar(select(Project.project_key).join(task_svc.Task, task_svc.Task.project_id == Project.id)
                           .where(task_svc.Task.task_nr == nr, task_svc._agent_reviewed(enforce)))
            hermes = s.scalar(select(Actor).where(Actor.name == "hermes"))
            assert (hit is not None) == ("local_only" in zones.allowed_classes(hermes, enforce))


@pytest.mark.db
def test_switch_to_cloud_model_refused_while_linked(lenv):
    _location("hermes", "local")
    _enforce(True)
    mktask(lenv, assigned_to="hermes")
    r = lenv["c"].patch("/api/v1/admin/actors/hermes", headers=lenv["admin"], json={"model_location": "cloud"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "privacy_violation"
    _enforce(False)  # Off: the model location does not matter for local_only yet.
    r = lenv["c"].patch("/api/v1/admin/actors/hermes", headers=lenv["admin"], json={"model_location": "cloud"})
    assert r.status_code == 200 and r.json()["model_location"] == "cloud"


@pytest.mark.db
def test_api_validates_and_shows_model_location(lenv):
    c, admin = lenv["c"], lenv["admin"]
    r = c.patch("/api/v1/admin/actors/claude-code", headers=admin, json={"model_location": "local"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_model_location"
    r = c.patch("/api/v1/admin/actors/opencode", headers=admin, json={"model_location": "none"})
    assert r.status_code == 422
    r = c.post("/api/v1/admin/actors", headers=admin, json={"name": "fresh", "display_name": "F",
                                                             "actor_type": "agent", "agent_kind": "hermes"})
    assert r.status_code == 201 and r.json()["actor"]["model_location"] == "cloud"  # Conservative default
    agents = {a["name"]: a for a in c.get("/api/v1/actors", headers=admin).json()}
    assert agents["hermes"]["model_location"] == "cloud" and agents["opencode"]["model_location"] == "local"


@pytest.mark.db
def test_project_switch_to_local_only_uses_model_location(env):
    mkproject(env)
    _location("hermes", "cloud")
    mktask(env, assigned_to="hermes")
    _enforce(True)
    r = env["c"].patch("/api/v1/projects/KET", headers=env["admin"], json={"privacy": "local_only"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "privacy_violation"
    _enforce(False)
    r = env["c"].patch("/api/v1/projects/KET", headers=env["admin"], json={"privacy": "local_only"})
    assert r.status_code == 200


@pytest.mark.db
def test_findings(lenv):
    _role("openclaw-agent", "reviewer")
    _location("openclaw-agent", "cloud")
    _enforce(False)
    nr = mktask(lenv, assigned_to="hermes", reviewer="openclaw-agent")["task_nr"]
    with db.session() as s:
        found = {f["actor"].name: f for f in zones.findings(s)}
    assert set(found) == {"hermes", "openclaw-agent"}  # opencode has a local model; cloud runtimes never had access
    assert found["hermes"]["tasks"] == [nr] and "KET" in found["hermes"]["projects"]
    assert found["openclaw-agent"]["reviews"] == [nr]


@pytest.mark.db
def test_admin_ui_edits_model_location_and_shows_findings(lenv, ui):  # noqa: F811
    _enforce(False)
    mktask(lenv, assigned_to="hermes")
    page = ui.get("/admin/settings").text
    assert "model-location-findings" in page and "/admin/users/hermes" in page
    form = ui.get("/admin/users/hermes").text
    assert 'name="model_location"' in form and 'name="confirm_local_model"' in form
    data = {"display_name": "hermes", "role": "worker", "runtime": "local", "agent_kind": "hermes",
            "lease_minutes": "120", "max_parallel_tasks": "1", "model_location": "local"}
    r = post(ui, "/admin/users/hermes", **data)
    assert r.status_code == 422  # Confirmation missing
    with db.session() as s:
        assert s.scalar(select(Actor.model_location).where(Actor.name == "hermes")) == "cloud"
    r = post(ui, "/admin/users/hermes", **data, confirm_local_model="1")
    assert r.status_code == 200
    with db.session() as s:
        assert s.scalar(select(Actor.model_location).where(Actor.name == "hermes")) == "local"
    assert "/admin/users/hermes" not in ui.get("/admin/settings").text.split("model-location-findings")[1]
    assert "lokales Modell" in ui.get("/admin/users").text


# --- Migration 0020 ------------------------------------------------------------------------------

@pytest.mark.db
def test_model_location_migration(ready_db):
    from alembic import command
    from sqlalchemy import create_engine

    from app.db import alembic_config
    from tests import conftest

    engine = create_engine(conftest.TEST_DB_URL)
    cfg = alembic_config()
    setting = text("SELECT setting_value FROM app_settings WHERE setting_key = 'privacy_enforce_model_location'")
    try:
        with engine.begin() as conn:
            conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
            for table in inspect(conn).get_table_names():
                conn.execute(text(f"DROP TABLE `{table}`"))
            conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")  # New installation: no agents → no row, default on.
            assert conn.execute(setting).scalar() is None
            command.downgrade(cfg, "0019")
            assert "model_location" not in {c["name"] for c in inspect(conn).get_columns("actors")}
            conn.execute(text("INSERT INTO actors (name, display_name, actor_type, agent_kind, runtime) VALUES "
                              "('loc', 'L', 'agent', 'opencode', 'local'), ('cc', 'C', 'agent', 'claude_code', "
                              "'cloud')"))
            command.upgrade(cfg, "0020")  # Existing installation: everyone cloud, enforcement off.
            rows = dict(conn.execute(text("SELECT name, model_location FROM actors")).all())
            assert rows == {"admin": "cloud", "loc": "cloud", "cc": "cloud"}
            assert conn.execute(setting).scalar() == "false"
            command.downgrade(cfg, "0019")
            assert conn.execute(setting).scalar() is None
            command.upgrade(cfg, "head")
    finally:
        engine.dispose()
        conftest._schema_ready = False  # The next ready_db recreates schema and test agents.
