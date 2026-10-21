"""Step 3: projects, tasks, state machine, claiming, and Gitea integration (test DB + fake Gitea)."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import db
from app.main import app
from app.models import Actor
from app.services import settings_store
from app.services.gitea import GiteaClient, GiteaError
from tests.conftest import bearer, make_token
from tests.gitea_fake import SERVICE_USER, FakeGitea

pytestmark = pytest.mark.db
SHA = "a" * 40
SHA2 = "b" * 40


@pytest.fixture
def gitea(respx_mock):
    return FakeGitea(respx_mock)


@pytest.fixture
def env(ready_db, gitea):
    """Service token and default owner set; agents have Gitea users."""
    with db.session() as s:
        settings_store.update(s, {"gitea_token": "svc-token", "gitea_default_owner": SERVICE_USER,
                                  "app_url": "https://tm.example"}, None)
        for name in ("claude-code", "hermes", "openclaw-agent", "codex"):
            a = s.query(Actor).filter_by(name=name).one()
            a.gitea_username = f"{name}-bot"
        s.commit()
    with TestClient(app, client=("127.0.0.1", 40000)) as c:
        yield {
            "c": c, "gitea": gitea,
            "admin": bearer(make_token("admin")), "claude": bearer(make_token("claude-code")),
            "hermes": bearer(make_token("hermes")), "openclaw-agent": bearer(make_token("openclaw-agent")),
            "codex": bearer(make_token("codex")),
        }


def mkproject(e, key="KET", privacy="cloud_ok", mode="create", status="active", **kw):
    r = e["c"].post("/api/v1/projects", headers=e["admin"], json={
        "key": key, "name": f"Projekt {key}", "status": status, "privacy": privacy, "gitea_mode": mode, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def mktask(e, who="admin", **kw):
    # Defined requires a description and acceptance criteria (publish rule); use test defaults.
    body = {"project": "KET", "title": "Login bauen", "description": "Testbeschreibung",
            "acceptance_criteria": "- Test erfüllt", **kw}
    r = e["c"].post("/api/v1/tasks", headers=e[who], json=body)
    assert r.status_code == 201, r.text
    return r.json()


def claim(e, who="claude", task_nr=None):
    return e["c"].post("/api/v1/agents/me/claim", headers=e[who], json={"task_nr": task_nr} if task_nr else None)


def events(e, nr):
    return [(ev["type"], ev["from"], ev["to"]) for ev in e["c"].get(f"/api/v1/tasks/{nr}", headers=e["admin"]).json()["events"]]


# --- Projects ------------------------------------------------------------------------------

def test_project_create_with_repo(env):
    p = mkproject(env)
    assert p["gitea_setup"]["ok"] and p["gitea_repo"] == "ket" and p["clone_url"].endswith("/taskbrigade/ket.git")
    repo = env["gitea"].repo(SERVICE_USER, "ket")
    assert {"brain.md", "AGENTS.md", "CLAUDE.md", "tasks/.gitkeep"} <= set(repo.files)
    assert "# Projekt KET – Brain" in repo.files["brain.md"]
    assert "Lies zuerst `brain.md`" in repo.files["CLAUDE.md"]
    prot = repo.protections[0]
    assert prot["push_whitelist_usernames"] == [SERVICE_USER] and prot["enable_merge_whitelist"]
    assert env["c"].get("/api/v1/projects/KET/brain", headers=env["claude"]).text.startswith("# Projekt KET")


def test_project_link_keeps_existing_files(env):
    env["gitea"].add_repo("admin", "altprojekt", {"brain.md": "MEIN BRAIN"})
    r = env["c"].post("/api/v1/projects", headers=env["admin"], json={
        "key": "ALT", "name": "Alt", "status": "active", "gitea_mode": "link", "gitea_owner": "admin",
        "gitea_repo": "altprojekt"})
    assert r.status_code == 201
    repo = env["gitea"].repo("admin", "altprojekt")
    assert repo.files["brain.md"] == "MEIN BRAIN" and "AGENTS.md" in repo.files
    assert repo.protections == []  # Protection on linking is optional.


def test_project_link_missing_repo(env):
    r = env["c"].post("/api/v1/projects", headers=env["admin"], json={
        "key": "NOPE", "name": "x", "gitea_mode": "link", "gitea_owner": "admin", "gitea_repo": "gibtsnicht"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "repo_not_found"
    assert env["c"].get("/api/v1/projects/NOPE", headers=env["admin"]).status_code == 404


def test_setup_error_keeps_project(env):
    env["gitea"].files_fail = True
    p = mkproject(env)
    assert not p["gitea_setup"]["ok"] and "500" in p["gitea_setup"]["error"]
    env["gitea"].files_fail = False
    r = env["c"].post("/api/v1/projects/KET/gitea", headers=env["admin"], json={"mode": "link"})
    assert r.json()["gitea_setup"]["ok"]  # Repeat setup.
    assert "brain.md" in env["gitea"].repo(SERVICE_USER, "ket").files


def test_idea_without_repo_and_promotion(env):
    p = mkproject(env, key="IDEE", mode="none", status="idea")
    assert p["gitea_repo"] is None and p["status"] == "idea"
    r = env["c"].patch("/api/v1/projects/IDEE", headers=env["admin"], json={"status": "active"})
    assert r.status_code == 422
    r = env["c"].post("/api/v1/projects/IDEE/gitea", headers=env["admin"], json={"mode": "create", "activate": True})
    assert r.json()["status"] == "active" and r.json()["gitea_repo"] == "idee"


@pytest.mark.parametrize("body,status", [
    ({"key": "k", "name": "x"}, 422), ({"key": "TOOLONGKEY", "name": "x"}, 422),
    ({"key": "AB", "name": "x", "status": "active"}, 422),  # Active without a repo.
])
def test_project_validation(env, body, status):
    assert env["c"].post("/api/v1/projects", headers=env["admin"], json=body).status_code == status


def test_only_admin_creates_projects(env):
    r = env["c"].post("/api/v1/projects", headers=env["hermes"], json={"key": "XY", "name": "x"})
    assert r.status_code == 403


# --- Create/edit tasks ----------------------------------------------------------------

def test_task_numbering_per_project(env):
    mkproject(env)
    mkproject(env, key="ABC")
    assert [mktask(env)["task_nr"] for _ in range(2)] == ["KET-0001", "KET-0002"]
    assert mktask(env, project="ABC")["task_nr"] == "ABC-0001"


def test_worker_task_creation_rules(env):
    mkproject(env)
    assert mktask(env, who="hermes")["assigned_to"] is None
    assert mktask(env, who="hermes", assigned_to="hermes")["assigned_to"] == "hermes"
    r = env["c"].post("/api/v1/tasks", headers=env["hermes"], json={"project": "KET", "title": "x",
                                                                     "assigned_to": "openclaw-agent"})
    assert r.status_code == 403


def test_privacy_blocks_cloud_agents(env):
    mkproject(env, privacy="local_only")
    r = env["c"].post("/api/v1/tasks", headers=env["admin"], json={"project": "KET", "title": "x",
                                                                    "assigned_to": "codex", "description": "d",
                                                                    "acceptance_criteria": "- a"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "privacy_violation"
    t = mktask(env, assigned_to="hermes")
    r = env["c"].patch(f"/api/v1/tasks/{t['task_nr']}", headers=env["admin"], json={"assigned_to": "claude-code"})
    assert r.status_code == 422
    r = env["c"].put("/api/v1/admin/projects/KET/members/codex", headers=env["admin"], json={})
    assert r.status_code == 422


def test_assignment_creates_membership_and_collaborator(env):
    mkproject(env)
    mktask(env, assigned_to="claude-code")
    assert env["gitea"].repo(SERVICE_USER, "ket").collaborators == {"claude-code-bot": "write"}
    m = env["c"].get("/api/v1/projects/KET/members", headers=env["admin"]).json()
    assert m[0]["actor"] == "claude-code" and m[0]["gitea_synced_at"] and m[0]["sync_error"] is None


def test_deactivation_revokes_collaborator(env):
    mkproject(env)
    mktask(env, assigned_to="openclaw-agent")
    env["c"].patch("/api/v1/admin/actors/openclaw-agent", headers=env["admin"], json={"is_active": False})
    assert env["gitea"].repo(SERVICE_USER, "ket").collaborators == {}


def test_member_api_put_delete(env):
    mkproject(env)
    r = env["c"].put("/api/v1/admin/projects/KET/members/hermes", headers=env["admin"],
                     json={"gitea_permission": "read"})
    assert r.json()["gitea_permission"] == "read"
    assert env["gitea"].repo(SERVICE_USER, "ket").collaborators["hermes-bot"] == "read"
    assert env["c"].delete("/api/v1/admin/projects/KET/members/hermes", headers=env["admin"]).status_code == 204
    assert "hermes-bot" not in env["gitea"].repo(SERVICE_USER, "ket").collaborators


def test_patch_with_if_match(env):
    mkproject(env)
    t = mktask(env)
    nr = t["task_nr"]
    r = env["c"].patch(f"/api/v1/tasks/{nr}", headers={**env["admin"], "If-Match": str(t["row_version"])},
                       json={"prio": 1, "title": "Neu"})
    assert r.status_code == 200 and r.json()["prio"] == 1 and r.json()["row_version"] == t["row_version"] + 1
    r = env["c"].patch(f"/api/v1/tasks/{nr}", headers={**env["admin"], "If-Match": str(t["row_version"])},
                       json={"prio": 2})
    assert r.status_code == 409 and r.json()["error"]["code"] == "version_conflict"
    assert ("prio_change", "3", "1") in events(env, nr)
    assert env["c"].patch(f"/api/v1/tasks/{nr}", headers=env["hermes"], json={"prio": 2}).status_code == 403


def test_backlog_filters(env):
    mkproject(env)
    mktask(env, title="Alpha", prio=1, assigned_to="hermes")
    mktask(env, title="Beta", prio=4)
    c, h = env["c"], env["admin"]
    assert c.get("/api/v1/tasks?assigned_to=none", headers=h).json()["total"] == 1
    assert [t["title"] for t in c.get("/api/v1/tasks?sort=prio", headers=h).json()["items"]] == ["Alpha", "Beta"]
    assert c.get("/api/v1/tasks?q=Bet", headers=h).json()["items"][0]["title"] == "Beta"
    assert c.get("/api/v1/tasks?status=defined,done&prio=4", headers=h).json()["total"] == 1
    assert c.get("/api/v1/tasks?assigned_to=me", headers=env["hermes"]).json()["total"] == 1


# --- Claim ----------------------------------------------------------------------------------

def test_claim_nothing_returns_204(env):
    mkproject(env)
    assert claim(env).status_code == 204


def test_claim_order_prio_then_age(env):
    mkproject(env)
    mktask(env, title="alt, prio 3", assigned_to="claude-code")
    mktask(env, title="neu, prio 1", assigned_to="claude-code", prio=1)
    r = claim(env)
    assert r.status_code == 200 and r.json()["title"] == "neu, prio 1"
    assert r.json()["status"] == "in_progress" and r.json()["lease_expires_at"]
    assert r.json()["branch_name"] == "task/KET-0002-neu-prio-1"
    assert r.json()["project_info"]["clone_url"].endswith("ket.git")


def test_claim_respects_max_parallel_and_explicit_task(env):
    mkproject(env)
    a = mktask(env, assigned_to="claude-code")
    b = mktask(env, assigned_to="claude-code")
    assert claim(env, task_nr=b["task_nr"]).json()["task_nr"] == b["task_nr"]
    assert claim(env).status_code == 204  # max_parallel_tasks = 1
    env["c"].patch("/api/v1/admin/actors/claude-code", headers=env["admin"], json={"max_parallel_tasks": 2})
    assert claim(env).json()["task_nr"] == a["task_nr"]


def test_claim_blocked_by_dependencies_and_inactive_project(env):
    mkproject(env)
    dep = mktask(env, assigned_to="hermes")
    t = mktask(env, assigned_to="claude-code", depends_on=[dep["task_nr"]])
    assert claim(env).status_code == 204
    r = claim(env, task_nr=t["task_nr"])
    assert r.status_code == 409 and r.json()["error"]["code"] == "blocked"
    env["c"].post(f"/api/v1/tasks/{dep['task_nr']}/transition", headers=env["admin"],
                  json={"to": "done", "override": True})
    env["c"].patch("/api/v1/projects/KET", headers=env["admin"], json={"status": "paused"})
    assert claim(env).status_code == 204
    env["c"].patch("/api/v1/projects/KET", headers=env["admin"], json={"status": "active"})
    assert claim(env).json()["task_nr"] == t["task_nr"]


def test_dependencies_refuse_self_unknown_and_circle(env):
    mkproject(env)
    a, b = mktask(env, assigned_to="hermes"), mktask(env, assigned_to="hermes")
    url = lambda t: f"/api/v1/tasks/{t['task_nr']}"  # noqa: E731

    def patch(t, deps):
        row = env["c"].get(url(t), headers=env["admin"]).json()["row_version"]
        return env["c"].patch(url(t), headers={**env["admin"], "If-Match": str(row)}, json={"depends_on": deps})

    assert patch(a, [a["task_nr"]]).status_code == 422
    assert patch(a, ["KET-9999"]).status_code == 404
    assert patch(b, [a["task_nr"]]).status_code == 200
    r = patch(a, [b["task_nr"]])  # a -> b -> a would never become claimable
    assert r.status_code == 422 and a["task_nr"] in r.text  # message language varies (DE/EN)
    assert patch(b, []).status_code == 200 and patch(a, [b["task_nr"]]).status_code == 200


def test_claim_foreign_task_forbidden(env):
    mkproject(env)
    t = mktask(env, assigned_to="hermes")
    assert claim(env, task_nr=t["task_nr"]).status_code == 403


def test_heartbeat_extends_lease(env):
    mkproject(env)
    t = mktask(env, assigned_to="claude-code")
    claim(env)
    with db.engine.begin() as conn:
        conn.execute(text("UPDATE tasks SET lease_expires_at = UTC_TIMESTAMP() WHERE task_nr = :n"), {"n": t["task_nr"]})
    r = env["c"].post(f"/api/v1/tasks/{t['task_nr']}/heartbeat", headers=env["claude"])
    assert r.status_code == 200
    assert env["c"].post(f"/api/v1/tasks/{t['task_nr']}/heartbeat", headers=env["hermes"]).status_code == 403


# --- Approval workflow (API level; CLI follows in step 4) --------------------------------------

def test_full_workflow(env):
    c, g = env["c"], env["gitea"]
    mkproject(env)
    nr = mktask(env, title="Login bauen", assigned_to="claude-code", acceptance_criteria="- geht")["task_nr"]

    t = claim(env).json()
    assert t["task_nr"] == nr and t["status"] == "in_progress"
    r = c.post(f"/api/v1/tasks/{nr}/ask", headers=env["claude"], json={"body": "OAuth oder Passwort?"})
    assert r.json()["status"] == "questions" and r.json()["lease_expires_at"] is None

    inbox_q = c.get("/api/v1/tasks?status=questions", headers=env["admin"]).json()["items"]
    assert [x["task_nr"] for x in inbox_q] == [nr]
    assert c.post(f"/api/v1/tasks/{nr}/answer", headers=env["claude"], json={"body": "x"}).status_code == 403
    r = c.post(f"/api/v1/tasks/{nr}/answer", headers=env["admin"], json={"body": "Passwort."})
    assert r.json()["status"] == "defined"

    t = claim(env).json()
    assert [(x["type"], x["body"]) for x in t["comments"]] == [("question", "OAuth oder Passwort?"),
                                                               ("answer", "Passwort.")]
    branch = t["branch_name"]
    r = c.post(f"/api/v1/tasks/{nr}/submit", headers=env["claude"],
               json={"branch_name": branch, "commit_sha": SHA, "summary": "Login fertig"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "branch_not_found"
    g.push_branch(SERVICE_USER, "ket", branch)
    r = c.post(f"/api/v1/tasks/{nr}/submit", headers=env["claude"],
               json={"branch_name": branch, "commit_sha": SHA, "summary": "Login fertig"})
    assert r.status_code == 200, r.text
    t = r.json()
    assert t["status"] == "for_review" and t["pr_url"].endswith("/pulls/1") and t["result_commit_sha"] == SHA
    pr = g.repo(SERVICE_USER, "ket").pulls[1]
    assert pr["title"] == f"[{nr}] Login bauen" and "https://tm.example/tasks/KET-0001" in pr["body"]

    r = c.post(f"/api/v1/tasks/{nr}/review", headers=env["admin"], json={"decision": "approve"})
    assert r.status_code == 200 and r.json()["status"] == "done" and r.json()["done_at"]
    assert r.json()["merge_commit_sha"] == "c" * 40
    assert r.json()["result_commit_sha"] == SHA
    assert c.get(f"/api/v1/tasks/{nr}", headers=env["admin"]).json()["merge_commit_sha"] == "c" * 40
    assert pr["merged"] and pr["merge_style"] == "squash" and branch not in g.repo(SERVICE_USER, "ket").branches

    assert events(env, nr) == [
        ("created", None, "defined"), ("assigned", None, "claude-code"),
        ("status_change", "defined", "in_progress"), ("status_change", "in_progress", "questions"),
        ("status_change", "questions", "defined"), ("status_change", "defined", "in_progress"),
        ("pr_created", None, "1"), ("status_change", "in_progress", "for_review"),
        ("merged", None, "1"), ("status_change", "for_review", "done"),
    ]


def test_reject_and_resubmit_reuses_pr(env):
    c, g = env["c"], env["gitea"]
    mkproject(env)
    nr = mktask(env, assigned_to="claude-code")["task_nr"]
    branch = claim(env).json()["branch_name"]
    g.push_branch(SERVICE_USER, "ket", branch)
    c.post(f"/api/v1/tasks/{nr}/submit", headers=env["claude"], json={"branch_name": branch, "commit_sha": SHA,
                                                                      "summary": "v1"})
    r = c.post(f"/api/v1/tasks/{nr}/review", headers=env["admin"], json={"decision": "reject"})
    assert r.status_code == 422  # Review comment required.
    r = c.post(f"/api/v1/tasks/{nr}/review", headers=env["admin"], json={"decision": "reject", "comment": "Tests fehlen"})
    assert r.json()["status"] == "changes_requested"
    assert claim(env).json()["branch_name"] == branch  # Preserve existing branch.
    r = c.post(f"/api/v1/tasks/{nr}/submit", headers=env["claude"], json={"branch_name": branch, "commit_sha": SHA2,
                                                                          "summary": "v2 mit Tests"})
    assert r.json()["pr_number"] == 1 and len(g.repo(SERVICE_USER, "ket").pulls) == 1
    assert g.repo(SERVICE_USER, "ket").comments[-1][0] == 1 and "v2 mit Tests" in g.repo(SERVICE_USER, "ket").comments[-1][1]


def test_merge_conflict_keeps_for_review(env):
    c, g = env["c"], env["gitea"]
    mkproject(env)
    nr = mktask(env, assigned_to="claude-code")["task_nr"]
    branch = claim(env).json()["branch_name"]
    g.push_branch(SERVICE_USER, "ket", branch)
    c.post(f"/api/v1/tasks/{nr}/submit", headers=env["claude"], json={"branch_name": branch, "commit_sha": SHA,
                                                                      "summary": "x"})
    g.merge_fails = True
    r = c.post(f"/api/v1/tasks/{nr}/review", headers=env["admin"], json={"decision": "approve"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "merge_failed" and "rebase" in r.json()["error"]["message"]
    assert c.get(f"/api/v1/tasks/{nr}", headers=env["admin"]).json()["status"] == "for_review"


def test_approval_recovers_merge_sha_after_failed_read(env, monkeypatch):
    c, g = env["c"], env["gitea"]
    mkproject(env)
    nr = mktask(env, assigned_to="claude-code")["task_nr"]
    branch = claim(env).json()["branch_name"]
    g.push_branch(SERVICE_USER, "ket", branch)
    c.post(f"/api/v1/tasks/{nr}/submit", headers=env["claude"], json={"branch_name": branch,
           "commit_sha": SHA, "summary": "Done"})
    read_sha = GiteaClient._merged_pull_sha
    calls = 0

    def fail_once(self, *args):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise GiteaError("Temporary read failure")
        return read_sha(self, *args)

    monkeypatch.setattr(GiteaClient, "_merged_pull_sha", fail_once)
    r = c.post(f"/api/v1/tasks/{nr}/review", headers=env["admin"], json={"decision": "approve"})
    assert r.status_code == 409
    assert g.repo(SERVICE_USER, "ket").pulls[1]["merged"]
    task = c.get(f"/api/v1/tasks/{nr}", headers=env["admin"]).json()
    assert task["status"] == "for_review" and task["merge_commit_sha"] is None
    r = c.post(f"/api/v1/tasks/{nr}/review", headers=env["admin"], json={"decision": "approve"})
    assert r.status_code == 200 and r.json()["status"] == "done"
    task = c.get(f"/api/v1/tasks/{nr}", headers=env["admin"]).json()
    assert task["merge_commit_sha"] == "c" * 40 and task["result_commit_sha"] == SHA


@pytest.mark.parametrize("body,code", [
    ({"branch_name": "main", "commit_sha": SHA, "summary": "x"}, "invalid_branch"),
    ({"branch_name": "task/x", "commit_sha": "kurz", "summary": "x"}, "invalid_sha"),
])
def test_submit_validation(env, body, code):
    mkproject(env)
    nr = mktask(env, assigned_to="claude-code")["task_nr"]
    claim(env)
    r = env["c"].post(f"/api/v1/tasks/{nr}/submit", headers=env["claude"], json=body)
    assert r.status_code == 422 and r.json()["error"]["code"] == code


def test_release_and_cancel_permissions(env):
    c = env["c"]
    mkproject(env)
    nr = mktask(env, assigned_to="claude-code")["task_nr"]
    claim(env)
    assert c.post(f"/api/v1/tasks/{nr}/release", headers=env["claude"], json={"reason": ""}).status_code == 422
    r = c.post(f"/api/v1/tasks/{nr}/release", headers=env["claude"], json={"reason": "Kein Zugriff auf X"})
    assert r.json()["status"] == "defined" and "Kein Zugriff auf X" in r.json()["comments"][-1]["body"]
    assert c.post(f"/api/v1/tasks/{nr}/transition", headers=env["claude"], json={"to": "cancelled"}).status_code == 403
    env["c"].patch("/api/v1/admin/actors/hermes", headers=env["admin"], json={"role": "orchestrator"})
    r = c.post(f"/api/v1/tasks/{nr}/transition", headers=env["hermes"], json={"to": "cancelled"})
    assert r.json()["status"] == "cancelled"
    r = c.post(f"/api/v1/tasks/{nr}/transition", headers=env["admin"], json={"to": "defined"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "invalid_transition"


def test_admin_override(env):
    c = env["c"]
    mkproject(env)
    nr = mktask(env, assigned_to="claude-code")["task_nr"]
    r = c.post(f"/api/v1/tasks/{nr}/transition", headers=env["hermes"], json={"to": "for_review", "override": True})
    assert r.status_code == 403
    r = c.post(f"/api/v1/tasks/{nr}/transition", headers=env["admin"], json={"to": "in_progress", "override": True})
    assert r.json()["status"] == "in_progress" and r.json()["lease_expires_at"]
    ev = c.get(f"/api/v1/tasks/{nr}", headers=env["admin"]).json()["events"][-1]
    assert ev["note"] == "manual override" and ev["to"] == "in_progress"


def test_lease_sweep(env):
    from app.scheduler import lease_sweep

    mkproject(env)
    nr = mktask(env, assigned_to="claude-code")["task_nr"]
    claim(env)
    lease_sweep()
    assert env["c"].get(f"/api/v1/tasks/{nr}", headers=env["admin"]).json()["status"] == "in_progress"
    with db.engine.begin() as conn:
        conn.execute(text("UPDATE tasks SET lease_expires_at = UTC_TIMESTAMP() - INTERVAL 1 MINUTE"))
    lease_sweep()
    t = env["c"].get(f"/api/v1/tasks/{nr}", headers=env["admin"]).json()
    assert t["status"] == "defined" and t["events"][-1]["type"] == "lease_expired" and t["events"][-1]["actor"] is None
    with db.session() as s:
        from app.services.tasks import inbox
        assert [x["task_nr"] for x in inbox(s)["lease_expired"]] == [nr]


def test_spec_file_creation(env):
    mkproject(env)
    t = mktask(env, title="Mit Spec", description="Details", create_spec_file=True)
    assert t["detail_task_git_url"].endswith("/src/branch/main/tasks/KET-0001.md")
    assert "Details" in env["gitea"].repo(SERVICE_USER, "ket").files["tasks/KET-0001.md"]


def test_subtasks_and_dependencies_in_payload(env):
    mkproject(env)
    parent = mktask(env, title="Epic")
    a = mktask(env, title="Teil A", parent_task_nr=parent["task_nr"])
    b = mktask(env, title="Teil B", parent_task_nr=parent["task_nr"], depends_on=[a["task_nr"]])
    p = env["c"].get(f"/api/v1/tasks/{parent['task_nr']}", headers=env["admin"]).json()
    assert [s["task_nr"] for s in p["subtasks"]] == [a["task_nr"], b["task_nr"]]
    assert b["dependencies"] == [{"task_nr": a["task_nr"], "status": "defined", "title": "Teil A",
                               "resolved": False}]
    assert b["parent_task_nr"] == parent["task_nr"]


# --- Agent abort and reassignment by human admins -------------------------------

def test_agent_abort_waits_for_human(env):
    c = env["c"]
    mkproject(env)
    nr = mktask(env, assigned_to="claude-code")["task_nr"]
    assert c.post(f"/api/v1/tasks/{nr}/abort", headers=env["claude"], json={"reason": "x"}).status_code == 409
    claim(env)
    assert c.post(f"/api/v1/tasks/{nr}/abort", headers=env["claude"], json={"reason": ""}).status_code == 422
    r = c.post(f"/api/v1/tasks/{nr}/abort", headers=env["claude"], json={"reason": "Modell zu schwach für Refactoring"})
    assert r.json()["status"] == "aborted" and r.json()["lease_expires_at"] is None
    assert "Modell zu schwach" in r.json()["comments"][-1]["body"]
    assert claim(env).status_code == 204  # Same agent cannot claim it again.
    with db.session() as s:
        from app.services.tasks import inbox
        assert [x["task_nr"] for x in inbox(s)["aborted"]] == [nr]


def test_human_admin_reassigns_running_task(env):
    c, g = env["c"], env["gitea"]
    mkproject(env)
    nr = mktask(env, assigned_to="claude-code")["task_nr"]
    branch = claim(env).json()["branch_name"]
    r = c.patch(f"/api/v1/tasks/{nr}", headers=env["admin"], json={"assigned_to": "hermes"})
    assert r.status_code == 200, r.text
    t = r.json()
    assert t["status"] == "defined" and t["assigned_to"] == "hermes" and t["lease_expires_at"] is None
    ev = [(e["type"], e["from"], e["to"], e["note"]) for e in t["events"]][-2:]
    assert ev == [("status_change", "in_progress", "defined", "neu zugewiesen an hermes"),
                  ("assigned", "claude-code", "hermes", None)]
    assert c.post(f"/api/v1/tasks/{nr}/heartbeat", headers=env["claude"]).status_code == 403  # Old agent removed.
    t = claim(env, who="hermes").json()
    assert t["task_nr"] == nr and t["branch_name"] == branch  # Branch is preserved.
    assert "hermes-bot" in g.repo(SERVICE_USER, "ket").collaborators


def test_reassign_aborted_and_for_review(env):
    c, g = env["c"], env["gitea"]
    mkproject(env)
    nr = mktask(env, assigned_to="claude-code")["task_nr"]
    branch = claim(env).json()["branch_name"]
    g.push_branch(SERVICE_USER, "ket", branch)
    c.post(f"/api/v1/tasks/{nr}/submit", headers=env["claude"], json={"branch_name": branch, "commit_sha": SHA,
                                                                      "summary": "halb fertig"})
    r = c.patch(f"/api/v1/tasks/{nr}", headers=env["admin"], json={"assigned_to": "openclaw-agent"})
    assert r.json()["status"] == "defined" and r.json()["pr_number"] == 1  # PR remains for new agent.
    claim(env, who="openclaw-agent")
    c.post(f"/api/v1/tasks/{nr}/abort", headers=env["openclaw-agent"], json={"reason": "zu komplex"})
    r = c.patch(f"/api/v1/tasks/{nr}", headers=env["admin"], json={"assigned_to": "hermes"})
    assert r.json()["status"] == "defined" and r.json()["assigned_to"] == "hermes"


def test_only_human_admins_reassign_running_tasks(env):
    c = env["c"]
    mkproject(env)
    nr = mktask(env, assigned_to="claude-code")["task_nr"]
    claim(env)
    c.patch("/api/v1/admin/actors/hermes", headers=env["admin"], json={"role": "orchestrator"})
    r = c.patch(f"/api/v1/tasks/{nr}", headers=env["hermes"], json={"assigned_to": "openclaw-agent"})
    assert r.status_code == 403
    c.patch("/api/v1/admin/actors/hermes", headers=env["admin"], json={"role": "admin"})  # Agent with admin role.
    assert c.patch(f"/api/v1/tasks/{nr}", headers=env["hermes"], json={"assigned_to": "openclaw-agent"}).status_code == 403
    r = c.post(f"/api/v1/tasks/{nr}/transition", headers=env["hermes"], json={"to": "done", "override": True})
    assert r.status_code == 403
    r = c.post(f"/api/v1/tasks/{nr}/transition", headers=env["hermes"], json={"to": "aborted", "comment": "x"})
    assert r.status_code == 403  # Other agent cannot abort, even with admin role.
    nr2 = mktask(env, assigned_to="claude-code")["task_nr"]  # Orchestrator/admin may reassign defined tasks.
    assert c.patch(f"/api/v1/tasks/{nr2}", headers=env["hermes"], json={"assigned_to": "openclaw-agent"}).status_code == 200


def test_reopen_and_admin_abort(env):
    c = env["c"]
    mkproject(env)
    nr = mktask(env, assigned_to="claude-code")["task_nr"]
    claim(env)
    r = c.post(f"/api/v1/tasks/{nr}/transition", headers=env["admin"], json={"to": "aborted", "comment": "hängt"})
    assert r.json()["status"] == "aborted"
    r = c.post(f"/api/v1/tasks/{nr}/transition", headers=env["admin"], json={"to": "defined",
                                                                             "comment": "Mit mehr Kontext nochmal"})
    assert r.json()["status"] == "defined" and r.json()["assigned_to"] == "claude-code"
    assert claim(env).json()["task_nr"] == nr
    assert c.patch(f"/api/v1/tasks/{nr}", headers=env["admin"], json={}).status_code == 200


def test_done_task_not_reassignable(env):
    mkproject(env)
    nr = mktask(env, assigned_to="claude-code")["task_nr"]
    env["c"].post(f"/api/v1/tasks/{nr}/transition", headers=env["admin"], json={"to": "done", "override": True})
    assert env["c"].patch(f"/api/v1/tasks/{nr}", headers=env["admin"], json={"assigned_to": "hermes"}).status_code == 409


def test_switch_to_cloud_blocked_by_local_only(env):
    """Changing runtime from local to cloud must preserve the privacy rule."""
    c = env["c"]
    mkproject(env, privacy="local_only")
    nr = mktask(env, assigned_to="hermes")["task_nr"]
    r = c.patch("/api/v1/admin/actors/hermes", headers=env["admin"], json={"runtime": "cloud"})
    assert r.status_code == 422 and "KET" in r.json()["error"]["message"]
    c.patch(f"/api/v1/tasks/{nr}", headers=env["admin"], json={"assigned_to": "openclaw-agent"})
    r = c.patch("/api/v1/admin/actors/hermes", headers=env["admin"], json={"runtime": "cloud"})
    assert r.status_code == 422  # Membership from assignment still exists.
    c.delete("/api/v1/admin/projects/KET/members/hermes", headers=env["admin"])
    assert c.patch("/api/v1/admin/actors/hermes", headers=env["admin"], json={"runtime": "cloud"}).status_code == 200


# --- Draft ----------------------------------------------------------------------------

def test_draft_is_never_claimed_until_published(env):
    c = env["c"]
    mkproject(env)
    nr = mktask(env, assigned_to="claude-code", status="draft")["task_nr"]
    assert claim(env).status_code == 204  # Draft is not delivered …
    r = claim(env, task_nr=nr)
    assert r.status_code == 409 and "draft" in r.json()["error"]["message"]  # … even by explicit claim.
    r = c.post(f"/api/v1/tasks/{nr}/transition", headers=env["admin"], json={"to": "defined"})
    assert r.json()["status"] == "defined"
    assert claim(env).json()["task_nr"] == nr
    ev = [(e["type"], e["from"], e["to"], e["note"]) for e in c.get(f"/api/v1/tasks/{nr}", headers=env["admin"]).json()["events"]]
    assert ev[0] == ("created", None, "draft", None) and ("status_change", "draft", "defined", "publish") in ev


def test_draft_permissions(env):
    c = env["c"]
    mkproject(env)
    own = mktask(env, who="hermes", status="draft")["task_nr"]  # Worker creates own draft.
    other = mktask(env, status="draft")["task_nr"]
    assert c.post(f"/api/v1/tasks/{other}/transition", headers=env["hermes"], json={"to": "defined"}).status_code == 403
    assert c.post(f"/api/v1/tasks/{own}/transition", headers=env["hermes"], json={"to": "defined"}).json()["status"] == "defined"
    # Return to draft: admin/orchestrator only, before work begins.
    assert c.post(f"/api/v1/tasks/{own}/transition", headers=env["hermes"], json={"to": "draft"}).status_code == 403
    assert c.post(f"/api/v1/tasks/{own}/transition", headers=env["admin"], json={"to": "draft"}).json()["status"] == "draft"
    running = mktask(env, assigned_to="claude-code")["task_nr"]
    claim(env)
    r = c.post(f"/api/v1/tasks/{running}/transition", headers=env["admin"], json={"to": "draft"})
    assert r.status_code == 409


def test_draft_can_be_edited_assigned_and_cancelled(env):
    c = env["c"]
    mkproject(env, privacy="local_only")
    t = mktask(env, status="draft", title="Rohfassung")
    nr = t["task_nr"]
    r = c.patch(f"/api/v1/tasks/{nr}", headers=env["admin"], json={"title": "Ausgearbeitet", "assigned_to": "hermes",
                                                                    "description": "Details"})
    assert r.status_code == 200 and r.json()["status"] == "draft" and r.json()["assigned_to"] == "hermes"
    r = c.patch(f"/api/v1/tasks/{nr}", headers=env["admin"], json={"assigned_to": "codex"})
    assert r.status_code == 422  # Privacy also applies to drafts.
    assert c.post(f"/api/v1/tasks/{nr}/transition", headers=env["admin"], json={"to": "cancelled"}).json()["status"] == "cancelled"
    assert c.post("/api/v1/tasks", headers=env["admin"], json={"project": "KET", "title": "x", "status": "done"}).status_code == 422



# --- Publish only with description and acceptance criteria ---------------------------------------

def test_definition_required_for_defined(env):
    c = env["c"]
    mkproject(env)
    r = c.post("/api/v1/tasks", headers=env["admin"], json={"project": "KET", "title": "Ohne Inhalt"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "definition_incomplete"
    assert "Beschreibung und Akzeptanzkriterien" in r.json()["error"]["message"]
    nr = mktask(env, status="draft", description="", acceptance_criteria="")["task_nr"]  # Draft may be empty.
    r = c.post(f"/api/v1/tasks/{nr}/transition", headers=env["admin"], json={"to": "defined"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "definition_incomplete"
    r = c.post(f"/api/v1/tasks/{nr}/transition", headers=env["admin"], json={"to": "defined", "override": True})
    assert r.status_code == 422  # Override does not bypass this.
    c.patch(f"/api/v1/tasks/{nr}", headers=env["admin"], json={"description": "Jetzt mit Text"})
    r = c.post(f"/api/v1/tasks/{nr}/transition", headers=env["admin"], json={"to": "defined"})
    assert r.status_code == 422 and "Akzeptanzkriterien" in r.json()["error"]["message"] \
        and "Beschreibung" not in r.json()["error"]["message"].split(":")[1]
    c.patch(f"/api/v1/tasks/{nr}", headers=env["admin"], json={"acceptance_criteria": "- erfüllt"})
    assert c.post(f"/api/v1/tasks/{nr}/transition", headers=env["admin"], json={"to": "defined"}).json()["status"] == "defined"
    # Returning to defined from active statuses is unchanged (legacy tasks without criteria).
    running = mktask(env, assigned_to="claude-code")["task_nr"]
    with db.engine.begin() as conn:
        conn.execute(text("UPDATE tasks SET acceptance_criteria = NULL WHERE task_nr = :n"), {"n": running})
    claim(env)
    r = c.post(f"/api/v1/tasks/{running}/release", headers=env["claude"], json={"reason": "zurück"})
    assert r.json()["status"] == "defined"


# --- Create tasks from Claude Desktop/Cowork (MCP create_task) -----------------------------------

def test_mcp_create_task_against_api(env, tmp_path):
    from fastapi.testclient import TestClient as TC

    from cli.client import Client
    from cli.mcp_server import TaskBrigadeMCP

    def mcp_as(who):
        cl = Client.__new__(Client)
        cl._http = TC(app, base_url="http://testserver/api/v1", client=("127.0.0.1", 40000), headers=env[who])
        return TaskBrigadeMCP(cl, tmp_path / who)

    mkproject(env)
    mkproject(env, key="PRIV", privacy="local_only")
    # Directory has no contact details or credentials and is readable by all actors.
    actors = env["c"].get("/api/v1/actors", headers=env["claude"]).json()
    assert {"openclaw-agent", "admin"} <= {a["name"] for a in actors}
    assert not any(k in actors[0] for k in ("email", "api_key_prefix", "tailscale_login"))

    worker = mcp_as("claude")
    assert "KET – Projekt KET (Owner admin, cloud_ok)" in worker.list_projects()
    assert "openclaw-agent" in worker.list_agents() and "Humans:" in worker.list_agents()
    out = worker.create_task("ket", "Für openclaw-agent", "Beschreibung", "- erfüllt", assigned_to="openclaw-agent")
    assert "Task not created" in out and "orchestrator" in out  # Workers may not assign others.
    out = worker.create_task("KET", "Idee", draft=True)
    assert "created" in out and "draft" in out

    env["c"].patch("/api/v1/admin/actors/claude-code", headers=env["admin"], json={"role": "orchestrator"})
    out = worker.create_task("KET", "Ohne Kriterien", "nur Text", assigned_to="openclaw-agent")
    assert "Task not created" in out and "Akzeptanzkriterien" in out  # API message (German)
    out = worker.create_task("KET", "Für openclaw-agent", "Beschreibung", "- erfüllt", assigned_to="openclaw-agent", prio=2)
    nr = out.split()[0]
    assert "assigned to openclaw-agent, defined, ready to claim" in out
    t = env["c"].get(f"/api/v1/tasks/{nr}", headers=env["admin"]).json()
    assert t["status"] == "defined" and t["assigned_to"] == "openclaw-agent" and t["prio"] == 2
    assert t["created_by"] == "claude-code"
    out = worker.create_task("PRIV", "Geheim", "x", "- y", assigned_to="codex")
    assert "Task not created" in out  # Privacy: local_only cannot go to cloud agents.
    assert "not assigned to anyone" in worker.create_task("KET", "Offen", "x", "- y")


def test_merge_conflict_detected_and_returned_to_agent(env):
    c, g = env["c"], env["gitea"]
    mkproject(env)
    nr = mktask(env, assigned_to="claude-code")["task_nr"]
    branch = claim(env).json()["branch_name"]
    g.push_branch(SERVICE_USER, "ket", branch)
    c.post(f"/api/v1/tasks/{nr}/submit", headers=env["claude"], json={"branch_name": branch, "commit_sha": SHA,
                                                                      "summary": "x"})
    g.conflict = {"pr_files": ["brain.md", "reviews/a.md"], "base_files": ["brain.md", "reviews/b.md"]}
    r = c.post(f"/api/v1/tasks/{nr}/review", headers=env["admin"], json={"decision": "approve"})
    err = r.json()["error"]
    assert r.status_code == 409 and err["code"] == "merge_conflict"
    assert "in: brain.md" in err["message"] and "reviews/a.md" not in err["message"] and "claude-code" in err["message"]
    assert c.get(f"/api/v1/tasks/{nr}", headers=env["admin"]).json()["status"] == "for_review"

    r = c.post(f"/api/v1/tasks/{nr}/review", headers=env["admin"],
               json={"decision": "reject_conflict", "comment": "Inhalt passt"})
    t = r.json()
    assert t["status"] == "changes_requested"
    body = t["comments"][-1]["body"]
    assert "Merge-Konflikt mit `main`" in body and "`brain.md`" in body and "Inhalt passt" in body
    # Non-admins may not do this.
    assert c.post(f"/api/v1/tasks/{nr}/review", headers=env["claude"],
                  json={"decision": "reject_conflict"}).status_code in (403, 409)



# --- brain-changes: project knowledge without merge conflicts -------------------------------------------

def _submit_with_brain(env, changes: str | None, nr=None):
    c, g = env["c"], env["gitea"]
    nr = nr or mktask(env, assigned_to="claude-code")["task_nr"]
    branch = claim(env).json()["branch_name"]
    g.push_branch(SERVICE_USER, "ket", branch)
    if changes is not None:
        g.put_branch_file(SERVICE_USER, "ket", branch, f"brain-changes/{nr}.md", changes)
    r = c.post(f"/api/v1/tasks/{nr}/submit", headers=env["claude"],
               json={"branch_name": branch, "commit_sha": SHA, "summary": "fertig"})
    assert r.status_code == 200, r.text
    return nr


def test_brain_changes_applied_in_approval_order(env):
    c, g = env["c"], env["gitea"]
    mkproject(env)
    repo = g.repo(SERVICE_USER, "ket")
    a = _submit_with_brain(env, "## add: Bekannte Stolperfallen\n- Aus Task A.\n")
    r = c.post(f"/api/v1/tasks/{a}/review", headers=env["admin"], json={"decision": "approve"})
    assert r.status_code == 200, r.text
    b = _submit_with_brain(env, "## add: Bekannte Stolperfallen\n- Aus Task B.\n\n"
                                "## change: Bekannte Stolperfallen\n### old\n- Aus Task A.\n### new\n- Aus Task A (präzisiert).\n")
    assert c.post(f"/api/v1/tasks/{b}/review", headers=env["admin"], json={"decision": "approve"}).status_code == 200
    brain = repo.files["brain.md"]
    assert "## Bekannte Stolperfallen\n\n- Aus Task A (präzisiert).\n- Aus Task B.\n\n## Glossar" in brain
    assert f"brain-changes/{a}.md" not in repo.files and f"brain-changes/{b}.md" not in repo.files
    comments = c.get(f"/api/v1/tasks/{b}", headers=env["admin"]).json()["comments"]
    assert "1 ergänzt, 1 geändert, 0 entfernt" in comments[-1]["body"]


def test_brain_changes_on_outdated_text_block_merge_and_return(env):
    c, g = env["c"], env["gitea"]
    mkproject(env)
    first = _submit_with_brain(env, "## add: Glossar\n- Lease: befristete Reservierung.\n")
    stale = "## change: Glossar\n### old\n- Lease: befristete Reservierung.\n### new\n- Lease: Reservierung auf Zeit.\n"
    c.post(f"/api/v1/tasks/{first}/review", headers=env["admin"], json={"decision": "approve"})
    # Another task changed the entry in parallel and was approved first.
    other = _submit_with_brain(env, "## change: Glossar\n### old\n- Lease: befristete Reservierung.\n"
                                    "### new\n- Lease: zeitlich begrenzte Reservierung.\n")
    c.post(f"/api/v1/tasks/{other}/review", headers=env["admin"], json={"decision": "approve"})
    nr = _submit_with_brain(env, stale)
    r = c.post(f"/api/v1/tasks/{nr}/review", headers=env["admin"], json={"decision": "approve"})
    err = r.json()["error"]
    assert r.status_code == 409 and err["code"] == "brain_conflict" and "steht so nicht (mehr)" in err["message"]
    assert g.repo(SERVICE_USER, "ket").pulls[3]["merged"] is False
    r = c.post(f"/api/v1/tasks/{nr}/review", headers=env["admin"], json={"decision": "reject_conflict"})
    t = r.json()
    assert t["status"] == "changes_requested"
    body = t["comments"][-1]["body"]
    assert f"brain-changes/{nr}.md" in body and "### old" in body and "Merge-Konflikt" not in body


def test_submit_notes_direct_brain_edit_and_invalid_format(env):
    c, g = env["c"], env["gitea"]
    mkproject(env)
    g.pr_files = ["brain.md", "src/x.py"]
    nr = _submit_with_brain(env, "## update: Glossar\n- x\n")
    note = c.get(f"/api/v1/tasks/{nr}", headers=env["admin"]).json()["comments"][-1]["body"]
    assert "direkt geändert" in note and "ungültiges Format" in note
    r = c.post(f"/api/v1/tasks/{nr}/review", headers=env["admin"], json={"decision": "approve"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "brain_conflict"
    # No change file or direct brain.md edit: no note; approval as before.
    g.pr_files = ["src/y.py"]
    nr2 = _submit_with_brain(env, None)
    assert not any(cm["type"] == "system" for cm in c.get(f"/api/v1/tasks/{nr2}", headers=env["admin"]).json()["comments"])
    assert c.post(f"/api/v1/tasks/{nr2}/review", headers=env["admin"], json={"decision": "approve"}).status_code == 200


# --- changelog.d and "one review at a time per agent" -------------------------------------------

def test_changelog_fragments_applied_without_conflicts(env):
    c, g = env["c"], env["gitea"]
    mkproject(env)
    repo = g.repo(SERVICE_USER, "ket")
    nrs = []
    for fragment in ("- Feature A.\n", "- Feature B\n  with details.\n"):
        nr = mktask(env, assigned_to="claude-code")["task_nr"]
        branch = claim(env).json()["branch_name"]
        g.push_branch(SERVICE_USER, "ket", branch)
        g.put_branch_file(SERVICE_USER, "ket", branch, f"changelog.d/{nr}.md", fragment)
        g.put_branch_file(SERVICE_USER, "ket", branch, f"brain-changes/{nr}.md", f"## add: Glossar\n- Term {nr}.\n")
        c.post(f"/api/v1/tasks/{nr}/submit", headers=env["claude"],
               json={"branch_name": branch, "commit_sha": SHA, "summary": "x"})
        nrs.append(nr)
    for nr in nrs:
        assert c.post(f"/api/v1/tasks/{nr}/review", headers=env["admin"], json={"decision": "approve"}).status_code == 200
    assert repo.files["CHANGELOG.md"] == "# Changelog\n\n## Unreleased\n\n- Feature A.\n- Feature B\n  with details.\n"
    assert not any(p.startswith(("changelog.d/KET", "brain-changes/KET")) for p in repo.files)
    assert f"- Term {nrs[1]}." in repo.files["brain.md"]
    last = c.get(f"/api/v1/tasks/{nrs[1]}", headers=env["admin"]).json()["comments"][-1]["body"]
    assert "CHANGELOG.md" in last and "brain.md" in last  # one commit, one summary


def test_invalid_changelog_fragment_blocks_merge(env):
    c, g = env["c"], env["gitea"]
    mkproject(env)
    g.pr_files = ["CHANGELOG.md"]
    nr = mktask(env, assigned_to="claude-code")["task_nr"]
    branch = claim(env).json()["branch_name"]
    g.push_branch(SERVICE_USER, "ket", branch)
    g.put_branch_file(SERVICE_USER, "ket", branch, f"changelog.d/{nr}.md", "## Added\n- x\n")
    c.post(f"/api/v1/tasks/{nr}/submit", headers=env["claude"], json={"branch_name": branch, "commit_sha": SHA,
                                                                      "summary": "x"})
    note = c.get(f"/api/v1/tasks/{nr}", headers=env["admin"]).json()["comments"][-1]["body"]
    assert "CHANGELOG.md" in note and "changelog.d" in note
    r = c.post(f"/api/v1/tasks/{nr}/review", headers=env["admin"], json={"decision": "approve"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "changelog_invalid"
    t = c.post(f"/api/v1/tasks/{nr}/review", headers=env["admin"], json={"decision": "reject_conflict"}).json()
    assert t["status"] == "changes_requested" and "bullet points" in t["comments"][-1]["body"]


def test_serialize_reviews_blocks_new_claims_in_project(env):
    c, g = env["c"], env["gitea"]
    mkproject(env)
    mkproject(env, key="ZWEI")
    first = mktask(env, assigned_to="claude-code", prio=1)["task_nr"]
    second = mktask(env, assigned_to="claude-code", prio=2)["task_nr"]
    other = mktask(env, project="ZWEI", assigned_to="claude-code", prio=3)["task_nr"]
    branch = claim(env).json()["branch_name"]
    g.push_branch(SERVICE_USER, "ket", branch)
    c.post(f"/api/v1/tasks/{first}/submit", headers=env["claude"], json={"branch_name": branch, "commit_sha": SHA,
                                                                         "summary": "x"})
    # option off: the next KET task is claimable even though `first` waits for review
    assert c.patch("/api/v1/projects/KET", headers=env["admin"], json={"serialize_reviews": True}).json()[
        "serialize_reviews"] is True
    r = claim(env)
    assert r.status_code == 200 and r.json()["task_nr"] == other  # other project is unaffected
    c.post(f"/api/v1/tasks/{other}/release", headers=env["claude"], json={"reason": "x"})
    r = c.post("/api/v1/agents/me/claim", headers=env["claude"], json={"task_nr": second})
    assert r.status_code == 409 and r.json()["error"]["code"] == "awaiting_review" and first in r.json()["error"]["message"]
    # rework comes first: `first` back to changes_requested → claimed before `second` (same project, lower prio no.)
    c.post(f"/api/v1/tasks/{first}/review", headers=env["admin"], json={"decision": "reject", "comment": "nochmal"})
    assert claim(env).json()["task_nr"] == first


def test_rework_before_new_work_at_same_prio(env):
    c, g = env["c"], env["gitea"]
    mkproject(env)
    old = mktask(env, assigned_to="claude-code", prio=2)["task_nr"]
    branch = claim(env).json()["branch_name"]
    g.push_branch(SERVICE_USER, "ket", branch)
    c.post(f"/api/v1/tasks/{old}/submit", headers=env["claude"], json={"branch_name": branch, "commit_sha": SHA,
                                                                       "summary": "x"})
    fresh = mktask(env, assigned_to="claude-code", prio=2)["task_nr"]  # new work, same priority
    c.post(f"/api/v1/tasks/{old}/review", headers=env["admin"], json={"decision": "reject", "comment": "nochmal"})
    assert claim(env).json()["task_nr"] == old  # rework first
    assert fresh != old
