"""Several owners per project: co-owners (`project_owners`) have the primary owner's rights, views and notifications."""
import pytest

from app.db import db
from app.models import Actor, Project, Task
from app.services import notifications
from app.services.state_machine import RULES, check_allowed
from tests.conftest import BOOTSTRAP_PW
from tests.gitea_fake import SERVICE_USER
from tests.test_owner import _options, _ui_login, human, submitted_task
from tests.test_workflow import claim, env, gitea, mkproject, mktask  # noqa: F401 (Fixtures)


# --- Without a DB -------------------------------------------------------------------------------------------

def test_owner_ids_and_check_allowed_accept_co_owners():
    anna = Actor(id=7, name="anna", actor_type="human", role="member")
    bob = Actor(id=8, name="bob", actor_type="human", role="member")
    carl = Actor(id=9, name="carl", actor_type="human", role="member")
    project = Project(owner_id=7, co_owners=[bob])
    assert project.owner_ids == {7, 8}
    task = Task(status="for_review", assigned_to=3, created_by=1)
    check_allowed(RULES["approve"], task, bob, project.owner_ids)
    check_allowed(RULES["approve"], task, anna, 7)  # A single id still works.
    with pytest.raises(Exception):
        check_allowed(RULES["approve"], task, carl, project.owner_ids)


# --- With a DB ----------------------------------------------------------------------------------------------

@pytest.mark.db
def test_co_owners_via_api_share_rights_and_owner_filter(env):
    c, g = env["c"], env["gitea"]
    anna = human(env, "anna", gitea_username="anna-git")
    bob = human(env, "bob")
    mkproject(env)
    nr = submitted_task(env)
    assert c.post(f"/api/v1/tasks/{nr}/review", headers=anna, json={"decision": "approve"}).status_code == 403
    # Agents and unknown names are rejected; members cannot change a project they do not own.
    assert c.patch("/api/v1/projects/KET", headers=env["admin"], json={"co_owners": ["hermes"]}).status_code == 422
    assert c.patch("/api/v1/projects/KET", headers=anna, json={"co_owners": ["anna"]}).status_code == 403

    r = c.patch("/api/v1/projects/KET", headers=env["admin"], json={"co_owners": ["anna", "admin"]})
    assert r.status_code == 200
    assert r.json()["owner"] == "admin" and r.json()["co_owners"] == ["anna"]  # The primary owner is not repeated.
    assert g.repo(SERVICE_USER, "ket").collaborators.get("anna-git") == "write"
    assert c.post(f"/api/v1/tasks/{nr}/review", headers=bob, json={"decision": "approve"}).status_code == 403
    r = c.post(f"/api/v1/tasks/{nr}/review", headers=anna, json={"decision": "approve"})
    assert r.status_code == 200 and r.json()["status"] == "done"

    # A co-owner manages the project and may add further co-owners.
    r = c.patch("/api/v1/projects/KET", headers=anna, json={"co_owners": ["anna", "bob"]})
    assert r.status_code == 200 and r.json()["co_owners"] == ["anna", "bob"]
    mktask(env, title="KET-Task")
    titles = lambda r: {t["title"] for t in r.json()["items"]}  # noqa: E731
    for who in (anna, bob, env["admin"]):
        assert "KET-Task" in titles(c.get("/api/v1/tasks", headers=who, params={"owner": "me"}))

    # Transferring the primary owner to a co-owner drops them from the co-owner list.
    r = c.patch("/api/v1/projects/KET", headers=env["admin"], json={"owner": "bob"})
    assert r.json()["owner"] == "bob" and r.json()["co_owners"] == ["anna"]
    r = c.patch("/api/v1/projects/KET", headers=bob, json={"co_owners": []})
    assert r.json()["co_owners"] == []
    assert c.patch("/api/v1/projects/KET", headers=anna, json={"name": "x"}).status_code == 403


@pytest.mark.db
def test_co_owners_get_notifications(env):
    human(env, "anna")
    mkproject(env)
    env["c"].patch("/api/v1/projects/KET", headers=env["admin"], json={"co_owners": ["anna"]})
    with db.session() as s:
        project = s.query(Project).filter_by(project_key="KET").one()
        assert [a.name for a in notifications.recipients(s, project)][:2] == ["admin", "anna"]


def _co_owners(env) -> list[str]:
    return env["c"].get("/api/v1/projects/KET", headers=env["admin"]).json()["co_owners"]


@pytest.mark.db
def test_ui_project_shown_to_all_owners_and_editable(env):
    human(env, "anna", password="anna-passwort-123")
    human(env, "bob", password="bob-passwort-1234")  # gitleaks:allow (test fixture)
    mkproject(env)
    mktask(env, title="KET-Task")
    anna = _ui_login("anna", "anna-passwort-123")
    assert "/projects/KET" not in anna.get("/projects").text

    admin = _ui_login("admin", BOOTSTRAP_PW)
    page = admin.get("/projects/KET").text
    assert 'name="co_owners"' not in page and "Co-Owner hinzufügen" in page  # Explicit control, no multi-select.
    assert 'action="/projects/KET/co-owners"' in page
    r = admin.post("/projects/KET/co-owners", data={"csrf_token": admin.csrf, "name": "anna", "tab": "members"},
                   follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("#members")
    assert "Co-Owner+anna+hinzugef" in r.headers["location"]
    admin.post("/projects/KET/co-owners", data={"csrf_token": admin.csrf, "name": "bob", "tab": "settings"})
    assert _co_owners(env) == ["anna", "bob"]

    for ui in (anna, _ui_login("bob", "bob-passwort-1234")):
        assert "/projects/KET" in ui.get("/projects").text  # Shown as an own project to every owner.
        assert "KET-Task" in ui.get("/backlog").text
        assert _options(ui.get("/tasks/new").text, "project") == {"KET"}
    page = anna.get("/projects/KET").text  # A co-owner manages the project: remove buttons for every co-owner.
    assert 'action="/projects/KET/co-owners/bob/remove"' in page and 'action="/projects/KET/co-owners/anna/remove"' in page

    # Saving the settings form leaves the co-owners alone, even with the old multi-select fields.
    r = admin.post("/projects/KET/edit", data={"csrf_token": admin.csrf, "name": "Ketchup", "status": "active",
                                              "privacy": "local_only", "owner": "admin", "co_owners_sent": "1"},
                   follow_redirects=False)
    assert r.status_code == 303
    assert _co_owners(env) == ["anna", "bob"]

    r = anna.post("/projects/KET/co-owners/bob/remove", data={"csrf_token": anna.csrf, "tab": "settings"},
                  follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("#settings")
    assert _co_owners(env) == ["anna"]
    admin.post("/projects/KET/co-owners/anna/remove", data={"csrf_token": admin.csrf})
    assert _co_owners(env) == []
    assert "/projects/KET" not in anna.get("/projects").text


@pytest.mark.db
def test_ui_co_owner_changes_are_checked(env):
    human(env, "anna", password="anna-passwort-123")
    human(env, "carl", password="carl-passwort-123")  # gitleaks:allow (test fixture)
    mkproject(env)
    admin = _ui_login("admin", BOOTSTRAP_PW)
    carl = _ui_login("carl", "carl-passwort-123")
    # Without the CSRF token nothing happens.
    r = admin.post("/projects/KET/co-owners", data={"name": "anna"}, follow_redirects=False)
    assert r.status_code == 403 and _co_owners(env) == []
    # Members who do not own the project may neither add nor remove; they see owners read-only.
    r = carl.post("/projects/KET/co-owners", data={"csrf_token": carl.csrf, "name": "carl"})
    assert r.status_code == 422 and _co_owners(env) == []
    admin.post("/projects/KET/co-owners", data={"csrf_token": admin.csrf, "name": "anna"})
    r = carl.post("/projects/KET/co-owners/anna/remove", data={"csrf_token": carl.csrf})
    assert r.status_code == 422 and _co_owners(env) == ["anna"]
    page = carl.get("/projects/KET").text
    assert "Co-Owner" in page and "anna" in page and "/co-owners" not in page
    # Agents, unknown names, the primary owner and existing co-owners are refused with a message.
    for name, msg in (("hermes", "hermes"), ("nobody", "nobody"), ("", "Bitte eine Person als Co-Owner wählen"),
                      ("admin", "admin ist bereits Owner"), ("anna", "anna ist bereits Owner")):
        r = admin.post("/projects/KET/co-owners", data={"csrf_token": admin.csrf, "name": name})
        assert r.status_code == 422 and msg in r.text, name
    r = admin.post("/projects/KET/co-owners/carl/remove", data={"csrf_token": admin.csrf})
    assert r.status_code == 422 and "carl ist kein Co-Owner" in r.text
    assert _co_owners(env) == ["anna"]


@pytest.mark.db
def test_ui_deactivated_co_owner_shown_and_removable(env):
    human(env, "anna")
    human(env, "bob")
    mkproject(env)
    env["c"].patch("/api/v1/projects/KET", headers=env["admin"], json={"co_owners": ["anna"]})
    assert env["c"].patch("/api/v1/admin/actors/anna", headers=env["admin"],
                          json={"is_active": False}).status_code == 200
    admin = _ui_login("admin", BOOTSTRAP_PW)
    page = admin.get("/projects/KET").text
    assert "deaktiviert" in page and 'action="/projects/KET/co-owners/anna/remove"' in page
    assert "<option>anna</option>" not in page  # Not offered for adding again.
    # Adding someone else keeps the deactivated co-owner; removing them works.
    r = admin.post("/projects/KET/co-owners", data={"csrf_token": admin.csrf, "name": "bob"}, follow_redirects=False)
    assert r.status_code == 303 and _co_owners(env) == ["anna", "bob"]
    admin.post("/projects/KET/co-owners/anna/remove", data={"csrf_token": admin.csrf})
    assert _co_owners(env) == ["bob"]
    # A deactivated person cannot be added.
    r = admin.post("/projects/KET/co-owners", data={"csrf_token": admin.csrf, "name": "anna"})
    assert r.status_code == 422 and _co_owners(env) == ["bob"]
