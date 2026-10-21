"""A cancelled dependency counts as resolved (claim, sprint check, thread note, task payload)."""
import pytest

from tests.test_sprints import mksprint, senv  # noqa: F401 (Fixtures)
from tests.test_workflow import claim, env, gitea, mkproject, mktask  # noqa: F401 (Fixtures)

pytestmark = pytest.mark.db


def cancel(e, nr):
    r = e["c"].post(f"/api/v1/tasks/{nr}/transition", headers=e["admin"], json={"to": "cancelled"})
    assert r.status_code == 200, r.text


def test_cancelled_dependency_does_not_block_claim(env):  # noqa: F811
    mkproject(env)
    dep = mktask(env, assigned_to="hermes")
    t = mktask(env, assigned_to="claude-code", depends_on=[dep["task_nr"]])
    assert claim(env).status_code == 204
    r = claim(env, task_nr=t["task_nr"])
    assert r.status_code == 409 and r.json()["error"]["code"] == "blocked"  # draft/defined still blocks
    cancel(env, dep["task_nr"])
    got = env["c"].get(f"/api/v1/tasks/{t['task_nr']}", headers=env["admin"]).json()
    assert got["dependencies"] == [{"task_nr": dep["task_nr"], "status": "cancelled", "title": dep["title"],
                                    "resolved": True}]
    assert claim(env).json()["task_nr"] == t["task_nr"]


def test_cancelling_a_dependency_notes_open_dependents(env):  # noqa: F811
    mkproject(env)
    dep = mktask(env, assigned_to="hermes")
    t = mktask(env, assigned_to="claude-code", depends_on=[dep["task_nr"]])
    other = mktask(env, assigned_to="claude-code")
    cancel(env, dep["task_nr"])
    notes = lambda nr: [c["body"] for c in env["c"].get(  # noqa: E731
        f"/api/v1/tasks/{nr}", headers=env["admin"]).json()["comments"] if c["type"] == "system"]
    assert any(dep["task_nr"] in n and ("cancelled" in n or "abgebrochen" in n) for n in notes(t["task_nr"]))
    assert notes(other["task_nr"]) == []


def test_cancelled_dependency_is_not_late_for_sprint(senv):  # noqa: F811
    from app.db import db
    from app.models import Sprint
    from app.services import sprints

    s = mksprint(senv, name="Jetzt", start_date="2026-10-10")
    loose = mktask(senv, title="lose")["task_nr"]
    waiting = mktask(senv, sprint=s["id"], title="wartet", depends_on=[loose])["task_nr"]
    with db.session() as session:
        preview = sprints.release_preview(session, session.get(Sprint, s["id"]))
        assert [t["task_nr"] for t in preview["waiting"]] == [waiting]  # still late while open
    cancel(senv, loose)
    with db.session() as session:
        preview = sprints.release_preview(session, session.get(Sprint, s["id"]))
        assert waiting not in [t["task_nr"] for t in preview["waiting"]]
