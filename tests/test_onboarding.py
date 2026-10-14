"""Agent onboarding: wizard (Admin → Neuer Agent), Gitea user via admin token or manual step, one-time join codes
and `tbr join`."""
import base64
import json
import logging
import stat

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from typer.testing import CliRunner

from app.db import db
from app.main import app
from app.models import Actor, JoinCode, ProjectMember
from app.services import onboarding, settings_store
from cli import join as join_mod
from cli import tm
from tests.conftest import bearer, login, make_token, requires_db
from tests.gitea_fake import API, GITEA, SERVICE_USER, FakeGitea

runner = CliRunner()


# --- tbr join without a server -------------------------------------------------------------------

def test_join_refuses_existing_config_without_force(tmp_path, respx_mock):
    (tmp_path / ".taskbrigade.toml").write_text('url = "x"\n')
    r = runner.invoke(tm.app, ["join", "https://tm.example", "tmj_abc", "--dir", str(tmp_path)])
    assert r.exit_code == 1 and "--force" in r.stderr and "still unused" in r.stderr
    assert not respx_mock.calls  # The code is not consumed.
    assert (tmp_path / ".taskbrigade.toml").read_text() == 'url = "x"\n'


def test_join_reports_used_code(tmp_path, respx_mock):
    respx_mock.post("https://tm.example/api/v1/join").mock(return_value=httpx.Response(410, json={
        "error": {"code": "join_code_used", "message": "already used"}}))
    r = runner.invoke(tm.app, ["join", "https://tm.example/", "tmj_abc", "--dir", str(tmp_path / "a")])
    assert r.exit_code == 1 and "410 join_code_used: already used" in r.stderr
    assert not (tmp_path / "a" / ".taskbrigade.toml").exists()


def test_join_hint_for_insecure_channel(tmp_path, respx_mock):
    respx_mock.post("http://tm.example/api/v1/join").mock(return_value=httpx.Response(403, json={
        "error": {"code": "insecure_channel", "message": "HTTPS only"}}))
    r = runner.invoke(tm.app, ["join", "http://tm.example", "tmj_abc", "--dir", str(tmp_path)])
    assert r.exit_code == 1 and "tailscale serve" in r.stderr


def test_join_rejects_bad_server_and_language(tmp_path):
    r = runner.invoke(tm.app, ["join", "tm.example", "tmj_abc", "--dir", str(tmp_path)])
    assert r.exit_code == 1 and "https://" in r.stderr
    r = runner.invoke(tm.app, ["join", "https://tm.example", "tmj_abc", "--dir", str(tmp_path), "--language", "fr"])
    assert r.exit_code == 1 and "fr" in r.stderr


def test_config_text_and_private_file(tmp_path):
    text_ = join_mod.config_text("https://tm.example", 'tm_a"b', "claude_code", "de")
    import tomllib

    data = tomllib.loads(text_)
    assert data["url"] == "https://tm.example" and data["token"] == 'tm_a"b' and data["language"] == "de"
    assert data["runner"]["command"][0] == "claude"
    assert "runner" not in tomllib.loads(join_mod.config_text("https://x", "t", "other"))
    assert tomllib.loads(join_mod.config_text("https://x", "t", "codex"))["runner"]["git"] == "runner"
    path = tmp_path / "c.toml"
    path.write_text("old")
    path.chmod(0o644)
    join_mod.write_private(path, "new")
    assert path.read_text() == "new" and stat.S_IMODE(path.stat().st_mode) == 0o600


# --- with the test database ----------------------------------------------------------------------

class FakeGiteaAdmin:
    """Admin and per-token user endpoints on top of FakeGitea."""

    def __init__(self, respx_mock):
        self.users = {"giteaadmin", SERVICE_USER}
        self.passwords: dict[str, str] = {}
        self.tokens = {"svc-token": SERVICE_USER, "admin-token": "giteaadmin"}
        self.created: list[dict] = []
        m = respx_mock
        m.get(f"{API}/user").mock(side_effect=self._me)  # Replaces FakeGitea's fixed answer.
        m.get(url__regex=rf"{API}/users/(?P<user>[^/]+)$").mock(side_effect=self._user)
        m.post(f"{API}/admin/users").mock(side_effect=self._create_user)
        m.delete(url__regex=rf"{API}/admin/users/(?P<user>[^/]+)$").mock(side_effect=self._delete_user)
        m.post(url__regex=rf"{API}/users/(?P<user>[^/]+)/tokens$").mock(side_effect=self._token)

    def _login(self, request) -> str | None:
        return self.tokens.get(request.headers.get("Authorization", "").removeprefix("token "))

    def _me(self, request):
        login = self._login(request)
        if login is None:
            return httpx.Response(401, json={"message": "invalid token"})
        return httpx.Response(200, json={"login": login, "email": f"{login}@noreply.localhost",
                                         "is_admin": login == "giteaadmin"})

    def _user(self, request, user):
        if self._login(request) is None:
            return httpx.Response(401, json={"message": "invalid token"})
        return httpx.Response(200 if user in self.users else 404, json={"login": user})

    def _create_user(self, request):
        if self._login(request) != "giteaadmin":
            return httpx.Response(403, json={"message": "admin only"})
        body = json.loads(request.content)
        self.created.append(body)
        self.users.add(body["username"])
        self.passwords[body["username"]] = body["password"]
        return httpx.Response(201, json={"login": body["username"]})

    def _delete_user(self, request, user):
        self.users.discard(user)
        return httpx.Response(204)

    def _token(self, request, user):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Basic "):
            return httpx.Response(401, json={"message": "basic auth required"})
        login, _sep, password = base64.b64decode(auth[6:]).decode().partition(":")
        if login != user or self.passwords.get(user) != password:
            return httpx.Response(401, json={"message": "bad credentials"})
        token = f"token-{user}"
        self.tokens[token] = user
        assert json.loads(request.content)["scopes"] == onboarding.AGENT_TOKEN_SCOPES
        return httpx.Response(201, json={"sha1": token})


@pytest.fixture
def gitea_env(ready_db, respx_mock):
    gitea = FakeGitea(respx_mock)
    admin = FakeGiteaAdmin(respx_mock)
    with db.session() as s:
        settings_store.update(s, {"gitea_url": GITEA, "gitea_token": "svc-token",
                                  "gitea_default_owner": SERVICE_USER}, None)
        s.commit()
    return gitea, admin


@pytest.fixture
def ui(ready_db):
    with TestClient(app, client=("127.0.0.1", 40000)) as c:
        assert login(c).status_code == 303
        c.csrf = c.get("/admin/users").text.split('"X-CSRF-Token": "')[1].split('"')[0]
        yield c


def _post(ui, url, **data):
    return ui.post(url, data={"csrf_token": ui.csrf, **data}, follow_redirects=True)


def _code(page: str) -> str:
    return page.split('id="join-cmd">tbr join ')[1].split()[1]


def _set(**values):
    with db.session() as s:
        settings_store.update(s, values, None)
        s.commit()


@requires_db
@pytest.mark.db
def test_wizard_with_admin_token_creates_gitea_user_and_code(gitea_env, ui, caplog):
    gitea, admin = gitea_env
    _set(gitea_admin_token="admin-token", app_url="https://tm.example")
    gitea.add_repo(SERVICE_USER, "hello")
    from app.services import projects

    with db.session() as s:
        owner = s.scalar(select(Actor).where(Actor.name == "admin"))
        projects.create(s, owner, key="HELLO", name="Hello", status="active", privacy="cloud_ok",
                        gitea_mode="link", gitea_owner=SERVICE_USER, gitea_repo="hello")
        s.commit()
    caplog.set_level(logging.INFO)
    page = _post(ui, "/admin/agents/new", name="bot-1", display_name="Bot", agent_kind="claude_code",
                 runtime="cloud", role="worker", projects="HELLO").text
    assert "Agent bot-1 angelegt." in page and "tbr join https://tm.example tmj_" in page
    assert "curl -fsSL https://tm.example/install.sh | sh" in page and "--dir ~/taskbrigade-work/bot-1" in page
    code = _code(page)
    assert f'<code id="join-token">{code}</code>' in page  # Code also on its own in the orange box.
    with db.session() as s:
        actor = s.scalar(select(Actor).where(Actor.name == "bot-1"))
        assert actor.api_key_hash is None  # No token until the join.
        assert actor.gitea_username == "bot-1" and actor.gitea_token_enc and "token-bot-1" not in actor.gitea_token_enc
        assert actor.runtime == "cloud" and actor.agent_kind == "claude_code"
        row = s.scalar(select(JoinCode).where(JoinCode.actor_id == actor.id))
        assert row.code_hash == onboarding.hash_code(code) and row.used_at is None
        assert s.scalar(select(ProjectMember).where(ProjectMember.actor_id == actor.id)).sync_error is None
    assert admin.created[0]["username"] == "bot-1" and admin.created[0]["must_change_password"] is False
    assert gitea.repo(SERVICE_USER, "hello").collaborators == {"bot-1": "write"}
    with db.engine.connect() as conn:  # Plain code nowhere in the database.
        dump = conn.execute(text("SELECT code_hash FROM join_codes")).scalars().all()
    assert code not in dump
    assert code not in caplog.text and "join_code_created" in caplog.text


@requires_db
@pytest.mark.db
def test_wizard_without_admin_token_guides_manual_step(gitea_env, ui):
    gitea, admin = gitea_env
    page = _post(ui, "/admin/agents/new", name="bot-2", agent_kind="other", runtime="local", role="worker").text
    assert "Gitea-User manuell verknüpfen" in page and "generate-access-token --username bot-2" in page
    assert "automatisch anlegen" not in page and "tbr join" in page
    assert not admin.created
    admin.tokens["manual-token"] = "bot-2"
    page = _post(ui, "/admin/agents/bot-2/gitea", gitea_username="bot-2", gitea_token="manual-token").text
    assert "Gitea-Token geprüft und verschlüsselt gespeichert." in page and "manuell verknüpfen" not in page
    page = _post(ui, "/admin/agents/bot-2/gitea", gitea_username="bot-2", gitea_token="wrong").text
    assert "Gitea-Prüfung fehlgeschlagen" in page
    page = _post(ui, "/admin/agents/bot-2/gitea/check").text
    assert "Gitea-Token ist gültig." in page


@requires_db
@pytest.mark.db
def test_wizard_existing_gitea_user_falls_back_to_manual(gitea_env, ui):
    _gitea, admin = gitea_env
    _set(gitea_admin_token="admin-token")
    admin.users.add("bot-3")
    page = _post(ui, "/admin/agents/new", name="bot-3", agent_kind="other", runtime="local", role="worker").text
    assert "Gitea-User &#39;bot-3&#39; existiert bereits" in page and "tbr join" in page
    with db.session() as s:
        assert s.scalar(select(Actor).where(Actor.name == "bot-3")).gitea_username is None


@requires_db
@pytest.mark.db
def test_wizard_rejects_cloud_agent_for_local_only_project(gitea_env, ui):
    from app.services import projects

    with db.session() as s:
        owner = s.scalar(select(Actor).where(Actor.name == "admin"))
        projects.create(s, owner, key="LOC", name="Local", status="idea", privacy="local_only", gitea_mode="none")
        s.commit()
    r = _post(ui, "/admin/agents/new", name="bot-4", agent_kind="codex", runtime="cloud", role="worker",
              projects="LOC")
    assert r.status_code == 422 and "local_only" in r.text
    with db.session() as s:
        assert s.scalar(select(Actor).where(Actor.name == "bot-4")) is None  # Rolled back completely.


def _new_code(name="hermes", minutes=None):
    with db.session() as s:
        actor = s.scalar(select(Actor).where(Actor.name == name))
        code, _expires = onboarding.create_code(s, actor, None, minutes)
        s.commit()
    return code


@requires_db
@pytest.mark.db
def test_redeem_once_hash_only_and_old_token_invalid(ready_db, caplog):
    old = make_token("hermes")
    code = _new_code()
    caplog.set_level(logging.INFO)
    with TestClient(app, client=("127.0.0.1", 40000)) as c:
        r = c.post("/api/v1/join", json={"code": code})
        assert r.status_code == 200, r.text
        token = r.json()["token"]
        assert r.json()["actor"]["name"] == "hermes" and token.startswith("tm_")
        assert c.get("/api/v1/agents/me", headers=bearer(token)).json()["name"] == "hermes"
        assert c.get("/api/v1/agents/me", headers=bearer(old)).status_code == 401
        again = c.post("/api/v1/join", json={"code": code})
        assert again.status_code == 410 and again.json()["error"]["code"] == "join_code_used"
        assert c.post("/api/v1/join", json={"code": "tmj_unknown"}).json()["error"]["code"] == "join_code_invalid"
    with db.session() as s:
        row = s.scalar(select(JoinCode))
        assert row.code_hash == onboarding.hash_code(code) and row.used_at and row.used_from == "127.0.0.1"
    assert "join_code_redeemed" in caplog.text and "join_code_rejected" in caplog.text
    assert code not in caplog.text and token not in caplog.text


@requires_db
@pytest.mark.db
def test_expired_code_and_new_code_invalidates_old(ready_db):
    first = _new_code()
    second = _new_code()
    with db.engine.begin() as conn:
        conn.execute(text("UPDATE join_codes SET expires_at = expires_at - INTERVAL 1 HOUR"))
    with TestClient(app, client=("127.0.0.1", 40000)) as c:
        assert c.post("/api/v1/join", json={"code": first}).json()["error"]["code"] == "join_code_invalid"
        r = c.post("/api/v1/join", json={"code": second})
        assert r.status_code == 410 and r.json()["error"]["code"] == "join_code_expired"
        assert "abgelaufen" in r.json()["error"]["message"]


@requires_db
@pytest.mark.db
def test_default_validity_is_15_minutes(ready_db):
    with db.session() as s:
        actor = s.scalar(select(Actor).where(Actor.name == "hermes"))
        _code_, expires = onboarding.create_code(s, actor)
        delta = (expires - onboarding.actors.utcnow()).total_seconds()
        assert 14 * 60 < delta <= 15 * 60
        settings_store.update(s, {"join_code_minutes": "5"}, None)
        _code_, expires = onboarding.create_code(s, actor)
        assert (expires - onboarding.actors.utcnow()).total_seconds() <= 5 * 60
        human = s.scalar(select(Actor).where(Actor.name == "admin"))
        with pytest.raises(onboarding.Unprocessable):
            onboarding.create_code(s, human)


@requires_db
@pytest.mark.db
def test_redeem_only_from_trusted_peers(ready_db, monkeypatch, settings):
    code = _new_code()
    monkeypatch.setattr(settings, "api_networks", ["192.168.1.0/24"], raising=False)
    with TestClient(app, client=("192.168.1.50", 40000)) as c:
        r = c.post("/api/v1/join", json={"code": code})
    assert r.status_code == 403 and r.json()["error"]["code"] == "insecure_channel"
    with db.session() as s:
        assert s.scalar(select(JoinCode)).used_at is None  # Still redeemable over HTTPS.


@requires_db
@pytest.mark.db
def test_failed_redeems_are_rate_limited(ready_db):
    with TestClient(app, client=("127.0.0.1", 40000)) as c:
        for _attempt in range(10):
            c.post("/api/v1/join", json={"code": "tmj_guess"})
        r = c.post("/api/v1/join", json={"code": _new_code()})
    assert r.status_code == 429


@requires_db
@pytest.mark.db
def test_admin_api_join_code_and_provision(gitea_env):
    _gitea, _admin = gitea_env
    with TestClient(app, client=("127.0.0.1", 40000)) as c:
        admin = bearer(make_token("admin"))
        r = c.post("/api/v1/admin/actors/openclaw-agent/gitea/provision", headers=admin)
        assert r.status_code == 422 and r.json()["error"]["code"] == "gitea_admin_missing"
        _set(gitea_admin_token="admin-token")
        r = c.post("/api/v1/admin/actors/openclaw-agent/gitea/provision", headers=admin)
        assert r.status_code == 200 and r.json()["gitea_username"] == "openclaw-agent"
        r = c.post("/api/v1/admin/actors/openclaw-agent/join-code", headers=admin)
        assert r.status_code == 201 and r.json()["code"].startswith("tmj_")
        r = c.post("/api/v1/admin/actors/openclaw-agent/join-code", headers=bearer(make_token("codex")))
        assert r.status_code == 403


@requires_db
@pytest.mark.db
def test_tm_join_end_to_end(gitea_env, tmp_path, monkeypatch):
    """UI code → `tbr join` against the in-process app: config written (600), whoami and Git access checked."""
    _gitea, admin = gitea_env
    _set(gitea_admin_token="admin-token", gitea_public_url=GITEA)
    with db.session() as s:
        actor = s.scalar(select(Actor).where(Actor.name == "claude-code"))
        onboarding.provision_gitea(s, actor)
        code, _expires = onboarding.create_code(s, actor)
        s.commit()
    api = TestClient(app, client=("127.0.0.1", 40000))

    def forward(request: httpx.Request):
        res = api.request(request.method, request.url.path, content=request.content,
                          headers={k: v for k, v in request.headers.items() if k.lower() != "host"})
        return httpx.Response(res.status_code, headers=res.headers, content=res.content)

    import respx

    with respx.mock(assert_all_called=False) as m:
        m.route(host="tm.example").mock(side_effect=forward)
        m.get(f"{API}/user").mock(side_effect=admin._me)
        monkeypatch.setattr(join_mod, "credential_helper_set", lambda: False)
        work = tmp_path / "work" / "claude-code"
        r = runner.invoke(tm.app, ["join", "https://tm.example", code, "--dir", str(work)])
        assert r.exit_code == 0, r.output
        assert "Joined as claude-code" in r.stdout and "Git access: ok" in r.stdout
        assert "credential.helper" in r.stdout
        cfg = work / ".taskbrigade.toml"
        assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
        import tomllib

        data = tomllib.loads(cfg.read_text())
        assert data["url"] == "https://tm.example" and data["runner"]["command"][0] == "claude"
        assert api.get("/api/v1/agents/me", headers=bearer(data["token"])).json()["name"] == "claude-code"
        r = runner.invoke(tm.app, ["join", "https://tm.example", code, "--dir", str(work), "--force"])
        assert r.exit_code == 1 and "join_code_used" in r.stderr
