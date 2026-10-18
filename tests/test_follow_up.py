"""Parallel-task limit on claim, open points on submit and follow-up tasks (API, UI, `tbr`)."""
import pytest

from tests.gitea_fake import SERVICE_USER
from tests.test_ui_cli import post, tm_as, ui  # noqa: F401 (Fixtures)
from tests.test_workflow import SHA, claim, env, gitea, mkproject, mktask  # noqa: F401 (Fixtures)

pytestmark = pytest.mark.db


def submit(env, nr, **extra):  # noqa: F811
    branch = env["c"].get(f"/api/v1/tasks/{nr}", headers=env["admin"]).json()["branch_name"]
    env["gitea"].push_branch(SERVICE_USER, "ket", branch)
    return env["c"].post(f"/api/v1/tasks/{nr}/submit", headers=env["claude"],
                         json={"branch_name": branch, "commit_sha": SHA, "summary": "fertig", **extra})


# --- Parallel-task limit ----------------------------------------------------------------------

def test_new_agent_gets_default_limit_two(env):  # noqa: F811
    r = env["c"].post("/api/v1/admin/actors", headers=env["admin"],
                      json={"name": "newbot", "display_name": "New bot", "actor_type": "agent", "agent_kind": "other"})
    assert r.status_code == 201, r.text
    assert r.json()["actor"]["max_parallel_tasks"] == 2


def test_limit_visible_and_editable_in_ui(env, ui):  # noqa: F811
    post(ui, "/profile/display", show_all_agents="1")  # Seeded agents have no owner; an admin sees them only so.
    mkproject(env)
    mktask(env, assigned_to="claude-code")
    claim(env)
    assert "1 / 1" in ui.get("/admin/users").text  # in progress / limit
    form = ui.get("/admin/users/claude-code").text
    assert 'name="max_parallel_tasks"' in form
    page = ui.get("/admin/users/new").text
    assert 'name="max_parallel_tasks" min="1" max="50" value="2"' in page  # Default for new actors


def test_claim_at_limit_explains_and_frees_after_release(env):  # noqa: F811
    mkproject(env)
    env["c"].patch("/api/v1/admin/actors/claude-code", headers=env["admin"], json={"max_parallel_tasks": 2})
    a, b, c = (mktask(env, assigned_to="claude-code")["task_nr"] for _i in range(3))
    assert claim(env).json()["task_nr"] == a
    assert claim(env).json()["task_nr"] == b
    assert claim(env).status_code == 204  # Without a number: no task, as before.
    r = claim(env, task_nr=c)
    assert r.status_code == 409 and r.json()["error"]["code"] == "parallel_limit"
    assert f"{a}, {b}" in r.json()["error"]["message"] and "2" in r.json()["error"]["message"]
    # Explicitly claiming a task that is already running still says so, not "limit".
    assert claim(env, task_nr=a).json()["error"]["code"] == "invalid_transition"
    env["c"].post(f"/api/v1/tasks/{a}/release", headers=env["claude"], json={"reason": "später"})
    assert claim(env, task_nr=c).json()["task_nr"] == c
    assert submit(env, b).json()["status"] == "for_review"  # Submitting frees a slot as well.
    assert claim(env).json()["task_nr"] == a


def test_tm_claim_and_mcp_explain_limit(env, tm_as, tmp_path, monkeypatch):  # noqa: F811
    mkproject(env)
    first = mktask(env, assigned_to="claude-code")["task_nr"]
    mktask(env, assigned_to="claude-code")
    assert claim(env).json()["task_nr"] == first  # Limit 1 for the test agent.
    r = tm_as(env["claude"], "claim")
    assert r.exit_code == 3 and "Limit reached" in r.output and first in r.output

    from cli.client import Client, Config
    from cli.mcp_server import TaskBrigadeMCP

    token = env["claude"]["Authorization"].split()[1]
    monkeypatch.setattr("cli.mcp_server.prepare", lambda *a, **k: None)  # Claim returned 204 (repo setup skipped).
    mcp = TaskBrigadeMCP(Client(Config("http://testserver", token)), tmp_path)
    assert "Limit reached" in mcp.start_task()


# --- Open points and follow-up tasks -----------------------------------------------------------

def test_submit_stores_open_points(env):  # noqa: F811
    mkproject(env)
    nr = mktask(env, assigned_to="claude-code")["task_nr"]
    claim(env)
    r = submit(env, nr, open_points=["- Rate limit fehlt", "  ", "2. Doku ergänzen"])
    assert r.status_code == 200, r.text
    assert r.json()["open_points"] == ["Rate limit fehlt", "Doku ergänzen"]
    pr = env["gitea"].repo(SERVICE_USER, "ket").pulls[1]
    assert "- Rate limit fehlt" in pr["body"] and "- Doku ergänzen" in pr["body"]


def test_submit_rejects_invalid_open_points(env):  # noqa: F811
    mkproject(env)
    nr = mktask(env, assigned_to="claude-code")["task_nr"]
    claim(env)
    assert submit(env, nr, open_points="kein Array").status_code == 422
    assert submit(env, nr, open_points=["x" * 1001]).json()["error"]["code"] == "invalid_open_points"


def test_follow_up_via_api_links_both_ways(env):  # noqa: F811
    mkproject(env)
    mkproject(env, key="DEMO")
    origin = mktask(env)["task_nr"]
    r = env["c"].post("/api/v1/tasks", headers=env["admin"],
                      json={"project": "KET", "title": "Nacharbeit", "status": "draft", "follow_up_of": origin})
    assert r.status_code == 201 and r.json()["follow_up_of"] == origin
    follow = r.json()["task_nr"]
    got = env["c"].get(f"/api/v1/tasks/{origin}", headers=env["admin"]).json()
    assert [f["task_nr"] for f in got["follow_ups"]] == [follow]
    r = env["c"].post("/api/v1/tasks", headers=env["admin"],
                      json={"project": "DEMO", "title": "falsch", "status": "draft", "follow_up_of": origin})
    assert r.status_code == 422


def test_follow_up_in_ui_takes_selected_open_points(env, ui):  # noqa: F811
    mkproject(env)
    nr = mktask(env, assigned_to="claude-code", title="Login bauen")["task_nr"]
    claim(env)
    submit(env, nr, open_points=["Rate limit fehlt", "Doku ergänzen", "Logging prüfen"])

    detail = ui.get(f"/tasks/{nr}").text
    assert "Offene Punkte" in detail and "Rate limit fehlt" in detail and f"/tasks/{nr}/follow-up" in detail
    form = ui.get(f"/tasks/{nr}/follow-up").text
    assert f'value="Follow-up zu {nr}: Login bauen"' in form
    assert form.count('name="points"') == 3 and form.count("checked") == 3  # All preselected.

    r = post(ui, f"/tasks/{nr}/follow-up", title=f"Follow-up zu {nr}: Login bauen", description="Restarbeiten",
             points=["0", "2"], prio="2", assigned_to="")
    assert r.status_code == 200 and "Follow-up-Task angelegt" in r.text
    new_nr = str(r.url).rsplit("/", 1)[1].split("?")[0]
    t = env["c"].get(f"/api/v1/tasks/{new_nr}", headers=env["admin"]).json()
    assert t["status"] == "draft" and t["assigned_to"] is None and t["follow_up_of"] == nr and t["prio"] == 2
    assert t["description"].startswith("Restarbeiten") and f"Offene Punkte aus {nr}:" in t["description"]
    assert "- Rate limit fehlt" in t["description"] and "- Logging prüfen" in t["description"]
    assert "Doku ergänzen" not in t["description"]
    assert f'href="/tasks/{new_nr}"' in ui.get(f"/tasks/{nr}").text  # Origin lists the follow-up.
    assert f'href="/tasks/{nr}"' in ui.get(f"/tasks/{new_nr}").text

    r = post(ui, f"/tasks/{nr}/follow-up", title="Checkliste", points=["1"], points_into="acceptance_criteria",
             assigned_to="claude-code", status="draft")
    t = env["c"].get(f"/api/v1/tasks/{str(r.url).rsplit('/', 1)[1].split('?')[0]}", headers=env["admin"]).json()
    assert t["acceptance_criteria"] == "- [ ] Doku ergänzen" and t["assigned_to"] == "claude-code"


def test_follow_up_without_open_points(env, ui):  # noqa: F811
    mkproject(env)
    nr = mktask(env)["task_nr"]
    form = ui.get(f"/tasks/{nr}/follow-up").text
    assert 'name="points"' not in form and "keine offenen Punkte" in form
    r = post(ui, f"/tasks/{nr}/follow-up", title="", description="x")
    assert r.status_code == 422


def test_tm_submit_open_points_and_show(env, tm_as):  # noqa: F811
    mkproject(env)
    nr = mktask(env, assigned_to="claude-code")["task_nr"]
    branch = claim(env).json()["branch_name"]
    env["gitea"].push_branch(SERVICE_USER, "ket", branch)
    r = tm_as(env["claude"], "submit", nr, "--branch", branch, "--sha", SHA, "--summary", "fertig",
              "--open-point", "Rate limit fehlt", "--open-point", "Doku ergänzen")
    assert r.exit_code == 0, r.output
    shown = tm_as(env["claude"], "show", nr).stdout
    assert "## Open points" in shown and "- Doku ergänzen" in shown
    follow = env["c"].post("/api/v1/tasks", headers=env["admin"],
                           json={"project": "KET", "title": "Nacharbeit", "status": "draft", "follow_up_of": nr})
    assert f"follows up on {nr}" in tm_as(env["admin"], "show", follow.json()["task_nr"]).stdout
