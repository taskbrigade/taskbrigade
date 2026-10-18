"""Data class per task (`tasks.data_class`, research/konzept-datenzonen.md §1/§8): rules 1–4 in create/update/assign/
claim over API, web UI, CLI and MCP, badges, notifications, attachments and migration 0022."""
import logging
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, literal, select, text

from app.db import alembic_config, db
from app.errors import Forbidden, Unprocessable
from app.models import Actor, Project, Task
from app.services import notifications, zones
from cli import client as client_mod
from cli.client import data_class_line, parse_data_class
from cli.mcp_server import TaskBrigadeMCP
from tests.conftest import requires_db
from tests.test_attachment_lock import _upload
from tests.test_db_integration import empty_test_db  # noqa: F401 (Fixture)
from tests.test_sprints import senv  # noqa: F401 (Fixture)
from tests.test_ui_cli import post, tm_as, ui  # noqa: F401 (Fixtures)
from tests.test_workflow import claim, env, gitea, mkproject, mktask  # noqa: F401 (Fixtures)
from tests.test_workflow_engine import graph  # noqa: F401 (Fixture)

# --- Rules without the DB ------------------------------------------------------------------------


def _project(privacy="local_only", owner_id=1):
    return Project(project_key="KET", privacy=privacy, owner_id=owner_id)


def _task(**kw):
    return Task(task_nr="KET-0001", **{"status": "defined", "task_kind": "work", **kw})


ADMIN = SimpleNamespace(role="admin", actor_type="human", id=9)
OWNER = SimpleNamespace(role="member", actor_type="human", id=1)
ORCH = SimpleNamespace(role="orchestrator", actor_type="agent", id=5)


@pytest.mark.parametrize("value,expected", [
    (None, None), ("", None), ("inherit", None), (" Sealed ", "sealed"), ("cloud_ok", "cloud_ok")])
def test_normalize_class(value, expected):
    assert zones.normalize_class(value) == expected


def test_normalize_class_refuses_unknown():
    with pytest.raises(Unprocessable) as e:
        zones.normalize_class("secret")
    assert e.value.code == "invalid_data_class"


def test_rule1_stricter_is_always_allowed():
    for new in ("local_only", "sealed"):
        zones.check_class(_project("cloud_ok"), _task(), new, ORCH)
    zones.check_class(_project("local_only"), _task(), "sealed", None)


def test_rule2_looser_only_for_owner_and_admins():
    with pytest.raises(Forbidden) as e:
        zones.check_class(_project("local_only"), _task(), "cloud_ok", ORCH)
    assert e.value.code == "data_class_forbidden"
    zones.check_class(_project("local_only"), _task(), "cloud_ok", OWNER)
    zones.check_class(_project("local_only"), _task(), "cloud_ok", ADMIN)


def test_rule3_runner_tasks_are_sealed():
    for kind in ("run", "profile"):
        with pytest.raises(Unprocessable) as e:
            zones.check_class(_project("cloud_ok"), _task(task_kind=kind), None, ADMIN)
        assert e.value.code == "data_class_runner"
        zones.check_class(_project("cloud_ok"), _task(task_kind=kind), "sealed", ADMIN)


@pytest.mark.parametrize("status,claimed", [("in_progress", None), ("for_review", None), ("defined", "x")])
def test_rule4_no_downgrade_after_start(status, claimed):
    task = _task(id=1, status=status, claimed_at=claimed, data_class="local_only")
    with pytest.raises(Unprocessable) as e:
        zones.check_class(_project("cloud_ok"), task, "cloud_ok", ADMIN)
    assert e.value.code == "data_class_downgrade"
    with pytest.raises(Unprocessable):
        zones.check_class(_project("cloud_ok"), task, None, ADMIN)  # Inherit = cloud_ok is looser too.
    zones.check_class(_project("cloud_ok"), task, "sealed", ADMIN)
    zones.check_class(_project("cloud_ok"), _task(id=1, data_class="local_only"), "cloud_ok", ADMIN)  # Not started


def test_effective_class_and_check_messages():
    project = _project("cloud_ok")
    assert zones.effective_class(project, _task(data_class="sealed")) == "sealed"
    claude = Actor(name="claude", actor_type="agent", agent_kind="claude_code", runtime="cloud", model_location="cloud")
    with pytest.raises(Unprocessable, match="sealed"):
        zones.check(project, claude, _task(data_class="sealed"))
    with pytest.raises(Unprocessable, match="KET-0001"):
        zones.check(project, claude, _task(data_class="local_only"))
    zones.check(_project("local_only"), claude, _task(data_class="cloud_ok"))


def test_cli_helpers():
    assert parse_data_class("local-only") == "local_only" and parse_data_class("none") == "inherit"
    assert parse_data_class("") is None
    with pytest.raises(ValueError):
        parse_data_class("open")
    assert "set on the task" in data_class_line({"data_class": "sealed", "data_class_effective": "sealed"})
    assert "inherited" in data_class_line({"data_class": None, "data_class_effective": "cloud_ok"})
    assert data_class_line({}) is None


# --- With the test DB -----------------------------------------------------------------------------

@pytest.fixture
def denv(senv):  # noqa: F811
    """KET cloud_ok (owner admin), LOC local_only (owner alex), orchestrator `orch`; enforcement on."""
    mkproject(senv, key="LOC", privacy="local_only", owner="alex")
    return senv


def _patch(e, nr, who="admin", **body):
    return e["c"].patch(f"/api/v1/tasks/{nr}", headers=e[who], json=body)


def _get(e, nr):
    return e["c"].get(f"/api/v1/tasks/{nr}", headers=e["admin"]).json()


def _set_data_access(name):
    with db.session() as s:
        s.scalar(select(Actor).where(Actor.name == name)).data_access = True
        s.commit()


def _set(nr, **values):
    with db.session() as s:
        task = s.scalar(select(Task).where(Task.task_nr == nr))
        for k, v in values.items():
            setattr(task, k, v)
        s.commit()


@pytest.mark.db
def test_new_task_inherits_the_project_class(denv):
    t = mktask(denv)
    assert (t["data_class"], t["data_class_effective"], t["task_kind"]) == (None, "cloud_ok", "work")
    t = mktask(denv, project="LOC", who="alex")
    assert (t["data_class"], t["data_class_effective"]) == (None, "local_only")


@pytest.mark.db
def test_sealed_task_only_for_humans(denv):
    """Rule 1 (orchestrator may tighten) and the zone check on assignment: no LLM agent on a sealed task."""
    t = mktask(denv, who="orch", data_class="sealed")
    assert t["data_class"] == "sealed"
    r = _patch(denv, t["task_nr"], assigned_to="claude-code")
    assert r.status_code == 422 and r.json()["error"]["code"] == "privacy_violation"
    assert _patch(denv, t["task_nr"], assigned_to="hermes").status_code == 422  # Local model, still an LLM.
    assert _patch(denv, t["task_nr"], assigned_to="admin").status_code == 422  # Human without the data right
    _set_data_access("admin")
    assert _patch(denv, t["task_nr"], assigned_to="admin").status_code == 200
    r = denv["c"].post("/api/v1/tasks", headers=denv["admin"], json={
        "project": "KET", "title": "x", "description": "d", "acceptance_criteria": "a", "data_class": "sealed",
        "assigned_to": "claude-code"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "privacy_violation"


@pytest.mark.db
def test_owner_may_loosen_with_audit(denv, caplog):
    """Rule 2: a cloud_ok code task in a local_only project – by the owner only, written to the audit log."""
    body = {"project": "LOC", "title": "Script", "description": "d", "acceptance_criteria": "a",
            "data_class": "cloud_ok", "assigned_to": "claude-code"}
    r = denv["c"].post("/api/v1/tasks", headers=denv["orch"], json=body)
    assert r.status_code == 403 and r.json()["error"]["code"] == "data_class_forbidden"
    with caplog.at_level(logging.INFO, logger="admin_audit"):
        r = denv["c"].post("/api/v1/tasks", headers=denv["alex"], json=body)
    assert r.status_code == 201, r.text
    assert any(rec.getMessage() == "data_class_loosened" and rec.task == r.json()["task_nr"] for rec in caplog.records)
    nr = r.json()["task_nr"]
    assert ("data_class", "inherited: local_only", "cloud_ok") in [
        (ev["type"], ev["from"].replace("geerbt", "inherited"), ev["to"]) for ev in _get(denv, nr)["events"]
        if ev["type"] == "data_class"]
    assert claim(denv, "claude").json()["task_nr"] == nr  # The cloud agent may work on this task only.
    plain = mktask(denv, project="LOC", who="alex")["task_nr"]
    assert _patch(denv, plain, "alex", assigned_to="claude-code").status_code == 422
    # The orchestrator cannot loosen an existing task either, but may tighten it again.
    other = mktask(denv, project="LOC", who="alex")["task_nr"]
    r = _patch(denv, other, "orch", data_class="cloud_ok")
    assert r.status_code == 403 and r.json()["error"]["code"] == "data_class_forbidden"


@pytest.mark.db
def test_runner_task_must_be_sealed(denv):
    nr = mktask(denv)["task_nr"]
    _set(nr, task_kind="run")
    r = _patch(denv, nr, data_class="cloud_ok")
    assert r.status_code == 422 and r.json()["error"]["code"] == "data_class_runner"
    assert _patch(denv, nr, data_class="sealed").json()["data_class"] == "sealed"


@pytest.mark.db
def test_class_only_stricter_after_start(denv):
    """Rule 4, and the assignee must be able to handle a stricter class."""
    nr = mktask(denv, assigned_to="hermes")["task_nr"]
    assert claim(denv, "hermes").json()["task_nr"] == nr
    assert _patch(denv, nr, data_class="local_only").json()["data_class_effective"] == "local_only"
    for value in ("cloud_ok", "inherit", None):
        r = _patch(denv, nr, data_class=value)
        assert r.status_code == 422 and r.json()["error"]["code"] == "data_class_downgrade", value
    r = _patch(denv, nr, data_class="sealed")  # Stricter, but hermes runs an LLM.
    assert r.status_code == 422 and r.json()["error"]["code"] == "privacy_violation"
    assert _get(denv, nr)["data_class"] == "local_only"
    nr2 = mktask(denv, assigned_to="claude-code")["task_nr"]
    r = _patch(denv, nr2, data_class="local_only")
    assert r.status_code == 422 and r.json()["error"]["code"] == "privacy_violation"
    # Reassigning in the same request checks the new assignee against the new class.
    assert _patch(denv, nr2, data_class="local_only", assigned_to="opencode").status_code == 200


@pytest.mark.db
def test_reviewer_must_handle_the_class(denv):
    with db.session() as s:
        s.scalar(select(Actor).where(Actor.name == "codex")).role = "reviewer"
        s.commit()
    nr = mktask(denv, reviewer="codex")["task_nr"]
    r = _patch(denv, nr, data_class="local_only")
    assert r.status_code == 422 and r.json()["error"]["code"] == "privacy_violation"


@pytest.mark.db
def test_claim_uses_the_task_class(denv):
    nr = mktask(denv, assigned_to="claude-code")["task_nr"]
    _set(nr, data_class="sealed")  # e.g. set before a rule existed
    assert claim(denv, "claude").status_code == 204
    r = claim(denv, "claude", nr)
    assert r.status_code == 422 and r.json()["error"]["code"] == "privacy_violation"
    _set(nr, data_class=None)
    assert claim(denv, "claude").json()["task_nr"] == nr


@pytest.mark.db
def test_project_privacy_switch_respects_task_classes(denv):
    c, admin = denv["c"], denv["admin"]
    started = mktask(denv, project="LOC", who="alex", assigned_to="hermes")["task_nr"]
    assert claim(denv, "hermes").json()["task_nr"] == started
    waiting = mktask(denv, project="LOC", who="alex")["task_nr"]
    assert c.patch("/api/v1/projects/LOC", headers=admin, json={"privacy": "cloud_ok"}).status_code == 200
    assert _get(denv, started)["data_class"] == "local_only"  # Rule 4: pinned, it was already being worked on.
    assert _get(denv, waiting)["data_class"] is None and _get(denv, waiting)["data_class_effective"] == "cloud_ok"
    # Back to local_only: a task with its own class cloud_ok keeps its cloud agent.
    own = mktask(denv, project="LOC", who="alex", assigned_to="claude-code", data_class="cloud_ok")["task_nr"]
    assert c.patch("/api/v1/projects/LOC", headers=admin, json={"privacy": "local_only"}).status_code == 200
    _patch(denv, own, data_class="inherit")
    assert _get(denv, own)["data_class"] == "cloud_ok"  # Refused: claude-code cannot work on local_only.


@pytest.mark.db
def test_sql_rule_matches_helper_for_all_classes(denv):
    with db.session() as s:
        s.scalar(select(Actor).where(Actor.name == "opencode")).model_location = "none"  # Stand-in runner
        s.scalar(select(Actor).where(Actor.name == "alex")).data_access = True  # Human with the data right
        s.commit()
        for enforce in (False, True):
            for actor in s.scalars(select(Actor)):
                for cls in zones.DATA_CLASSES:
                    hit = s.scalar(select(Actor.id).where(Actor.id == actor.id,
                                                          zones.sql_may_handle(Actor, literal(cls), enforce)))
                    assert (hit is not None) == (cls in zones.allowed_classes(actor, enforce)), (actor.name, cls)


@pytest.mark.db
def test_sealed_task_hides_content_and_refuses_spreadsheets(denv):
    nr = mktask(denv, title="Secret run", data_class="sealed")["task_nr"]
    plain = mktask(denv, title="Open one")["task_nr"]
    with db.session() as s:
        task = s.scalar(select(Task).where(Task.task_nr == nr))
        assert "Secret run" not in notifications.task_text(s, task, "question")
        task = s.scalar(select(Task).where(Task.task_nr == plain))
        assert "Open one" in notifications.task_text(s, task, "question")
    r = _upload(denv, nr, "data.csv")
    assert r.status_code == 422 and r.json()["error"]["code"] == "use_dataset_upload"


@pytest.mark.db
def test_duplicate_keeps_class_unless_looser_than_allowed(denv):
    loose = mktask(denv, project="LOC", who="alex", data_class="cloud_ok")["task_nr"]
    r = denv["c"].post(f"/api/v1/tasks/{loose}/duplicate", headers=denv["orch"], json={})
    assert r.status_code == 201 and r.json()["data_class"] is None  # Orchestrator: falls back to local_only
    r = denv["c"].post(f"/api/v1/tasks/{loose}/duplicate", headers=denv["alex"], json={})
    assert r.json()["data_class"] == "cloud_ok"


@pytest.mark.db
def test_ui_form_badges_and_lists(denv, ui):  # noqa: F811
    r = post(ui, "/tasks/new", project="KET", title="Sealed via UI", description="d", acceptance_criteria="a",
             status="defined", data_class="sealed")
    assert r.status_code == 200, r.text
    nr = r.url.path.rsplit("/", 1)[-1]
    assert _get(denv, nr)["data_class"] == "sealed"
    assert 'class="badge zone zone-sealed"' in r.text and "Versiegelt" in r.text
    assert '<option value="cloud_ok" selected' not in r.text
    kanban = ui.get("/kanban?owner=all").text
    assert 'zone-sealed zone-compact" data-class="sealed"' in kanban
    detail = ui.get(f"/tasks/{nr}").text
    row = _get(denv, nr)["row_version"]
    r = post(ui, f"/tasks/{nr}/edit", row_version=row, title="Sealed via UI", description="d", acceptance_criteria="a",
             prio=3, data_class="inherit")
    assert r.status_code == 200 and _get(denv, nr)["data_class"] is None
    assert "vom Projekt geerbt" in detail or "vom Projekt geerbt" in r.text
    assert "gemischt" not in ui.get("/projects").text
    mktask(denv, data_class="local_only")
    assert "gemischt" in ui.get("/projects").text and "gemischt" in ui.get("/projects/KET").text
    users = ui.get("/admin/users").text
    assert "Zone" in users and "zone-local_only" in users  # Local humans/agents: up to local_only or sealed
    # Rule 4 in the form: a started task offers no looser class.
    started = mktask(denv, assigned_to="hermes", data_class="local_only")["task_nr"]
    claim(denv, "hermes", started)
    page = ui.get(f"/tasks/{started}").text
    assert '<option value="cloud_ok" disabled>' in page


@pytest.mark.db
def test_cli_and_mcp(denv, tm_as):  # noqa: F811
    r = tm_as(denv["admin"], "create", "--project", "KET", "--title", "CLI", "--description", "d", "--acceptance",
              "a", "--data-class", "local_only")
    assert r.exit_code == 0, r.output
    nr = r.stdout.strip()
    assert _get(denv, nr)["data_class"] == "local_only"
    assert "- Data class: local_only (set on the task)" in tm_as(denv["admin"], "show", nr).stdout
    r = tm_as(denv["admin"], "update", nr, "--data-class", "inherit")
    assert r.exit_code == 0, r.output
    assert _get(denv, nr)["data_class"] is None
    assert tm_as(denv["admin"], "update", nr, "--data-class", "open").exit_code == 2
    r = tm_as(denv["orch"], "create", "--project", "LOC", "--title", "x", "--data-class", "cloud_ok")
    assert r.exit_code != 0
    mcp = TaskBrigadeMCP(client_mod.Client(client_mod.Config(
        "http://testserver", denv["admin"]["Authorization"].split()[1])), None)
    out = mcp.create_task("KET", "Per MCP", "D", "A", data_class="sealed")
    assert "Data class: sealed (set on the task)" in out, out
    mnr = out.split()[0]
    assert "Not changed" in mcp.update_task(mnr, data_class="weird")
    assert "Data class: local_only (set on the task)" in mcp.update_task(mnr, data_class="local_only")


@pytest.mark.db
@requires_db
def test_migration_roundtrip_keeps_tasks(empty_test_db):  # noqa: F811
    cfg = alembic_config()
    script = ScriptDirectory.from_config(cfg)
    revision = next(r for r in script.walk_revisions() if r.module.__file__.endswith("_task_data_class.py"))
    with empty_test_db.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, revision.down_revision)
        conn.execute(text("INSERT INTO projects (project_key, name, created_by, owner_id, privacy) "
                          "VALUES ('DEMO', 'Demo', 1, 1, 'local_only')"))
        conn.execute(text("INSERT INTO tasks (task_nr, project_id, title, status, created_by) "
                          "VALUES ('DEMO-0001', LAST_INSERT_ID(), 'Existing', 'in_progress', 1)"))
        query = text("SELECT task_nr, title, status FROM tasks")
        before = conn.execute(query).all()
        command.upgrade(cfg, revision.revision)
        assert conn.execute(query).all() == before
        assert conn.execute(text("SELECT data_class FROM tasks")).scalar_one() is None  # Inherits local_only
        for cls in zones.DATA_CLASSES:
            conn.execute(text("UPDATE tasks SET data_class=:c"), {"c": cls})
            assert conn.execute(text("SELECT data_class FROM tasks")).scalar_one() == cls
        command.downgrade(cfg, revision.down_revision)
        assert "data_class" not in {c["name"] for c in inspect(conn).get_columns("tasks")}
        assert conn.execute(query).all() == before
        command.upgrade(cfg, "head")
        assert conn.execute(query).all() == before


def test_zone_badge_colours_are_readable():
    """Badge text on its background keeps >= 4.5:1 (WCAG AA for small text)."""
    import re
    from pathlib import Path

    css = Path(__file__).resolve().parents[1].joinpath("app/static/app.css").read_text()

    def lum(hex_):
        rgb = [int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

    for zone in ("cloud", "local", "sealed"):
        fg = re.search(rf"--st-zone-{zone}: (#[0-9A-Fa-f]{{6}})", css).group(1)
        bg = re.search(rf"--st-zone-{zone}-bg: (#[0-9A-Fa-f]{{6}})", css).group(1)
        hi, lo = sorted((lum(fg), lum(bg)), reverse=True)
        assert (hi + 0.05) / (lo + 0.05) >= 4.5, zone


@pytest.mark.db
def test_workflow_activation_applies_the_class_rules(graph):  # noqa: F811
    """WF 2 stores the step class on the task; a looser step class needs an owner/admin template owner (rule 2)."""
    from app.services.workflows import engine as wf

    s, owner, agent, other, template, steps = graph
    agent.max_parallel_tasks = 5  # Several runs below keep their first task open.
    run = wf.start(s, owner, template, force_parallel=True)
    assert run.status == "running" and wf._latest(s, run)[1].task.data_class == "cloud_ok"
    template.project.privacy = "local_only"
    steps[0].data_class = "cloud_ok"  # A cloud code step in a local_only project, set by the admin owner.
    run = wf.start(s, owner, template, force_parallel=True)
    assert (run.status, run.pause_reason) == ("running", None)
    assert wf._latest(s, run)[1].task.data_class == "cloud_ok"
    template.owner_id = other.id  # An agent owning the template may not loosen.
    s.flush()
    s.expire(template, ["owner"])
    run = wf.start(s, owner, template, force_parallel=True)
    assert run.status == "paused" and run.pause_reason == "data_class_forbidden"
