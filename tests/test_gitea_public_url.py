"""Internal vs. public Gitea address (GITEA_PUBLIC_URL): clone URLs, git credentials and links use the public one."""
import pytest
from fastapi.testclient import TestClient

from app.db import db
from app.main import app
from app.models import Actor
from app.services import settings_store
from app.services.settings_store import to_public_url
from cli.tm import _host_matches
from tests.conftest import bearer, make_token, requires_db
from tests.gitea_fake import GITEA, SERVICE_USER, FakeGitea

PUBLIC = "http://localhost:3000"
SHA = "c" * 40


@pytest.mark.parametrize("url,expected", [
    (f"{GITEA}/taskbrigade/demo", f"{PUBLIC}/taskbrigade/demo"),
    (f"{GITEA}/taskbrigade/demo/pulls/1", f"{PUBLIC}/taskbrigade/demo/pulls/1"),
    (GITEA, PUBLIC),
    (f"{GITEA}0/x", f"{GITEA}0/x"),  # Another port is not the internal address.
    ("https://elsewhere.example.org/x", "https://elsewhere.example.org/x"),
    (None, None),
])
def test_to_public_url(url, expected):
    assert to_public_url(url, GITEA + "/", PUBLIC + "/") == expected


@pytest.mark.parametrize("public", ["", None, GITEA])
def test_to_public_url_without_separate_address(public):
    assert to_public_url(f"{GITEA}/a/b", GITEA, public) == f"{GITEA}/a/b"


def test_git_credential_host_match_uses_public_address():
    request = {"protocol": "http", "host": "localhost:3000"}
    assert _host_matches(request, PUBLIC)
    assert not _host_matches(request, "http://gitea:3000")


# --- with the test database ----------------------------------------------------------------

@pytest.fixture
def env(ready_db, respx_mock):
    gitea = FakeGitea(respx_mock)
    with db.session() as s:
        settings_store.update(s, {"gitea_token": "svc-token", "gitea_default_owner": SERVICE_USER}, None)
        s.query(Actor).filter_by(name="hermes").one().gitea_username = "hermes-bot"
        s.commit()
    with TestClient(app, client=("127.0.0.1", 40000)) as c:
        yield {"c": c, "gitea": gitea, "admin": bearer(make_token("admin")), "hermes": bearer(make_token("hermes"))}


def _set_public(url):
    with db.session() as s:
        settings_store.update(s, {"gitea_public_url": url}, None)
        s.commit()


def _project(e):
    r = e["c"].post("/api/v1/projects", headers=e["admin"], json={
        "key": "KET", "name": "Projekt KET", "status": "active", "privacy": "local_only", "gitea_mode": "create"})
    assert r.status_code == 201, r.text
    return r.json()


@requires_db
@pytest.mark.db
def test_project_urls_use_public_address(env):
    _set_public(PUBLIC)
    p = _project(env)
    assert p["clone_url"] == f"{PUBLIC}/{SERVICE_USER}/ket.git"
    assert p["repo_url"] == f"{PUBLIC}/{SERVICE_USER}/ket"
    # The app itself keeps talking to the internal address.
    assert all(str(call.request.url).startswith(GITEA) for call in env["gitea"].calls)


@requires_db
@pytest.mark.db
def test_without_public_address_everything_stays_internal(env):
    p = _project(env)
    assert p["clone_url"] == f"{GITEA}/{SERVICE_USER}/ket.git" and p["repo_url"] == f"{GITEA}/{SERVICE_USER}/ket"
    with db.session() as s:
        assert settings_store.gitea_public_url(s) == GITEA


@requires_db
@pytest.mark.db
def test_git_credentials_and_pr_link_use_public_address(env):
    c = env["c"]
    _set_public(PUBLIC + "/")
    _project(env)
    with db.session() as s:
        from app import crypto
        from app.services.actors import _fernet

        hermes = s.query(Actor).filter_by(name="hermes").one()
        hermes.gitea_token_enc = crypto.encrypt(_fernet(), "hermes-gitea-token")
        s.commit()
    creds = c.get("/api/v1/agents/me/git-credentials", headers=env["hermes"]).json()
    assert creds["gitea_url"] == PUBLIC

    r = c.post("/api/v1/tasks", headers=env["admin"], json={
        "project": "KET", "title": "Login", "description": "d", "acceptance_criteria": "- a", "assigned_to": "hermes"})
    nr = r.json()["task_nr"]
    t = c.post("/api/v1/agents/me/claim", headers=env["hermes"]).json()
    assert t["project_info"]["clone_url"] == f"{PUBLIC}/{SERVICE_USER}/ket.git"
    env["gitea"].push_branch(SERVICE_USER, "ket", t["branch_name"])
    r = c.post(f"/api/v1/tasks/{nr}/submit", headers=env["hermes"],
               json={"branch_name": t["branch_name"], "commit_sha": SHA, "summary": "fertig"})
    assert r.status_code == 200, r.text
    assert r.json()["pr_url"] == f"{PUBLIC}/{SERVICE_USER}/ket/pulls/1"


@requires_db
@pytest.mark.db
def test_public_url_setting_is_validated(env):
    r = env["c"].put("/api/v1/admin/settings", headers=env["admin"], json={"gitea_public_url": "localhost:3000"})
    assert r.status_code == 422
