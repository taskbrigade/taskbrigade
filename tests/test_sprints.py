"""Sprints: data model, rules and API (test DB + fake Gitea)."""
import pytest
from sqlalchemy import text

from app.db import db
from tests.conftest import bearer, make_token
from tests.test_workflow import claim, env, events, gitea, mkproject, mktask  # noqa: F401

pytestmark = pytest.mark.db


@pytest.fixture
def senv(env):  # noqa: F811
    """Project KET (owner admin) plus a human owner `alex` of project OWN and an orchestrator."""
    c = env["c"]
    mkproject(env)
    r = c.post("/api/v1/admin/actors", headers=env["admin"], json={
        "name": "alex", "display_name": "Alex", "actor_type": "human", "role": "member"})
    assert r.status_code == 201, r.text
    mkproject(env, key="OWN", owner="alex")
    env["alex"] = bearer(make_token("alex"))
    env["orch"] = bearer(make_token("openclaw-agent", role="orchestrator"))
    return env


def mksprint(e, key="KET", who="admin", **kw):
    r = e["c"].post(f"/api/v1/projects/{key}/sprints", headers=e[who], json={"name": "Sprint 1", **kw})
    assert r.status_code == 201, r.text
    return r.json()


def sprint_url(key, sid, suffix=""):
    return f"/api/v1/projects/{key}/sprints/{sid}{suffix}"


def task(e, nr):
    return e["c"].get(f"/api/v1/tasks/{nr}", headers=e["admin"]).json()


# --- Data model / CRUD ----------------------------------------------------------------------

def test_tasks_without_sprint_unchanged(senv):
    t = mktask(senv)
    assert t["status"] == "defined" and t["sprint"] is None and t["sprint_name"] is None and "notice" not in t
    assert claim(senv, who="claude").status_code == 204  # Unassigned, as before.


def test_sprint_crud_and_listing(senv):
    c = senv["c"]
    s = mksprint(senv, goal="  Login fertig  ", start_date="2026-10-12", end_date="2026-10-23")
    assert s["status"] == "planned" and s["goal"] == "Login fertig" and s["created_by"] == "admin"
    assert s["project"] == "KET" and s["task_counts"] == {}
    r = c.post("/api/v1/projects/KET/sprints", headers=senv["admin"], json={"name": "Sprint 1"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "sprint_exists"
    r = c.post("/api/v1/projects/KET/sprints", headers=senv["admin"],
               json={"name": "Rückwärts", "start_date": "2026-10-12", "end_date": "2026-10-01"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_dates"
    r = c.patch(sprint_url("KET", s["id"]), headers=senv["admin"], json={"name": "Sprint 1a", "goal": None})
    assert r.status_code == 200 and r.json()["name"] == "Sprint 1a" and r.json()["goal"] is None
    assert c.patch(sprint_url("KET", s["id"]), headers=senv["admin"], json={"status": "closed"}).status_code == 422
    mksprint(senv, name="Sprint 2")
    listed = c.get("/api/v1/projects/KET/sprints", headers=senv["claude"]).json()
    assert [x["name"] for x in listed] == ["Sprint 1a", "Sprint 2"]
    assert c.get("/api/v1/projects/KET/sprints?status=closed", headers=senv["admin"]).json() == []
    assert c.get("/api/v1/projects/KET/sprints?status=bogus", headers=senv["admin"]).status_code == 422
    # A sprint is only reachable through its own project.
    assert c.get(sprint_url("OWN", s["id"]), headers=senv["admin"]).status_code == 404


def test_sprint_rights(senv):
    c = senv["c"]
    assert c.post("/api/v1/projects/KET/sprints", headers=senv["hermes"], json={"name": "W"}).status_code == 403
    assert c.post("/api/v1/projects/KET/sprints", headers=senv["alex"], json={"name": "A"}).status_code == 403
    own = mksprint(senv, key="OWN", who="alex")  # Owner of OWN.
    orch = mksprint(senv, key="OWN", who="orch", name="Orchestriert")
    assert c.patch(sprint_url("OWN", own["id"]), headers=senv["hermes"], json={"name": "x"}).status_code == 403
    assert c.post(sprint_url("OWN", orch["id"], "/tasks"), headers=senv["orch"],
                  json={"tasks": [{"title": "a"}]}).status_code == 201
    assert c.post(sprint_url("OWN", orch["id"], "/tasks"), headers=senv["hermes"],
                  json={"tasks": [{"title": "a"}]}).status_code == 403
    # Release and close: admins and the owner, not orchestrators or workers.
    assert c.post(sprint_url("OWN", own["id"], "/release"), headers=senv["orch"]).status_code == 403
    assert c.post(sprint_url("OWN", own["id"], "/release"), headers=senv["hermes"]).status_code == 403
    assert c.post(sprint_url("OWN", own["id"], "/release"), headers=senv["alex"]).status_code == 200
    assert c.post(sprint_url("OWN", own["id"], "/close"), headers=senv["orch"], json={}).status_code == 403
    assert c.post(sprint_url("OWN", own["id"], "/close"), headers=senv["alex"], json={}).status_code == 200
    ket = mksprint(senv)
    assert c.post(sprint_url("KET", ket["id"], "/release"), headers=senv["alex"]).status_code == 403
    assert c.post(sprint_url("KET", ket["id"], "/release"), headers=senv["admin"]).status_code == 200
    # Workers may not put their tasks into a sprint.
    r = c.post("/api/v1/tasks", headers=senv["hermes"], json={"project": "KET", "title": "w", "sprint": ket["id"]})
    assert r.status_code == 403


# --- Assignment rules -----------------------------------------------------------------------

def test_create_in_planned_sprint_is_draft_with_notice(senv):
    s = mksprint(senv)
    t = mktask(senv, sprint=s["id"], assigned_to="claude-code")
    assert t["status"] == "draft" and t["sprint"] == s["id"] and t["sprint_name"] == "Sprint 1"
    assert "Sprint 1" in t["notice"]
    assert claim(senv).status_code == 204  # Drafts are never claimed.
    t2 = mktask(senv, sprint=s["id"], status="draft", description="", acceptance_criteria="")
    assert t2["status"] == "draft" and "notice" not in t2
    # Without description a defined request in a planned sprint still works (it becomes a draft).
    r = senv["c"].post("/api/v1/tasks", headers=senv["admin"], json={"project": "KET", "title": "leer",
                                                                     "sprint": s["id"]})
    assert r.status_code == 201 and r.json()["status"] == "draft"


def test_create_in_released_sprint_keeps_status(senv):
    s = mksprint(senv)
    senv["c"].post(sprint_url("KET", s["id"], "/release"), headers=senv["admin"])
    t = mktask(senv, sprint=s["id"])
    assert t["status"] == "defined" and "notice" not in t


def test_assign_existing_tasks(senv):
    c = senv["c"]
    planned = mksprint(senv)
    defined = mktask(senv)["task_nr"]
    running = mktask(senv, assigned_to="claude-code", prio=1)["task_nr"]
    assert claim(senv).json()["task_nr"] == running
    for nr in (defined, running):
        r = c.patch(f"/api/v1/tasks/{nr}", headers=senv["admin"], json={"sprint": planned["id"]})
        assert r.status_code == 200, r.text
    assert task(senv, defined)["status"] == "draft"
    assert ("status_change", "defined", "draft") in events(senv, defined)
    assert ("sprint", None, "Sprint 1") in events(senv, defined)
    assert task(senv, running)["status"] == "in_progress"  # Other statuses are kept.
    # Detaching is always possible and keeps the status.
    r = c.patch(f"/api/v1/tasks/{defined}", headers=senv["admin"], json={"sprint": None})
    assert r.json()["sprint"] is None and r.json()["status"] == "draft"
    assert ("sprint", "Sprint 1", None) in events(senv, defined)


def test_assign_only_within_project_and_not_closed(senv):
    c = senv["c"]
    own = mksprint(senv, key="OWN")
    nr = mktask(senv)["task_nr"]
    r = c.patch(f"/api/v1/tasks/{nr}", headers=senv["admin"], json={"sprint": own["id"]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "sprint_other_project"
    r = c.post("/api/v1/tasks", headers=senv["admin"], json={"project": "KET", "title": "x", "sprint": own["id"]})
    assert r.status_code == 422
    assert c.patch(f"/api/v1/tasks/{nr}", headers=senv["admin"], json={"sprint": 999999}).status_code == 422
    s = mksprint(senv)
    c.post(sprint_url("KET", s["id"], "/release"), headers=senv["admin"])
    c.post(sprint_url("KET", s["id"], "/close"), headers=senv["admin"], json={})
    r = c.patch(f"/api/v1/tasks/{nr}", headers=senv["admin"], json={"sprint": s["id"]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "sprint_closed"
    # Assignment rights: like editing (admin, orchestrator, owner), not workers.
    assert c.patch(f"/api/v1/tasks/{nr}", headers=senv["hermes"], json={"sprint": None}).status_code == 403


def test_task_filter_by_sprint(senv):
    c = senv["c"]
    s1, s2 = mksprint(senv), mksprint(senv, name="Sprint 2")
    a = mktask(senv, sprint=s1["id"])["task_nr"]
    b = mktask(senv, sprint=s2["id"])["task_nr"]
    free = mktask(senv)["task_nr"]
    nrs = lambda q: {t["task_nr"] for t in c.get(f"/api/v1/tasks?{q}", headers=senv["admin"]).json()["items"]}  # noqa: E731
    assert nrs(f"sprint={s1['id']}") == {a}
    assert nrs(f"sprint={s1['id']},{s2['id']}") == {a, b}
    assert nrs("sprint=none") == {free}
    assert c.get("/api/v1/tasks?sprint=abc", headers=senv["admin"]).status_code == 422
    detail = c.get(sprint_url("KET", s1["id"]), headers=senv["admin"]).json()
    assert [t["task_nr"] for t in detail["tasks"]] == [a] and detail["task_counts"] == {"draft": 1}


# --- Release ------------------------------------------------------------------------------

def test_release_publishes_complete_drafts_only(senv):
    c = senv["c"]
    s = mksprint(senv)
    ok = mktask(senv, sprint=s["id"], assigned_to="claude-code")["task_nr"]
    no_ac = mktask(senv, sprint=s["id"], acceptance_criteria="")["task_nr"]
    empty = mktask(senv, sprint=s["id"], description=" ", acceptance_criteria=None)["task_nr"]
    r = c.post(sprint_url("KET", s["id"], "/release"), headers=senv["admin"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sprint"]["status"] == "released" and body["sprint"]["released_at"]
    assert [t["task_nr"] for t in body["released"]] == [ok]
    blocked = {t["task_nr"]: t["missing"] for t in body["blocked"]}
    assert blocked == {no_ac: ["acceptance_criteria"], empty: ["description", "acceptance_criteria"]}
    assert task(senv, ok)["status"] == "defined" and task(senv, no_ac)["status"] == "draft"
    ev = [(e["type"], e["from"], e["to"], e["note"]) for e in task(senv, ok)["events"]]
    assert ("status_change", "draft", "defined", "publish") in ev  # Same event as a single publication.
    assert claim(senv).json()["task_nr"] == ok
    # Release again: drafts completed since are published, the sprint stays released.
    c.patch(f"/api/v1/tasks/{no_ac}", headers=senv["admin"], json={"acceptance_criteria": "- jetzt"})
    body = c.post(sprint_url("KET", s["id"], "/release"), headers=senv["admin"]).json()
    assert [t["task_nr"] for t in body["released"]] == [no_ac] and [t["task_nr"] for t in body["blocked"]] == [empty]
    assert body["sprint"]["status"] == "released"


def test_release_warns_about_late_dependencies(senv):
    """`waiting`: released tasks whose dependency is not done and has no sprint, sits in a planned sprint or in a
    later one. Done dependencies, the same sprint and earlier released sprints give no warning."""
    from app.models import Sprint
    from app.services import sprints

    c = senv["c"]
    early = mksprint(senv, name="Früh", start_date="2026-10-01")
    later = mksprint(senv, name="Später", start_date="2026-10-20")
    planned_before = mksprint(senv, name="Geplant", start_date="2026-10-05")
    s = mksprint(senv, name="Jetzt", start_date="2026-10-10")
    in_early = mktask(senv, sprint=early["id"], title="früh")["task_nr"]
    in_later = mktask(senv, sprint=later["id"], title="später")["task_nr"]
    for x in (early, later):
        assert c.post(sprint_url("KET", x["id"], "/release"), headers=senv["admin"]).status_code == 200
    in_planned = mktask(senv, sprint=planned_before["id"], title="geplant")["task_nr"]
    loose = mktask(senv, title="lose")["task_nr"]
    done = mktask(senv, title="fertig")["task_nr"]
    same = mktask(senv, sprint=s["id"], title="gleich")["task_nr"]
    with db.session() as session:
        session.execute(text("UPDATE tasks SET status = 'done' WHERE task_nr = :nr"), {"nr": done})
        session.commit()
    nrs = {dep: mktask(senv, sprint=s["id"], title=f"wartet {dep}", depends_on=[dep])["task_nr"]
           for dep in (in_early, in_later, in_planned, loose, done, same)}
    blocked = mktask(senv, sprint=s["id"], acceptance_criteria="", depends_on=[loose])["task_nr"]

    with db.session() as session:
        preview = sprints.release_preview(session, session.get(Sprint, s["id"]))
        waiting = {t["task_nr"]: [d["task_nr"] for d in t["waits_for"]] for t in preview["waiting"]}
        assert waiting == {nrs[in_later]: [in_later], nrs[in_planned]: [in_planned], nrs[loose]: [loose]}
        assert blocked not in waiting  # Only tasks that would be released.
        by_dep = {t["waits_for"][0]["task_nr"]: t["waits_for"][0] for t in preview["waiting"]}
        assert by_dep[loose]["sprint"] is None
        assert (by_dep[in_planned]["sprint_name"], by_dep[in_planned]["sprint_status"]) == ("Geplant", "planned")
        assert by_dep[in_later]["sprint_status"] == "released"
        # Without start dates the creation order (id) decides.
        session.execute(text("UPDATE sprints SET start_date = NULL"))
        assert {t["task_nr"] for t in sprints.release_preview(session, session.get(Sprint, s["id"]))["waiting"]} \
            == {nrs[in_planned], nrs[loose]}
        session.rollback()
        drafts = text("SELECT COUNT(*) FROM tasks WHERE sprint_id = :sid AND status = 'draft'")
        assert session.scalar(drafts, {"sid": s["id"]}) == 8  # The preview changes nothing.

    # The release itself still publishes them and returns the same warning.
    body = c.post(sprint_url("KET", s["id"], "/release"), headers=senv["admin"]).json()
    assert {t["task_nr"] for t in body["released"]} == set(nrs.values()) | {same}
    assert {t["task_nr"] for t in body["waiting"]} == {nrs[in_later], nrs[in_planned], nrs[loose]}
    assert task(senv, nrs[loose])["status"] == "defined"


def test_release_of_closed_sprint_refused(senv):
    c = senv["c"]
    s = mksprint(senv)
    c.post(sprint_url("KET", s["id"], "/release"), headers=senv["admin"])
    c.post(sprint_url("KET", s["id"], "/close"), headers=senv["admin"], json={"detach": True})
    r = c.post(sprint_url("KET", s["id"], "/release"), headers=senv["admin"])
    assert r.status_code == 409 and r.json()["error"]["code"] == "sprint_closed"


# --- Close --------------------------------------------------------------------------------

def _sprint_with_mixed_tasks(e):
    c = e["c"]
    s = mksprint(e)
    running = mktask(e, sprint=s["id"], assigned_to="claude-code", prio=1)["task_nr"]
    waiting = mktask(e, sprint=s["id"])["task_nr"]
    draft = mktask(e, sprint=s["id"], acceptance_criteria="")["task_nr"]
    done = mktask(e, sprint=s["id"])["task_nr"]
    cancelled = mktask(e, sprint=s["id"])["task_nr"]
    c.post(sprint_url("KET", s["id"], "/release"), headers=e["admin"])
    assert claim(e).json()["task_nr"] == running
    with db.engine.begin() as conn:
        conn.execute(text("UPDATE tasks SET status='done' WHERE task_nr=:n"), {"n": done})
    c.post(f"/api/v1/tasks/{cancelled}/transition", headers=e["admin"], json={"to": "cancelled"})
    return s, running, waiting, draft, done, cancelled


def test_close_requires_released_and_a_choice(senv):
    c = senv["c"]
    planned = mksprint(senv, name="Geplant")
    r = c.post(sprint_url("KET", planned["id"], "/close"), headers=senv["admin"], json={"detach": True})
    assert r.status_code == 409 and r.json()["error"]["code"] == "invalid_sprint_status"
    s, *_rest = _sprint_with_mixed_tasks(senv)
    r = c.post(sprint_url("KET", s["id"], "/close"), headers=senv["admin"], json={})
    assert r.status_code == 422 and r.json()["error"]["code"] == "open_tasks"
    r = c.post(sprint_url("KET", s["id"], "/close"), headers=senv["admin"], json={"detach": True, "move_to": 1})
    assert r.status_code == 422
    r = c.post(sprint_url("KET", s["id"], "/close"), headers=senv["admin"], json={"move_to": s["id"]})
    assert r.status_code == 422
    assert c.get(sprint_url("KET", s["id"]), headers=senv["admin"]).json()["status"] == "released"


def test_close_detach(senv):
    c = senv["c"]
    s, running, waiting, draft, done, cancelled = _sprint_with_mixed_tasks(senv)
    r = c.post(sprint_url("KET", s["id"], "/close"), headers=senv["admin"], json={"detach": True})
    assert r.status_code == 200, r.text
    assert set(r.json()["detached"]) == {running, waiting, draft} and r.json()["moved"] == []
    assert r.json()["sprint"]["status"] == "closed" and r.json()["sprint"]["closed_at"]
    statuses = {nr: (task(senv, nr)["status"], task(senv, nr)["sprint"]) for nr in (running, waiting, draft, done, cancelled)}
    assert statuses == {running: ("in_progress", None), waiting: ("defined", None), draft: ("draft", None),
                        done: ("done", s["id"]), cancelled: ("cancelled", s["id"])}


def test_close_move_to_other_sprint(senv):
    c = senv["c"]
    s, running, waiting, draft, done, _cancelled = _sprint_with_mixed_tasks(senv)
    nxt = mksprint(senv, name="Sprint 2")
    other = mksprint(senv, key="OWN")
    r = c.post(sprint_url("KET", s["id"], "/close"), headers=senv["admin"], json={"move_to": other["id"]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "sprint_other_project"
    r = c.post(sprint_url("KET", s["id"], "/close"), headers=senv["admin"], json={"move_to": nxt["id"]})
    assert r.status_code == 200, r.text
    assert set(r.json()["moved"]) == {running, waiting, draft} and r.json()["moved_to"] == nxt["id"]
    assert task(senv, running)["status"] == "in_progress" and task(senv, running)["sprint"] == nxt["id"]
    # The next sprint is still planned: a waiting defined task returns to draft there (assignment rule).
    assert task(senv, waiting)["status"] == "draft" and task(senv, waiting)["sprint"] == nxt["id"]
    assert task(senv, done)["sprint"] == s["id"]


# --- Batch creation -------------------------------------------------------------------------

def test_batch_create_drafts(senv):
    c = senv["c"]
    s = mksprint(senv)
    r = c.post(sprint_url("KET", s["id"], "/tasks"), headers=senv["admin"], json={"tasks": [
        {"title": "Nur Titel"},
        {"title": "Komplett", "description": "D", "acceptance_criteria": "- A", "assigned_to": "hermes", "prio": 1},
    ]})
    assert r.status_code == 201, r.text
    created = r.json()["created"]
    assert [t["title"] for t in created] == ["Nur Titel", "Komplett"]
    assert all(t["status"] == "draft" and t["sprint"] == s["id"] for t in created)
    assert created[1]["assigned_to"] == "hermes" and created[1]["prio"] == 1
    # Also drafts in a released sprint; a later release publishes the complete ones.
    c.post(sprint_url("KET", s["id"], "/release"), headers=senv["admin"])
    r = c.post(sprint_url("KET", s["id"], "/tasks"), headers=senv["admin"],
               json={"tasks": [{"title": "Nachzügler", "description": "D", "acceptance_criteria": "- A"}]})
    assert r.json()["created"][0]["status"] == "draft"
    released = c.post(sprint_url("KET", s["id"], "/release"), headers=senv["admin"]).json()["released"]
    assert [t["title"] for t in released] == ["Nachzügler"]


def test_batch_create_is_all_or_nothing(senv):
    c = senv["c"]
    s = mksprint(senv)
    r = c.post(sprint_url("KET", s["id"], "/tasks"), headers=senv["admin"], json={"tasks": [
        {"title": "gut"}, {"title": "schlecht", "assigned_to": "gibt-es-nicht"}]})
    assert r.status_code == 422 and r.json()["error"]["message"].startswith("Eintrag 2:")
    assert c.get("/api/v1/tasks?project=KET", headers=senv["admin"]).json()["total"] == 0
    assert c.post(sprint_url("KET", s["id"], "/tasks"), headers=senv["admin"], json={"tasks": []}).status_code == 422
    t = mktask(senv)  # Numbering was rolled back with the batch.
    assert t["task_nr"] == "KET-0001"


# --- Reopen -------------------------------------------------------------------------------

def _closed_sprint(e, detach=True, move_to=None):
    """Close a sprint with mixed tasks; returns the sprint plus the open task numbers."""
    s, running, waiting, draft, done, cancelled = _sprint_with_mixed_tasks(e)
    body = {"detach": True} if detach else {"move_to": move_to}
    r = e["c"].post(sprint_url("KET", s["id"], "/close"), headers=e["admin"], json=body)
    assert r.status_code == 200, r.text
    return s, running, waiting, draft, done, cancelled


def test_reopen_requires_closed_status(senv):
    c = senv["c"]
    planned = mksprint(senv, name="Geplant")
    r = c.post(sprint_url("KET", planned["id"], "/reopen"), headers=senv["admin"], json={})
    assert r.status_code == 409 and r.json()["error"]["code"] == "invalid_sprint_status"
    s = mksprint(senv, name="Freigegeben")
    c.post(sprint_url("KET", s["id"], "/release"), headers=senv["admin"])
    r = c.post(sprint_url("KET", s["id"], "/reopen"), headers=senv["admin"], json={})
    assert r.status_code == 409 and r.json()["error"]["code"] == "invalid_sprint_status"
    assert c.get(sprint_url("KET", s["id"]), headers=senv["admin"]).json()["status"] == "released"


def test_reopen_rights(senv):
    c = senv["c"]
    own = mksprint(senv, key="OWN", who="alex")
    c.post(sprint_url("OWN", own["id"], "/release"), headers=senv["alex"])
    c.post(sprint_url("OWN", own["id"], "/close"), headers=senv["alex"], json={"detach": True})
    assert c.post(sprint_url("OWN", own["id"], "/reopen"), headers=senv["orch"], json={}).status_code == 403
    assert c.post(sprint_url("OWN", own["id"], "/reopen"), headers=senv["hermes"], json={}).status_code == 403
    assert c.post(sprint_url("OWN", own["id"], "/reopen"), headers=senv["alex"], json={}).status_code == 200


def test_reopen_restores_detached_tasks(senv):
    c = senv["c"]
    s, running, waiting, draft, done, cancelled = _closed_sprint(senv)
    assert c.get(sprint_url("KET", s["id"]), headers=senv["admin"]).json()["status"] == "closed"
    r = c.post(sprint_url("KET", s["id"], "/reopen"), headers=senv["admin"], json={"restore_tasks": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sprint"]["status"] == "released" and body["sprint"]["closed_at"] is None
    assert set(body["restored"]) == {running, waiting, draft} and body["skipped"] == []
    # Statuses unchanged; the sprint is released, so nothing is turned into a draft again.
    assert [(task(senv, nr)["status"], task(senv, nr)["sprint"]) for nr in (running, waiting, draft)] == \
        [(task(senv, nr)["status"], s["id"]) for nr in (running, waiting, draft)]
    assert task(senv, waiting)["status"] == "defined" and task(senv, running)["status"] == "in_progress"
    assert task(senv, draft)["status"] == "draft"
    # Finished tasks never moved out and stay.
    assert task(senv, done)["sprint"] == s["id"] and task(senv, cancelled)["sprint"] == s["id"]
    # Events: the move back is a `sprint` event with no old value.
    assert ("sprint", None, s["name"]) in events(senv, running)


def test_reopen_across_a_move_back_and_forth(senv):
    """The close moved tasks to another sprint: reopen only takes back the ones still there, the ones moved on
    afterwards are skipped."""
    c = senv["c"]
    nxt = mksprint(senv, name="Sprint 2")
    s, running, waiting, draft, done, cancelled = _closed_sprint(senv, detach=False, move_to=nxt["id"])
    assert task(senv, running)["sprint"] == nxt["id"]
    # Someone moves `draft` on into a third sprint after the close.
    third = mksprint(senv, name="Sprint 3")
    assert c.patch(f"/api/v1/tasks/{draft}", headers=senv["admin"], json={"sprint": third["id"]}).status_code == 200
    r = c.post(sprint_url("KET", s["id"], "/reopen"), headers=senv["admin"], json={})
    body = r.json()
    assert set(body["restored"]) == {running, waiting}
    assert body["skipped"] == [draft]  # Moved elsewhere later – stays in Sprint 3.
    assert task(senv, draft)["sprint"] == third["id"]
    assert task(senv, running)["sprint"] == s["id"]


def test_reopen_skips_finished_tasks_and_keeps_their_sprint(senv):
    """A task finished after the close stays where it is and is reported as skipped."""
    c = senv["c"]
    s, running, waiting, draft, _done, _cancelled = _closed_sprint(senv)
    with db.engine.begin() as conn:
        conn.execute(text("UPDATE tasks SET status='done' WHERE task_nr=:n"), {"n": waiting})
    r = c.post(sprint_url("KET", s["id"], "/reopen"), headers=senv["admin"], json={})
    body = r.json()
    assert set(body["restored"]) == {running, draft} and body["skipped"] == [waiting]
    assert task(senv, waiting)["sprint"] is None and task(senv, waiting)["status"] == "done"


def test_reopen_without_restore(senv):
    c = senv["c"]
    s, running, waiting, draft, _done, _cancelled = _closed_sprint(senv)
    r = c.post(sprint_url("KET", s["id"], "/reopen"), headers=senv["admin"], json={"restore_tasks": False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sprint"]["status"] == "released" and body["restored"] == [] and body["skipped"] == []
    assert task(senv, running)["sprint"] is None and task(senv, waiting)["sprint"] is None


def test_reopen_round_trip_close_reopen_close(senv):
    """close → reopen → close again works and moves the tasks out a second time."""
    c = senv["c"]
    s, running, waiting, draft, _done, _cancelled = _closed_sprint(senv)
    assert c.post(sprint_url("KET", s["id"], "/reopen"), headers=senv["admin"], json={}).status_code == 200
    assert set(task(senv, nr)["sprint"] for nr in (running, waiting, draft)) == {s["id"]}
    r = c.post(sprint_url("KET", s["id"], "/close"), headers=senv["admin"], json={"detach": True})
    assert r.status_code == 200, r.text
    assert set(r.json()["detached"]) == {running, waiting, draft}


def test_reopen_ignores_same_named_sprint_in_another_project(senv):
    """Sprint names are only unique per project: a `sprint` event of a different project's same-named sprint must
    not be picked up (it would otherwise make reopen fail with `sprint_other_project`)."""
    c = senv["c"]
    mkproject(senv, key="OTP")
    # OTP gets its own "Sprint 1" whose close detaches a task – a same-named `sprint` event in another project.
    other = mksprint(senv, key="OTP")
    other_task = mktask(senv, project="OTP", sprint=other["id"])["task_nr"]
    assert c.post(sprint_url("OTP", other["id"], "/release"), headers=senv["admin"]).status_code == 200
    assert c.post(sprint_url("OTP", other["id"], "/close"), headers=senv["admin"],
                  json={"detach": True}).status_code == 200
    assert task(senv, other_task)["sprint"] is None
    # KET closes and reopens its own "Sprint 1": the OTP task is not restored and not touched.
    s, running, waiting, draft, _done, _cancelled = _closed_sprint(senv)
    assert s["name"] == other["name"] == "Sprint 1"
    r = c.post(sprint_url("KET", s["id"], "/reopen"), headers=senv["admin"], json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["restored"]) == {running, waiting, draft} and body["skipped"] == []
    assert task(senv, other_task)["sprint"] is None  # Belongs to OTP, stays detached.


def test_reopen_does_not_restore_tasks_moved_out_before_close(senv):
    """A task moved out of the sprint on purpose while it was released (before the close) is not restored: only
    the close's own moves are taken back."""
    c = senv["c"]
    s, running, waiting, draft, _done, _cancelled = _sprint_with_mixed_tasks(senv)
    # Moved out by hand while the sprint is released – its `sprint` event lies before the close call.
    early = mktask(senv, sprint=s["id"])["task_nr"]
    assert c.patch(f"/api/v1/tasks/{early}", headers=senv["admin"], json={"sprint": None}).status_code == 200
    assert task(senv, early)["sprint"] is None
    assert c.post(sprint_url("KET", s["id"], "/close"), headers=senv["admin"],
                  json={"detach": True}).status_code == 200
    r = c.post(sprint_url("KET", s["id"], "/reopen"), headers=senv["admin"], json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["restored"]) == {running, waiting, draft}
    assert early not in body["restored"] and early not in body["skipped"]
    assert task(senv, early)["sprint"] is None  # Stays where it was moved before the close.


def test_reopen_falls_back_to_events_for_sprints_closed_before_the_column(senv):
    """Sprints closed before `moved_task_ids` existed (NULL) still reopen: the `sprint` events written by the close
    call are used instead, limited to this sprint's project and to the close's timestamp."""
    c = senv["c"]
    s, running, waiting, draft, _done, _cancelled = _closed_sprint(senv)
    with db.engine.begin() as conn:  # Pretend the sprint was closed before the migration.
        conn.execute(text("UPDATE sprints SET moved_task_ids = NULL WHERE id = :i"), {"i": s["id"]})
    r = c.post(sprint_url("KET", s["id"], "/reopen"), headers=senv["admin"], json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["restored"]) == {running, waiting, draft} and body["skipped"] == []
    assert set(task(senv, nr)["sprint"] for nr in (running, waiting, draft)) == {s["id"]}
