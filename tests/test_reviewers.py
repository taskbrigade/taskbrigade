"""Reviewer agents: role reviewer, reviewer per task, project default, review jobs with lease, decisions by agents
(test DB + fake Gitea)."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db import db
from app.models import Actor, Task
from app.services import state_machine
from app.services import tasks as task_svc
from app.services.actors import utcnow
from tests.conftest import bearer, make_token
from tests.gitea_fake import SERVICE_USER
from tests.test_ui_cli import post, tm_as, ui  # noqa: F401 (Fixtures)
from tests.test_workflow import SHA, claim, env, events, gitea, mkproject, mktask  # noqa: F401

pytestmark = pytest.mark.db


def _role(name: str, role: str, runtime: str | None = None) -> None:
    with db.session() as s:
        a = s.scalar(select(Actor).where(Actor.name == name))
        a.role = role
        if runtime:
            a.runtime = runtime
        s.commit()


@pytest.fixture
def renv(env):
    """hermes is a reviewer agent (local); claude-code works on the tasks."""
    _role("hermes", "reviewer")
    mkproject(env)
    return env


def _submitted(e, **kw) -> str:
    nr = mktask(e, assigned_to="claude-code", **kw)["task_nr"]
    branch = claim(e).json()["branch_name"]
    e["gitea"].push_branch(SERVICE_USER, "ket", branch)
    r = e["c"].post(f"/api/v1/tasks/{nr}/submit", headers=e["claude"],
                    json={"branch_name": branch, "commit_sha": SHA, "summary": "Login fertig"})
    assert r.status_code == 200, r.text
    return nr


def _review_claim(e, who="hermes", task_nr=None):
    body = {"reviews": True, **({"task_nr": task_nr} if task_nr else {})}
    return e["c"].post("/api/v1/agents/me/claim", headers=e[who], json=body)


def _decide(e, nr, decision, who="hermes", comment=None):
    return e["c"].post(f"/api/v1/tasks/{nr}/review", headers=e[who],
                       json={"decision": decision, **({"comment": comment} if comment else {})})


# --- Role and reviewer field ---------------------------------------------------------------------

def test_reviewer_role_only_for_agents(env):
    c = env["c"]
    r = c.post("/api/v1/admin/actors", headers=env["admin"],
               json={"name": "alex", "display_name": "Alex", "actor_type": "human", "role": "reviewer"})
    assert r.status_code == 422 and "reviewer" in r.json()["error"]["message"]
    r = c.patch("/api/v1/admin/actors/hermes", headers=env["admin"], json={"role": "reviewer"})
    assert r.status_code == 200 and r.json()["role"] == "reviewer"


def test_reviewer_must_be_human_or_reviewer_agent(renv):
    r = renv["c"].post("/api/v1/tasks", headers=renv["admin"], json={
        "project": "KET", "title": "x", "description": "d", "acceptance_criteria": "a", "reviewer": "opencode"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_reviewer"
    t = mktask(renv, reviewer="hermes")
    assert t["reviewer"] == "hermes" and t["reviewer_kind"] == "agent" and t["reviewer_effective"]
    assert ("reviewer", None, "hermes") in events(renv, t["task_nr"])
    t = mktask(renv, reviewer="admin")  # A human reviewer.
    assert t["reviewer"] == "admin" and t["reviewer_kind"] == "human"


def test_no_self_review(renv):
    c = renv["c"]
    r = c.post("/api/v1/tasks", headers=renv["admin"], json={
        "project": "KET", "title": "x", "description": "d", "acceptance_criteria": "a", "assigned_to": "hermes",
        "reviewer": "hermes"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "self_review"
    nr = mktask(renv, assigned_to="claude-code", reviewer="hermes")["task_nr"]
    r = c.patch(f"/api/v1/tasks/{nr}", headers=renv["admin"], json={"assigned_to": "hermes"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "self_review"
    r = c.patch(f"/api/v1/tasks/{nr}", headers=renv["admin"], json={"reviewer": "claude-code"})
    assert r.status_code == 422  # claude-code is a worker and the assignee.
    r = c.patch(f"/api/v1/tasks/{nr}", headers=renv["admin"], json={"reviewer": None})
    assert r.status_code == 200 and r.json()["reviewer"] is None


def test_local_only_needs_local_reviewer(env):
    _role("opencode", "reviewer", runtime="cloud")
    mkproject(env, privacy="local_only")
    r = env["c"].post("/api/v1/tasks", headers=env["admin"], json={
        "project": "KET", "title": "x", "description": "d", "acceptance_criteria": "a", "reviewer": "opencode"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "privacy_violation"


def test_project_default_reviewer(renv):
    c = renv["c"]
    r = c.patch("/api/v1/projects/KET", headers=renv["admin"], json={"default_reviewer": "codex"})
    assert r.status_code == 422  # A worker agent cannot review.
    r = c.patch("/api/v1/projects/KET", headers=renv["admin"], json={"default_reviewer": "hermes"})
    assert r.status_code == 200 and r.json()["default_reviewer"] == "hermes"
    assert mktask(renv)["reviewer"] == "hermes"
    assert mktask(renv, reviewer="none")["reviewer"] is None  # Explicitly nobody.
    t = mktask(renv, assigned_to="hermes")  # The default would review its own task: not applied, noted.
    assert t["reviewer"] is None
    assert any(e["type"] == "reviewer" and "Projekt-Standard nicht übernommen" in (e["note"] or "") for e in t["events"])
    r = c.patch("/api/v1/projects/KET", headers=renv["admin"], json={"default_reviewer": "none"})
    assert r.json()["default_reviewer"] is None and mktask(renv)["reviewer"] is None


# --- Review jobs and decisions -------------------------------------------------------------------

def test_reviewer_agent_approves(renv):
    c, g = renv["c"], renv["gitea"]
    nr = _submitted(renv, reviewer="hermes")
    t = c.get(f"/api/v1/tasks/{nr}", headers=renv["admin"]).json()
    assert any("Reviewer-Agenten hermes" in x["body"] for x in t["comments"] if x["type"] == "system")
    # Without the reviews flag (old runners) no review job is handed out.
    assert c.post("/api/v1/agents/me/claim", headers=renv["hermes"]).status_code == 204
    jobs = c.get("/api/v1/agents/me/reviews", headers=renv["hermes"]).json()["items"]
    assert [(j["task_nr"], j["job"], j["review_claimed"]) for j in jobs] == [(nr, "review", False)]
    r = _review_claim(renv)
    assert r.status_code == 200 and r.json()["job"] == "review" and r.json()["task_nr"] == nr
    assert r.json()["review_lease_expires_at"] and r.json()["status"] == "for_review"
    assert _review_claim(renv).status_code == 204  # Nothing else waiting.
    # Only the entered reviewer: another reviewer agent and the assignee are refused.
    _role("opencode", "reviewer")
    renv["opencode"] = bearer(make_token("opencode"))
    assert _decide(renv, nr, "approve", who="opencode").status_code == 403
    assert _decide(renv, nr, "approve", who="claude").status_code == 403
    r = _decide(renv, nr, "approve", comment="Sieht gut aus")
    assert r.status_code == 200, r.text
    t = r.json()
    assert t["status"] == "done" and t["review_claimed_at"] is None
    assert g.repo(SERVICE_USER, "ket").pulls[1]["merged"]
    assert any(x["body"] == "Abgenommen von hermes (Reviewer-Agent)" and x["type"] == "system" for x in t["comments"])
    review = [x for x in t["comments"] if x["type"] == "review"][-1]
    assert review["author"] == "hermes" and review["by_agent"]
    assert ("review_claimed", None, "hermes") in events(renv, nr)


def test_reviewer_agent_rejects_and_returns_conflict(renv):
    c = renv["c"]
    nr = _submitted(renv, reviewer="hermes")
    assert _review_claim(renv).json()["task_nr"] == nr
    assert _decide(renv, nr, "reject").status_code == 422  # Comment required.
    r = _decide(renv, nr, "reject", comment="Tests fehlen")
    assert r.json()["status"] == "changes_requested"
    assert any(x["body"] == "Zurückgewiesen von hermes (Reviewer-Agent)" for x in r.json()["comments"])
    branch = claim(renv).json()["branch_name"]
    c.post(f"/api/v1/tasks/{nr}/submit", headers=renv["claude"],
           json={"branch_name": branch, "commit_sha": SHA, "summary": "v2"})
    assert _review_claim(renv).json()["task_nr"] == nr  # Back in the reviewer's queue after resubmission.
    r = _decide(renv, nr, "reject_conflict")
    assert r.json()["status"] == "changes_requested"
    assert any(x["body"] == "Konflikt zurückgegeben von hermes (Reviewer-Agent)" for x in r.json()["comments"])


def test_humans_can_always_decide(renv):
    nr = _submitted(renv, reviewer="hermes")
    assert _review_claim(renv).json()["task_nr"] == nr
    r = _decide(renv, nr, "approve", who="admin")
    assert r.status_code == 200 and r.json()["status"] == "done" and r.json()["review_claimed_at"] is None
    assert not any("(Reviewer-Agent)" in x["body"] for x in r.json()["comments"])
    assert _decide(renv, nr, "approve").status_code == 409  # The agent comes too late.


def test_human_reviewer_may_decide(renv):
    c = renv["c"]
    r = c.post("/api/v1/admin/actors", headers=renv["admin"],
               json={"name": "alex", "display_name": "Alex", "actor_type": "human", "role": "member"})
    assert r.status_code == 201, r.text
    renv["alex"] = bearer(make_token("alex"))
    nr = _submitted(renv)
    assert _decide(renv, nr, "approve", who="alex").status_code == 403
    nr = _submitted(renv, reviewer="alex")
    assert _decide(renv, nr, "approve", who="alex").json()["status"] == "done"


def test_submit_drops_reviewer_that_cannot_review(renv):
    nr = _submitted(renv, reviewer="hermes")
    t = renv["c"].get(f"/api/v1/tasks/{nr}", headers=renv["admin"]).json()
    assert t["reviewer"] == "hermes"
    nr = mktask(renv, assigned_to="claude-code", reviewer="hermes")["task_nr"]
    with db.session() as s:  # Self-review that slipped through (e.g. an old row): dropped at submission.
        task = s.scalar(select(Task).where(Task.task_nr == nr))
        task.reviewer_id = task.assigned_to
        s.commit()
    with db.session() as s:
        task = s.scalar(select(Task).where(Task.task_nr == nr))
        task.status, task.branch_name = "in_progress", f"task/{nr}-x"
        s.commit()
    branch = f"task/{nr}-x"
    renv["gitea"].push_branch(SERVICE_USER, "ket", branch)
    r = renv["c"].post(f"/api/v1/tasks/{nr}/submit", headers=renv["claude"],
                       json={"branch_name": branch, "commit_sha": SHA, "summary": "x"})
    assert r.status_code == 200, r.text
    t = r.json()
    assert t["reviewer"] is None and any("Reviewer entfernt" in x["body"] and "kein Selbst-Review" in x["body"]
                                         for x in t["comments"])


def test_reviewer_role_lost_falls_back_to_humans(renv):
    nr = mktask(renv, assigned_to="claude-code", reviewer="hermes")["task_nr"]
    _role("hermes", "worker")
    branch = claim(renv).json()["branch_name"]
    renv["gitea"].push_branch(SERVICE_USER, "ket", branch)
    t = renv["c"].post(f"/api/v1/tasks/{nr}/submit", headers=renv["claude"],
                       json={"branch_name": branch, "commit_sha": SHA, "summary": "x"}).json()
    assert t["reviewer"] is None and any("kann diesen Task nicht reviewen" in x["body"] for x in t["comments"])
    with db.session() as s:
        assert [x["task_nr"] for x in task_svc.inbox(s)["for_review"]] == [nr]


def test_inbox_separates_agent_reviews(renv):
    agent_nr = _submitted(renv, reviewer="hermes")
    with db.session() as s:
        data = task_svc.inbox(s)
    assert [t["task_nr"] for t in data["agent_review"]] == [agent_nr] and data["for_review"] == []


def test_inbox_lists_agent_decisions_until_the_rework_starts(renv):
    by_agent, by_human = _submitted(renv, reviewer="hermes"), _submitted(renv, reviewer="hermes")
    assert _review_claim(renv, task_nr=by_agent).json()["task_nr"] == by_agent
    with db.session() as s:
        running = task_svc.inbox(s)["agent_review"]
    assert {t["task_nr"]: bool(t["review_claimed_at"]) for t in running} == {by_agent: True, by_human: False}
    assert all(t["submitted_at"] for t in running)
    assert _decide(renv, by_agent, "reject", comment="Tests fehlen").json()["status"] == "changes_requested"
    assert _decide(renv, by_human, "reject", who="admin", comment="Nein").json()["status"] == "changes_requested"
    with db.session() as s:
        data = task_svc.inbox(s)
    assert data["agent_review"] == []
    assert [(t["task_nr"], t["decision"], t["reviewer"]) for t in data["agent_decided"]] == [(by_agent, "reject", "hermes")]
    assert data["agent_decided"][0]["decided_at"] is not None
    with db.session() as s:
        assert task_svc.inbox(s, owner_id=-1)["agent_decided"] == []  # Owner filter applies.
    claim(renv)  # The assignee picks the rework up: nothing left to act on.
    with db.session() as s:
        assert task_svc.inbox(s)["agent_decided"] == []


def test_review_lease_expires_and_returns_to_queue(renv):
    c = renv["c"]
    nr = _submitted(renv, reviewer="hermes")
    _review_claim(renv)
    r = c.post(f"/api/v1/tasks/{nr}/heartbeat", headers=renv["hermes"])
    assert r.status_code == 200 and r.json()["lease_expires_at"]
    with db.session() as s:
        task = s.scalar(select(Task).where(Task.task_nr == nr))
        task.review_lease_expires_at = utcnow() - timedelta(minutes=1)
        s.commit()
    with db.session() as s:
        assert task_svc.sweep_expired_leases(s) == [nr]
        s.commit()
    assert ("review_lease_expired", None, None) in events(renv, nr)
    assert c.get(f"/api/v1/tasks/{nr}", headers=renv["admin"]).json()["review_claimed_at"] is None
    assert _review_claim(renv).json()["task_nr"] == nr


def test_review_release(renv):
    c = renv["c"]
    nr = _submitted(renv, reviewer="hermes")
    r = c.post(f"/api/v1/tasks/{nr}/review/release", headers=renv["hermes"], json={"reason": "x"})
    assert r.status_code == 409  # Not claimed yet.
    _review_claim(renv)
    r = c.post(f"/api/v1/tasks/{nr}/review/release", headers=renv["hermes"], json={"reason": "Runner-Start fehlgeschlagen"})
    assert r.status_code == 200 and r.json()["review_claimed_at"] is None
    assert "Runner-Start fehlgeschlagen" in r.json()["comments"][-1]["body"]
    assert c.post(f"/api/v1/tasks/{nr}/review/release", headers=renv["claude"], json={"reason": "x"}).status_code == 403


def test_review_jobs_count_towards_parallel_limit(renv):
    nr1 = _submitted(renv, reviewer="hermes")
    nr2 = _submitted(renv, reviewer="hermes")
    assert _review_claim(renv).json()["task_nr"] == nr1
    assert _review_claim(renv).status_code == 204  # max_parallel_tasks = 1
    r = _review_claim(renv, task_nr=nr2)
    assert r.status_code == 409 and r.json()["error"]["code"] == "parallel_limit"
    work = mktask(renv, assigned_to="hermes")["task_nr"]
    assert claim(renv, who="hermes").status_code == 204  # Work waits too.
    assert _review_claim(renv, task_nr=nr1).json()["task_nr"] == nr1  # Own job again: refresh only.
    _decide(renv, nr1, "approve")
    assert _review_claim(renv).json()["task_nr"] == nr2
    _decide(renv, nr2, "reject", comment="nein")
    assert _review_claim(renv).json()["task_nr"] == work  # Then work.


def test_serialize_reviews_one_review_job_per_project(renv):
    with db.session() as s:
        s.scalar(select(Actor).where(Actor.name == "hermes")).max_parallel_tasks = 3
        s.commit()
    nr1 = _submitted(renv, reviewer="hermes")
    nr2 = _submitted(renv, reviewer="hermes")
    renv["c"].patch("/api/v1/projects/KET", headers=renv["admin"], json={"serialize_reviews": True})
    assert _review_claim(renv).json()["task_nr"] == nr1
    assert _review_claim(renv).status_code == 204
    r = _review_claim(renv, task_nr=nr2)
    assert r.status_code == 409 and r.json()["error"]["code"] == "awaiting_review"


def test_review_model_resolved_for_reviewer(renv):
    with db.session() as s:
        hermes = s.scalar(select(Actor).where(Actor.name == "hermes"))
        hermes.model_tiers, hermes.models_allowed = '{"complex": "big-model"}', '["exact"]'
        s.commit()
    _submitted(renv, reviewer="hermes", model_tier="complex")
    assert _review_claim(renv).json()["model_effective"]["model"] == "big-model"


def test_reviewer_change_until_approval(renv):
    c = renv["c"]
    nr = _submitted(renv, reviewer="hermes")
    _review_claim(renv)
    r = c.patch(f"/api/v1/tasks/{nr}", headers=renv["admin"], json={"reviewer": "admin"})
    assert r.status_code == 200 and r.json()["reviewer"] == "admin" and r.json()["review_claimed_at"] is None
    _decide(renv, nr, "approve", who="admin")
    r = c.patch(f"/api/v1/tasks/{nr}", headers=renv["admin"], json={"reviewer": "hermes"})
    assert r.status_code == 409


def test_check_allowed_reviewer_rule():
    rule = state_machine.RULES["approve"]
    task = Task(status="for_review", assigned_to=2)
    agent = Actor(id=5, role="reviewer", actor_type="agent")
    with pytest.raises(state_machine.Forbidden):
        state_machine.check_allowed(rule, task, agent, owner_id=1)
    state_machine.check_allowed(rule, task, agent, owner_id=1, is_reviewer=True)
    state_machine.check_allowed(rule, task, Actor(id=1, role="member", actor_type="human"), owner_id=1)


# --- CLI ------------------------------------------------------------------------------------------------------------

def test_cli_reviewer_options_and_review_command(renv, tm_as):  # noqa: F811
    r = tm_as(renv["admin"], "create", "--project", "KET", "--title", "CLI", "--description", "d", "--acceptance",
              "a", "--assign", "claude-code", "--reviewer", "hermes")
    assert r.exit_code == 0, r.output
    nr = r.stdout.strip()
    r = tm_as(renv["admin"], "update", nr, "--reviewer", "none")
    assert r.exit_code == 0 and "reviewer=–" in r.output
    r = tm_as(renv["admin"], "update", nr, "--reviewer", "hermes")
    assert r.exit_code == 0 and "reviewer=hermes" in r.output
    branch = claim(renv).json()["branch_name"]
    renv["gitea"].push_branch(SERVICE_USER, "ket", branch)
    renv["c"].post(f"/api/v1/tasks/{nr}/submit", headers=renv["claude"],
                   json={"branch_name": branch, "commit_sha": SHA, "summary": "fertig"})
    r = tm_as(renv["hermes"], "list")
    assert r.exit_code == 0 and f"{nr}" in r.stdout and "review" in r.stdout and "1 review job(s)" in r.output
    r = tm_as(renv["hermes"], "claim")
    assert r.exit_code == 0 and f"Review job {nr} (claimed for you)" in r.stdout
    assert "review (claimed)" in tm_as(renv["hermes"], "list").stdout
    r = tm_as(renv["hermes"], "review", nr, "reject")
    assert r.exit_code == 2 and "needs a comment" in r.output
    r = tm_as(renv["hermes"], "review", nr, "release")
    assert r.exit_code == 2
    r = tm_as(renv["hermes"], "review", nr, "release", "--reason", "später")
    assert r.exit_code == 0 and "review job given back" in r.stdout
    r = tm_as(renv["codex"], "review", nr, "approve")
    assert r.exit_code == 1 and "403" in r.output
    r = tm_as(renv["hermes"], "review", nr, "approve", "--comment", "passt")
    assert r.exit_code == 0 and f"{nr}: done" in r.stdout
    r = tm_as(renv["admin"], "list", "--assigned-to", "all", "--reviewer", "hermes", "--status", "done")
    assert nr in r.stdout


# --- Web UI ---------------------------------------------------------------------------------------------------------

def test_ui_reviewer_fields_inbox_and_detail(renv, ui):  # noqa: F811
    post(ui, "/profile/display", show_all_agents="1")  # Seeded agents have no owner; an admin sees them only so.
    page = ui.get("/tasks/new?project=KET").text
    assert 'name="reviewer"' in page and "hermes (Reviewer-Agent)" in page and "– Projekt-Standard –" in page
    assert '<option value="codex"' not in page  # Workers are no reviewers.
    r = post(ui, "/projects/KET/edit", name="Projekt KET", status="active", privacy="cloud_ok",
             default_reviewer="hermes")
    assert r.status_code == 200 and "Standard-Reviewer" in r.text
    r = post(ui, "/tasks/new", project="KET", title="UI-Task", description="d", acceptance_criteria="a",
             assigned_to="claude-code", status="draft")
    assert r.status_code == 200, r.text
    nr = r.url.path.rsplit("/", 1)[-1]
    assert renv["c"].get(f"/api/v1/tasks/{nr}", headers=renv["admin"]).json()["reviewer"] == "hermes"
    row = renv["c"].get(f"/api/v1/tasks/{nr}", headers=renv["admin"]).json()["row_version"]
    r = post(ui, f"/tasks/{nr}/edit", row_version=row, title="UI-Task", prio=3, assigned_to="claude-code",
             reviewer="none")
    assert r.status_code == 200 and renv["c"].get(f"/api/v1/tasks/{nr}", headers=renv["admin"]).json()["reviewer"] is None
    row = renv["c"].get(f"/api/v1/tasks/{nr}", headers=renv["admin"]).json()["row_version"]
    r = post(ui, f"/tasks/{nr}/edit", row_version=row, title="UI-Task", prio=3, assigned_to="claude-code",
             reviewer="claude-code")
    assert "kann den eigenen Task nicht reviewen" in r.text or "nicht reviewen" in r.text
    agent_nr = _submitted(renv, reviewer="hermes")
    inbox = ui.get("/").text
    assert "Reviews durch Agenten" in inbox and agent_nr in inbox.split("Reviews durch Agenten")[1]
    detail = ui.get(f"/tasks/{agent_nr}").text
    assert "Wartet auf den Reviewer-Agenten hermes." in detail and "Du kannst trotzdem selbst entscheiden" in detail
    assert 'value="approve"' in detail  # The admin can still decide.
    _review_claim(renv)
    _decide(renv, agent_nr, "reject", comment="Tests fehlen")
    detail = ui.get(f"/tasks/{agent_nr}").text
    assert "Zurückgewiesen von hermes (Reviewer-Agent)" in detail and "hermes</strong> (Reviewer-Agent)" in detail


def test_ui_inbox_shows_agent_reviews_in_own_section(renv, ui):  # noqa: F811
    nr = _submitted(renv, reviewer="hermes")
    page = ui.get("/").text
    cards = page.split('class="inbox-cards"')[1].split('class="inbox-agent-reviews"')[0]
    assert f'id="ar-{nr}"' not in cards and "Alles erledigt" in cards  # Not under "Needs your action".
    section = page.split('class="inbox-agent-reviews"')[1].split('class="inbox-notices"')[0]
    assert "Reviews durch Agenten" in section and '<span class="count-pill">1</span>' in section
    row = section.split(f'id="ar-{nr}"')[1].split("</li>")[0]
    assert "Wartet auf Reviewer-Agenten" in row and "hermes" in row and "Wartet seit" in row
    assert 'class="action-card' not in row and 'class="agent' not in row.replace("agent-review", "")  # No stray dot.
    assert f'hx-post="/inbox/{nr}/approve"' in row and f'hx-post="/inbox/{nr}/reject"' in row  # Admin overrules.
    assert "ui-btn-secondary ui-btn-xs" in row and 'class="secondary"' in row  # … as small secondary buttons.
    assert 'class="inbox-divider"' in page
    # Counts: subtitle, "Needs your action" pill and tab badge only count items that need the user.
    assert "0 Einträge brauchen dich" in page
    assert 'Aktion erforderlich <span class="count-pill">0</span>' in page
    assert '<span class="nav-badge"' not in page
    assert "Reviews 1" in page  # The segmented filter counts both.
    assert "Ein Reviewer-Agent entscheidet diese Reviews" in section  # Short explanation under the heading.
    # The notices panel has no agent-review table or details any more, only a one-line pointer to the section.
    notices = page.split('class="inbox-notices"')[1]
    assert 'class="notice agent-review"' not in notices and nr not in notices
    assert "Diese Reviews entscheidet" not in notices and "<table" not in notices
    assert '<a href="#agent-reviews-title">1 Review durch Agenten ↓</a>' in notices
    _review_claim(renv)
    page = ui.get("/?tab=questions").text
    assert f'id="ar-{nr}"' not in page and 'class="inbox-agent-reviews"' not in page
    assert 'href="#agent-reviews-title"' not in page  # No pointer to a hidden section.
    row = ui.get("/?tab=reviews").text.split(f'id="ar-{nr}"')[1].split("</li>")[0]
    assert "Review läuft" in row and "Review läuft seit" in row
    _decide(renv, nr, "reject", comment="Tests fehlen")
    page = ui.get("/?tab=reviews").text
    row = page.split(f'id="ar-{nr}"')[1].split("</li>")[0]
    assert "Änderungen angefordert" in row and "hermes" in row and f'href="/tasks/{nr}"' in row
    assert "/approve" not in row  # Decided: the state machine only lets the assignee continue.
    assert "Reviews 1" in page and '<span class="nav-badge"' not in page
    r = post(ui, f"/inbox/{nr}/approve")
    assert r.status_code == 422  # The endpoint keeps refusing it (status changes_requested).


def test_ui_inbox_hides_agent_section_without_agent_reviews(renv, ui):  # noqa: F811
    nr = _submitted(renv)
    page = ui.get("/").text
    assert 'class="inbox-agent-reviews"' not in page and 'class="inbox-divider"' not in page
    assert 'href="#agent-reviews-title"' not in page
    assert f'id="r-{nr}"' in page.split('class="inbox-cards"')[1]
    assert "1 Eintrag braucht dich" in page and '<span class="nav-badge" aria-label="Aktion erforderlich">1</span>' in page


def test_sprint_plan_batch_and_duplicate_keep_reviewer(renv):
    from app.models import Project, Sprint
    from app.services import sprints

    with db.session() as s:
        admin = s.scalar(select(Actor).where(Actor.name == "admin"))
        project = s.scalar(select(Project).where(Project.project_key == "KET"))
        result = sprints.save_plan(s, admin, project, None, name="S1", entries=[
            {"title": "Mit Reviewer", "reviewer": "hermes"}, {"title": "Ohne", "reviewer": "none"},
            {"title": "Standard"}])
        s.commit()
        first, second, third = result["created"]
        sprint = s.get(Sprint, result["sprint"].id)
        names = {t.task_nr: t.reviewer_id for t in sprints.ordered_tasks(s, sprint)}
        hermes = s.scalar(select(Actor).where(Actor.name == "hermes"))
        assert names == {first: hermes.id, second: None, third: None}
        # Existing draft: empty keeps the reviewer, `none` removes it.
        task = s.scalar(select(Task).where(Task.task_nr == first))
        sprints.save_plan(s, admin, project, sprint, name="S1", entries=[
            {"task_nr": first, "row_version": task.row_version, "title": "Mit Reviewer", "reviewer": "", "prio": 3},
            {"task_nr": second, "title": "Ohne", "prio": 3}, {"task_nr": third, "title": "Standard", "prio": 3}])
        assert s.scalar(select(Task).where(Task.task_nr == first)).reviewer_id == hermes.id
        task = s.scalar(select(Task).where(Task.task_nr == first))
        sprints.save_plan(s, admin, project, sprint, name="S1", entries=[
            {"task_nr": first, "row_version": task.row_version, "title": "Mit Reviewer", "reviewer": "none", "prio": 3},
            {"task_nr": second, "title": "Ohne", "prio": 3}, {"task_nr": third, "title": "Standard", "prio": 3}])
        assert s.scalar(select(Task).where(Task.task_nr == first)).reviewer_id is None
        created = sprints.batch_create(s, admin, sprint, [{"title": "B", "prio": 3, "reviewer": "hermes"}])
        assert created[0].reviewer_id == hermes.id
        s.commit()
    nr = mktask(renv, reviewer="hermes")["task_nr"]
    dup = renv["c"].post(f"/api/v1/tasks/{nr}/duplicate", headers=renv["admin"], json={}).json()
    assert dup["reviewer"] == "hermes"
    dup = renv["c"].post(f"/api/v1/tasks/{nr}/duplicate", headers=renv["admin"], json={"assign": "hermes"}).json()
    assert dup["reviewer"] is None and dup["assigned_to"] == "hermes"  # Would review its own task.
