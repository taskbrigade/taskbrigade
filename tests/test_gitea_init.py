"""Compose profile `gitea`: one-shot setup (app/gitea_init.py) – service user, token, URLs, demo project."""
import json

import httpx
import pytest

from app import gitea_init
from app.config import Settings
from app.container import StartupError
from app.db import db
from app.models import Actor, AppSetting, Project, ProjectMember, Task
from app.services import settings_store
from tests.conftest import requires_db
from tests.gitea_fake import API, GITEA, SERVICE_USER, FakeGitea

PUBLIC = "http://localhost:3000"


def _cfg(**kw) -> gitea_init.InitConfig:
    return gitea_init.InitConfig(**{"gitea_url": GITEA, "public_url": PUBLIC, "admin_user": "giteaadmin",
                                    "admin_password": "admin-password-123", **kw})


def test_config_from_env():
    s = Settings(_env_file=None, gitea_url="http://gitea:3000/", gitea_public_url="http://localhost:3300")
    cfg = gitea_init.InitConfig.from_env(s, {"TBR_GITEA_ADMIN_PASSWORD": "pw", "TBR_GITEA_DEMO": "0"})
    assert (cfg.gitea_url, cfg.public_url) == ("http://gitea:3000", "http://localhost:3300")
    # Service user resolved later: the stored default owner (an installation from before 0.8), else taskbrigade.
    assert cfg.admin_user == "giteaadmin" and cfg.service_user == "" and not cfg.demo
    # Names before TaskBrigade 0.8 still work.
    cfg = gitea_init.InitConfig.from_env(s, {"TM_GITEA_ADMIN_PASSWORD": "old", "TM_GITEA_SERVICE_USER": "svc"})
    assert (cfg.admin_password, cfg.service_user) == ("old", "svc")
    # Without GITEA_PUBLIC_URL the public address is the internal one (as in the native installation).
    cfg = gitea_init.InitConfig.from_env(Settings(_env_file=None, gitea_url="http://g:3000"), {})
    assert cfg.public_url == "http://g:3000" and cfg.demo and cfg.admin_password == ""


def test_run_requires_admin_password():
    with pytest.raises(StartupError, match="TBR_GITEA_ADMIN_PASSWORD"):
        gitea_init.run(Settings(_env_file=None), _cfg(admin_password=""))


def test_wait_for_gitea_gives_up(respx_mock):
    respx_mock.get(f"{API}/version").mock(return_value=httpx.Response(502))
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    admin = gitea_init.GiteaAdmin(GITEA, "giteaadmin", "pw")
    with pytest.raises(StartupError, match="not reachable after 4 s"):
        gitea_init.wait_for_gitea(admin, 4, sleep=sleep, clock=lambda: now[0])


def test_wait_for_gitea_returns_version(respx_mock):
    respx_mock.get(f"{API}/version").mock(return_value=httpx.Response(200, json={"version": "1.24.7"}))
    assert gitea_init.wait_for_gitea(gitea_init.GiteaAdmin(GITEA, "a", "b"), 0) == "1.24.7"


def test_check_admin_rejects_non_admin(respx_mock):
    respx_mock.get(f"{API}/user").mock(return_value=httpx.Response(200, json={"login": "x", "is_admin": False}))
    with pytest.raises(gitea_init.GiteaError, match="not a Gitea admin"):
        gitea_init.GiteaAdmin(GITEA, "x", "pw").check_admin()


def test_summary_shows_agent_token_once():
    result = gitea_init.InitResult(steps=["Project DEMO created"], agent_token="tm_abc")
    text = gitea_init.summary(_cfg(), result)
    assert "tm_abc" in text and "tbr prepare --task DEMO-0001" in text and PUBLIC in text
    assert "tm_" not in gitea_init.summary(_cfg(), gitea_init.InitResult())


# --- with the test database ----------------------------------------------------------------

class FakeAdmin:
    """Gitea admin endpoints on top of FakeGitea: users, tokens and `GET /user` per token."""

    def __init__(self, respx_mock):
        self.users = {"giteaadmin"}
        self.tokens: dict[str, str] = {"svc-old": "someone-else"}
        self.created_tokens: list[str] = []
        m = respx_mock
        m.get(f"{API}/user").mock(side_effect=self._me)
        m.get(url__regex=rf"{API}/users/(?P<user>[^/]+)$").mock(side_effect=self._user)
        m.post(f"{API}/admin/users").mock(side_effect=self._create_user)
        m.delete(url__regex=rf"{API}/users/(?P<user>[^/]+)/tokens/(?P<name>[^/]+)$").mock(
            return_value=httpx.Response(404))
        m.post(url__regex=rf"{API}/users/(?P<user>[^/]+)/tokens$").mock(side_effect=self._token)

    def _me(self, request):
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            return httpx.Response(200, json={"login": "giteaadmin", "is_admin": True})
        login = self.tokens.get(auth.removeprefix("token "))
        if login is None:
            return httpx.Response(401, json={"message": "invalid token"})
        return httpx.Response(200, json={"login": login, "email": f"{login}@noreply.localhost",
                                         "is_admin": login == "giteaadmin"})

    def _user(self, request, user):
        return httpx.Response(200 if user in self.users else 404, json={"login": user})

    def _create_user(self, request):
        self.users.add(json.loads(request.content)["username"])
        return httpx.Response(201, json={})

    def _token(self, request, user):
        token = f"token-{user}-{len(self.created_tokens)}"
        self.tokens[token] = user
        self.created_tokens.append(token)
        expected = gitea_init.ADMIN_TOKEN_SCOPES if user == "giteaadmin" else gitea_init.TOKEN_SCOPES
        assert json.loads(request.content)["scopes"] == expected
        return httpx.Response(201, json={"sha1": token})


@pytest.fixture
def fakes(ready_db, respx_mock):
    gitea = FakeGitea(respx_mock)
    return gitea, FakeAdmin(respx_mock)


def _setup(cfg, settings):
    admin = gitea_init.GiteaAdmin(cfg.gitea_url, cfg.admin_user, cfg.admin_password)
    result = gitea_init.InitResult()
    with db.session() as s:
        gitea_init.configure_service(s, admin, cfg, result)
        if cfg.demo:
            gitea_init.ensure_demo(s, admin, cfg, settings, result)
        s.commit()
    return result


@requires_db
@pytest.mark.db
def test_init_creates_service_settings_and_demo(fakes, settings):
    gitea, admin = fakes
    result = _setup(_cfg(), settings)
    assert {SERVICE_USER, gitea_init.DEMO_AGENT} <= admin.users
    assert result.agent_token and result.agent_token.startswith("tm_")
    with db.session() as s:
        # Stored in the database: the app container does not see the init container's environment.
        assert s.get(AppSetting, "gitea_url").setting_value == GITEA
        assert s.get(AppSetting, "gitea_public_url").setting_value == PUBLIC
        assert settings_store.get(s, "gitea_token") == "token-taskbrigade-0"
        assert settings_store.get(s, "gitea_default_owner") == SERVICE_USER
        assert settings_store.get(s, "gitea_admin_token") == "token-giteaadmin-1"
        assert s.get(AppSetting, "gitea_admin_token").is_secret
        agent = s.query(Actor).filter_by(name=gitea_init.DEMO_AGENT).one()
        assert agent.gitea_username == gitea_init.DEMO_AGENT and agent.runtime == "local"
        project = s.query(Project).filter_by(project_key="DEMO").one()
        assert project.status == "active" and project.gitea_url == f"{PUBLIC}/{SERVICE_USER}/demo"
        assert s.get(ProjectMember, (project.id, agent.id)) is not None
        task = s.query(Task).filter_by(project_id=project.id).one()
        assert task.task_nr == "DEMO-0001" and task.status == "defined" and task.assigned_to == agent.id
    assert (SERVICE_USER, "demo") in gitea.repos
    assert gitea.repo(SERVICE_USER, "demo").collaborators == {gitea_init.DEMO_AGENT: "write"}


@requires_db
@pytest.mark.db
def test_init_is_idempotent(fakes, settings):
    _gitea, admin = fakes
    _setup(_cfg(), settings)
    tokens = list(admin.created_tokens)
    again = _setup(_cfg(), settings)
    assert again.steps == [] and again.agent_token is None
    assert admin.created_tokens == tokens  # Valid tokens are kept.
    with db.session() as s:
        assert s.query(Task).count() == 1 and s.query(Project).count() == 1


@requires_db
@pytest.mark.db
def test_init_replaces_invalid_token_and_keeps_owner(fakes, settings):
    _gitea, _admin = fakes
    with db.session() as s:
        settings_store.update(s, {"gitea_token": "svc-old", "gitea_default_owner": "team"}, None)
        s.commit()
    result = _setup(_cfg(demo=False), settings)
    assert f"Service token for {SERVICE_USER} stored" in result.steps
    with db.session() as s:
        assert settings_store.get(s, "gitea_token") == "token-taskbrigade-0"
        assert settings_store.get(s, "gitea_default_owner") == "team"
        assert s.query(Project).count() == 0


@requires_db
@pytest.mark.db
def test_init_keeps_the_service_user_of_an_installation_before_0_8(fakes, settings):
    """Its service user is `taskmeister`: a re-run with the new default must not create a second one."""
    _gitea, admin = fakes
    _setup(_cfg(service_user="taskmeister", demo=False), settings)
    tokens = list(admin.created_tokens)
    again = _setup(_cfg(demo=False), settings)
    assert again.steps == [] and admin.created_tokens == tokens
    assert "taskbrigade" not in admin.users
    with db.session() as s:
        assert settings_store.get(s, "gitea_default_owner") == "taskmeister"
