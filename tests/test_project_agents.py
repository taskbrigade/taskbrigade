"""Creating and changing projects by agents: API, MCP `create_project`/`update_project` and `tbr project …` share
`projects.create`/`projects.update` with the web UI (orchestrators act on behalf of their owner)."""
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import db
from app.main import app
from app.models import Actor, DataAudit, Project, ProjectMember, Task
from app.services.gitea import GiteaClient, GiteaError
from tests.conftest import bearer, make_token
from tests.gitea_fake import SERVICE_USER
from tests.test_owner import human
from tests.test_ui_cli import tm_as  # noqa: F401 (Fixture)
from tests.test_workflow import claim, env, gitea, mkproject, mktask  # noqa: F401 (Fixtures)

pytestmark = pytest.mark.db


def _agent(name: str, role: str, owner: str | None) -> None:
    with db.session() as s:
        agent = s.scalar(select(Actor).where(Actor.name == name))
        agent.role = role
        agent.owner_id = s.scalar(select(Actor.id).where(Actor.name == owner)) if owner else None
        s.commit()


@pytest.fixture
def penv(env):
    """`anna` (member) owns the orchestrator agent `openclaw-agent`; `bob` is another member."""
    env["anna"] = human(env, "anna", gitea_username="anna-git")
    env["bob"] = human(env, "bob")
    _agent("openclaw-agent", "orchestrator", "anna")
    env["orch"] = bearer(make_token("openclaw-agent"))
    return env


def _project(key: str) -> Project | None:
    with db.session() as s:
        return s.scalar(select(Project).where(Project.project_key == key))


def _audit(action: str) -> list[dict]:
    with db.session() as s:
        rows = s.scalars(select(DataAudit).where(DataAudit.action == action).order_by(DataAudit.id))
        return [{"actor_id": r.actor_id, "via": r.via, **json.loads(r.detail_json)} for r in rows]


def _mcp_as(env, tmp_path, who):
    from cli.client import Client
    from cli.mcp_server import TaskBrigadeMCP

    cl = Client.__new__(Client)
    cl._http = TestClient(app, base_url="http://testserver/api/v1", client=("127.0.0.1", 40000), headers=env[who])
    return TaskBrigadeMCP(cl, tmp_path / who)


# --- Create ----------------------------------------------------------------------------------

def test_orchestrator_creates_project_like_the_web_ui(penv):
    c = penv["c"]
    r = c.post("/api/v1/projects", headers=penv["orch"], json={
        "key": "ecom", "name": "Shop", "description": "Webshop", "privacy": "cloud_ok", "status": "active",
        "gitea_mode": "create", "default_reviewer": "anna"})
    assert r.status_code == 201, r.text
    p = r.json()
    # The agent acts for its owner: the human becomes owner, never the agent.
    assert (p["key"], p["owner"], p["privacy"], p["default_reviewer"]) == ("ECOM", "anna", "cloud_ok", "anna")
    assert p["url"] == "https://tm.example/projects/ECOM" and p["gitea_setup"]["ok"]
    repo = penv["gitea"].repo(SERVICE_USER, "ecom")
    assert {"brain.md", "AGENTS.md", "CLAUDE.md", "brain-changes/README.md", "tasks/.gitkeep"} <= set(repo.files)
    assert repo.protections  # Branch protection as for a project created in the web UI.
    assert "anna-git" in repo.collaborators  # The owner becomes a collaborator (reviews pull requests).
    with db.session() as s:
        project = s.scalar(select(Project).where(Project.project_key == "ECOM"))
        agent_id = s.scalar(select(Actor.id).where(Actor.name == "openclaw-agent"))
        assert project.created_by == agent_id
        assert s.scalar(select(ProjectMember).where(ProjectMember.project_id == project.id)) is not None


def test_create_refusals(penv, monkeypatch):
    c = penv["c"]
    body = {"key": "NEU", "name": "Neu", "privacy": "cloud_ok"}
    r = c.post("/api/v1/projects", headers=penv["claude"], json=body)  # Worker agent: no role
    assert r.status_code == 403 and r.json()["error"]["code"] == "role_required"
    assert "orchestrator" in r.json()["error"]["message"]
    _agent("hermes", "orchestrator", None)  # Orchestrator without owner: nobody to act for
    r = c.post("/api/v1/projects", headers=bearer(make_token("hermes")), json=body)
    assert r.status_code == 403 and r.json()["error"]["code"] == "role_required"
    r = c.post("/api/v1/projects", headers=penv["orch"], json={**body, "owner": "bob"})  # Not its owner's project
    assert r.status_code == 403
    r = c.post("/api/v1/projects", headers=penv["orch"], json={**body, "owner": "claude-code"})  # Agent as owner
    assert r.status_code == 422
    r = c.post("/api/v1/projects", headers=penv["orch"], json={**body, "privacy": "sealed"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_privacy"
    r = c.post("/api/v1/projects", headers=penv["orch"], json={**body, "default_reviewer": "claude-code"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_reviewer"  # Role worker cannot review
    assert _project("NEU") is None

    # Duplicate key: clear message, the existing project stays as it was.
    mkproject(penv, key="DUP", privacy="local_only")
    r = c.post("/api/v1/projects", headers=penv["orch"], json={"key": "dup", "name": "Zweites", "privacy": "cloud_ok"})
    assert r.status_code == 409 and "DUP" in r.json()["error"]["message"]
    assert (_project("DUP").name, _project("DUP").privacy) == ("Projekt DUP", "local_only")

    # Gitea refuses the repository: nothing is saved.
    def fail(*a, **kw):
        raise GiteaError("500 boom")
    monkeypatch.setattr(GiteaClient, "create_repo", fail)
    r = c.post("/api/v1/projects", headers=penv["orch"], json={**body, "gitea_mode": "create", "status": "active"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "gitea_error"
    assert _project("NEU") is None


# --- Update ----------------------------------------------------------------------------------

def test_orchestrator_loosens_and_tightens_privacy(penv):
    c = penv["c"]
    mkproject(penv, privacy="local_only", owner="anna")
    started = mktask(penv, assigned_to="hermes")["task_nr"]
    assert claim(penv, who="hermes").status_code == 200
    fresh = mktask(penv, title="Noch offen")["task_nr"]

    r = c.patch("/api/v1/projects/KET", headers=penv["orch"], json={"privacy": "cloud_ok"})
    assert r.status_code == 200, r.text
    assert r.json()["privacy"] == "cloud_ok" and r.json()["changed"] == ["privacy"]
    with db.session() as s:  # As in the web UI: started tasks keep local_only, the others inherit cloud_ok.
        classes = {t.task_nr: t.data_class for t in s.scalars(select(Task))}
        agent_id = s.scalar(select(Actor.id).where(Actor.name == "openclaw-agent"))
    assert classes == {started: "local_only", fresh: None}
    assert _audit("privacy_changed") == [{"actor_id": agent_id, "via": "api", "project": "KET", "old": "local_only",
                                          "new": "cloud_ok"}]

    # Tightening is refused while open tasks sit with cloud agents; the reply names them.
    c.patch(f"/api/v1/tasks/{fresh}", headers=penv["admin"], json={"assigned_to": "claude-code"})
    r = c.patch("/api/v1/projects/KET", headers=penv["orch"], json={"privacy": "local_only"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "privacy_violation"
    assert fresh in r.json()["error"]["message"] and started not in r.json()["error"]["message"]
    c.patch(f"/api/v1/tasks/{fresh}", headers=penv["admin"], json={"assigned_to": None})
    r = c.patch("/api/v1/projects/KET", headers=penv["orch"], json={"privacy": "local_only"})
    assert r.status_code == 200 and r.json()["privacy"] == "local_only"
    assert [a["new"] for a in _audit("privacy_changed")] == ["cloud_ok", "local_only"]


def test_update_rights_and_only_given_fields(penv):
    c = penv["c"]
    mkproject(penv, privacy="local_only", owner="bob")
    r = c.patch("/api/v1/projects/KET", headers=penv["orch"], json={"privacy": "cloud_ok"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "role_required"  # anna is not an owner of KET
    r = c.patch("/api/v1/projects/KET", headers=penv["claude"], json={"privacy": "cloud_ok"})
    assert r.status_code == 403 and "orchestrator" in r.json()["error"]["message"]
    assert _project("KET").privacy == "local_only"

    # anna becomes co-owner → her orchestrator may act; only the given fields change.
    assert c.patch("/api/v1/projects/KET", headers=penv["admin"], json={"co_owners": ["anna"]}).status_code == 200
    r = c.patch("/api/v1/projects/KET", headers=penv["orch"], json={"name": "Neuer Name", "default_reviewer": "anna"})
    assert r.status_code == 200 and sorted(r.json()["changed"]) == ["default_reviewer", "name"]
    assert (r.json()["privacy"], r.json()["owner"], r.json()["default_reviewer"]) == ("local_only", "bob", "anna")
    r = c.patch("/api/v1/projects/KET", headers=penv["orch"], json={"default_reviewer": "none"})
    assert r.json()["default_reviewer"] is None
    r = c.patch("/api/v1/projects/KET", headers=penv["orch"], json={"owner": "openclaw-agent"})
    assert r.status_code == 422  # An agent never becomes owner.


# --- MCP and CLI ------------------------------------------------------------------------------

def test_mcp_create_and_update_project(penv, tmp_path):
    orch, worker = _mcp_as(penv, tmp_path, "orch"), _mcp_as(penv, tmp_path, "claude")
    out = orch.create_project("ecom", "Shop", "Webshop", privacy="local_only", default_reviewer="anna")
    assert "Project ECOM created." in out and "- Owner: anna" in out and "- Privacy: local_only" in out
    assert "- Default reviewer: anna" in out and "- Link: https://tm.example/projects/ECOM" in out
    assert "ECOM – Shop (Owner anna, local_only, default reviewer anna)" in orch.list_projects()
    assert "409" in orch.create_project("ECOM", "Nochmal")
    assert "sealed is a data class" in orch.create_project("SEAL", "x", privacy="sealed")
    out = worker.create_project("WORK", "Worker")
    assert "Project not created" in out and "403" in out and "orchestrator" in out

    out = orch.update_project("ecom", privacy="cloud_ok")
    assert "Project ECOM changed (privacy)." in out and "- Privacy: cloud_ok" in out
    assert "nothing changed" in orch.update_project("ECOM", privacy="cloud_ok")
    assert "Nothing to change" in orch.update_project("ECOM")
    out = worker.update_project("ECOM", privacy="local_only")
    assert "Not changed" in out and "403" in out and "orchestrator" in out
    out = orch.update_project("ECOM", default_reviewer="none", description="")
    assert "- Default reviewer: none" in out and _project("ECOM").description is None


def test_tbr_project_create_and_update(penv, tm_as):  # noqa: F811
    r = tm_as(penv["orch"], "project", "create", "--key", "cli", "--name", "Per CLI", "--privacy", "cloud_ok")
    assert r.exit_code == 0 and "Project CLI created." in r.stdout and "- Owner: anna" in r.stdout, r.output
    assert _project("CLI").gitea_repo == "cli" and _project("CLI").status == "active"
    r = tm_as(penv["orch"], "project", "create", "--key", "IDEA", "--name", "Idee", "--no-repo")
    assert r.exit_code == 0 and _project("IDEA").gitea_repo is None and _project("IDEA").status == "idea"
    r = tm_as(penv["claude"], "project", "create", "--key", "WRK", "--name", "x")
    assert r.exit_code == 1 and "403" in r.output
    r = tm_as(penv["orch"], "project", "create", "--key", "X1", "--name", "x", "--privacy", "sealed")
    assert r.exit_code == 2 and "sealed" in r.output

    r = tm_as(penv["orch"], "project", "update", "cli", "--privacy", "local_only", "--default-reviewer", "anna")
    assert r.exit_code == 0 and "Project CLI changed" in r.stdout and "- Privacy: local_only" in r.stdout, r.output
    assert tm_as(penv["orch"], "project", "update", "CLI").exit_code == 2
    r = tm_as(penv["orch"], "projects")
    assert "CLI" in r.stdout and "(default reviewer anna)" in r.stdout
    r = tm_as(penv["orch"], "project", "update", "NOPE", "--name", "x")
    assert r.exit_code == 1 and "404" in r.output and "Project not found" in r.output
