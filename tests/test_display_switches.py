"""Profile switches "Show all projects" and "Show all agents" end to end: saved through the real
profile form (htmx on change), survive a reload, and every list/choice page follows them – off and on."""
import re

import pytest
from fastapi.testclient import TestClient

from app.db import db
from app.main import app
from app.models import Actor
from tests.conftest import login
from tests.test_agent_owner import _agent
from tests.test_owner import human
from tests.test_workflow import claim, env, gitea, mkproject, mktask  # noqa: F401 (Fixtures)

pytestmark = pytest.mark.db


@pytest.fixture
def denv(env):
    """admin owns KET and the private agent codex; olga (member) owns OLGA and the *public* agents hermes and
    opencode (a reviewer agent); claude-code is public without an owner."""
    env["olga"] = human(env, "olga", password="olga-passwort-123")  # gitleaks:allow (test fixture)
    mkproject(env)
    mkproject(env, key="OLGA")
    assert env["c"].patch("/api/v1/projects/OLGA", headers=env["admin"], json={"owner": "olga"}).status_code == 200
    _agent(env, "codex", owner="admin", is_public=False)
    _agent(env, "hermes", owner="olga", is_public=True)
    _agent(env, "opencode", owner="olga", is_public=True, role="reviewer")
    env["c"].post("/api/v1/projects/OLGA/sprints", headers=env["admin"], json={"name": "Olga-Sprint"})
    return env


def _ui() -> TestClient:
    ui = TestClient(app)
    assert login(ui).status_code == 303
    ui.csrf = ui.get("/profile").text.split('"X-CSRF-Token": "')[1].split('"')[0]
    return ui


def _toggle(ui: TestClient, **switches: bool) -> None:
    """What the browser sends when a switch changes: the whole #pf-display form via htmx (checked ones only)."""
    data = {"csrf_token": ui.csrf, **{k: "1" for k, on in switches.items() if on}}
    r = ui.post("/profile/display", data=data, headers={"HX-Request": "true"})
    assert r.status_code == 200 and r.text == "Gespeichert."


def _options(html: str, name: str) -> set[str]:
    """Option values (or texts) of the last <select name=…> on the page – the quick capture comes first."""
    selects = re.findall(rf'<select[^>]*\bname="{name}"[^>]*>(.*?)</select>', html, re.S)
    out = set()
    for value, text in re.findall(r'<option(?: value="([^"]*)")?[^>]*>([^<]*)', selects[-1]):
        if not text.strip().startswith("–"):  # "– choose –" placeholders.
            out.add(value if value else text.strip())
    return out - {"", "none", "all", "Alle meine Projekte", "Alle Akteure", "Alle Projekte"}


def _checked(page: str, name: str) -> bool:
    tag = re.search(rf'<input[^>]*name="{name}"[^>]*>', page).group(0)
    return " checked" in tag


# --- Saving ----------------------------------------------------------------------------------------------------

def test_switches_sit_in_their_own_form_and_save_on_change(denv):
    with _ui() as ui:
        page = ui.get("/profile").text
        form = page.split('<form id="pf-display"', 1)[1].split("</form>", 1)[0]
        assert 'hx-post="/profile/display"' in form and 'hx-trigger="change"' in form
        assert 'name="show_all_projects"' in form and 'name="show_all_agents"' in form
        assert 'name="csrf_token"' in form
        assert not _checked(page, "show_all_projects") and not _checked(page, "show_all_agents")

        _toggle(ui, show_all_projects=True, show_all_agents=True)
        page = ui.get("/profile").text  # Reload shows the stored state.
        assert _checked(page, "show_all_projects") and _checked(page, "show_all_agents")
        # "Save all changes" of the notification form does not reset them.
        assert "Gespeichert." in ui.post("/profile/notifications", data={"csrf_token": ui.csrf}).text
        page = ui.get("/profile").text
        assert _checked(page, "show_all_projects") and _checked(page, "show_all_agents")

        _toggle(ui, show_all_projects=False, show_all_agents=True)  # Unchecked = missing from the form = off.
        page = ui.get("/profile").text
        assert not _checked(page, "show_all_projects") and _checked(page, "show_all_agents")
    with db.session() as s:
        admin = s.query(Actor).filter_by(name="admin").one()
        assert admin.show_all_projects is False and admin.show_all_agents is True
    with _ui() as ui:  # New login, new browser: still stored.
        assert _checked(ui.get("/profile").text, "show_all_agents")


def test_profile_help_text_describes_the_new_agent_rule(denv):
    with _ui() as ui:
        de = ui.get("/profile").text
        assert "Aus: Auswahlen, Agentenfilter und Admin → Benutzer & Agenten zeigen nur deine eigenen Agenten." in de
        ui.cookies.set("tm_lang", "en")
        en = ui.get("/profile").text
        assert "Off: choices, agent filters and Admin → Users & agents show only your own agents." in en


# --- Show all projects -----------------------------------------------------------------------------------------

def test_projects_switch_off_and_on_on_every_page(denv):
    e = denv
    mktask(e, title="KET-Task")
    nr = mktask(e, project="OLGA", title="Olga-Task", assigned_to="claude-code")["task_nr"]
    claim(e)
    assert e["c"].post(f"/api/v1/tasks/{nr}/ask", headers=e["claude"], json={"body": "Frage Olga?"}).status_code == 200
    with _ui() as ui:
        for on in (False, True):
            _toggle(ui, show_all_projects=on)
            seen = (lambda text: "Olga-Task" in text) if on else (lambda text: "Olga-Task" not in text)
            projects = ui.get("/projects").text
            assert ("/projects/OLGA" in projects) is on and "/projects/KET" in projects
            assert seen(ui.get("/backlog").text) and seen(ui.get("/kanban").text)
            inbox = ui.get("/").text
            assert ("Frage Olga?" in inbox) is on
            expected = {"KET", "OLGA"} if on else {"KET"}
            assert _options(ui.get("/backlog").text, "project") == expected
            assert _options(ui.get("/kanban").text, "project") == expected
            assert _options(ui.get("/planning").text, "project") == expected
            assert _options(ui.get("/tasks/new").text, "project") == expected
            assert _options(ui.get("/sprints/new").text, "project") == expected
            header = ui.get("/").text.split('<select', 2)[1]  # Quick capture "+" in the header.
            assert ('value="OLGA"' in header) is on
            assert ("Olga-Sprint" in ui.get("/backlog").text) is on
            assert ui.get("/projects/OLGA").status_code == 200  # Direct links keep working.


# --- Show all agents -------------------------------------------------------------------------------------------

def test_agents_switch_off_shows_only_own_agents_everywhere(denv):
    e = denv
    nr = mktask(e, assigned_to="hermes", reviewer="opencode")["task_nr"]
    with _ui() as ui:
        _toggle(ui, show_all_projects=True)
        for page, name in (("/backlog", "agent"), ("/kanban", "agent"), ("/tasks/new", "assigned_to"),
                           ("/backlog", "assigned_to")):
            options = _options(ui.get(page).text, name)
            assert {"codex", "admin", "olga"} <= options, (page, name)  # Own agents and all humans.
            assert not {"hermes", "opencode", "claude-code"} & options, (page, name)  # Public, other owner.
        assert "opencode" not in _options(ui.get("/tasks/new").text, "reviewer")
        assert "opencode" not in _options(ui.get("/projects/KET").text, "default_reviewer")
        # Already assigned agents of another owner stay on the task.
        detail = ui.get(f"/tasks/{nr}").text
        assert "hermes" in _options(detail, "assign") and "opencode" in _options(detail, "reviewer")
        assert "hermes" in ui.get("/kanban").text  # Card shows its assignee.
        assert "hermes" in _options(ui.get("/kanban?agent=hermes").text, "agent")  # Selected filter stays.

        _toggle(ui, show_all_projects=True, show_all_agents=True)
        for page, name in (("/backlog", "agent"), ("/kanban", "agent"), ("/tasks/new", "assigned_to")):
            assert {"codex", "hermes", "opencode", "claude-code"} <= _options(ui.get(page).text, name), page
        assert "opencode" in _options(ui.get("/tasks/new").text, "reviewer")


def test_admin_users_hides_other_owners_agents_with_hint(denv):
    with _ui() as ui:
        page = ui.get("/admin/users").text
        assert "Agenten · olga" not in page and "detail=hermes" not in page and "detail=claude-code" not in page
        assert "detail=codex" in page and "detail=olga" in page and "detail=admin" in page  # Humans always.
        assert "4 Agenten anderer Owner ausgeblendet" in page  # hermes, opencode, claude-code, openclaw-agent
        hint = page.split('class="users-hidden-hint', 1)[1].split("</p>", 1)[0]
        assert "all_agents=1" in hint and ">Alle Agenten anzeigen</a>" in hint, hint
        seg = page.split('class="ui-seg"', 1)[1].split("</div>", 1)[0]
        assert "Alle 3<" in seg and "Agenten 1<" in seg and "Menschen 2<" in seg  # Counts of visible rows.
        page = ui.get("/admin/users?type=agent&all_agents=1").text  # Link: all agents, this view only.
        assert "Agenten · olga" in page and "detail=hermes" in page and "ausgeblendet" not in page
        assert 'name="all_agents" value="1"' in page  # The filter form keeps it.
        assert 'id="user-panel-title" title="hermes"' in ui.get("/admin/users?detail=hermes").text  # Detail opens.

        _toggle(ui, show_all_agents=True)
        page = ui.get("/admin/users").text
        assert "Agenten · olga" in page and "ausgeblendet" not in page


def test_members_keep_own_and_public_agents(denv):
    """Members have no switch; their choices stay own + public (the rule above is for admins)."""
    ui = TestClient(app)
    r = ui.get("/login")
    csrf = r.text.split('name="csrf_token" value="')[1].split('"')[0]
    ui.post("/login", data={"username": "olga", "password": "olga-passwort-123", "csrf_token": csrf})  # gitleaks:allow
    options = _options(ui.get("/backlog").text, "agent")
    assert {"hermes", "opencode", "claude-code"} <= options and "codex" not in options
    assert 'name="show_all_agents"' not in ui.get("/profile").text
