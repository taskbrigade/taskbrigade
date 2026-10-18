"""Step 2: token auth and admin API for actors, settings, and Gitea credentials (against test DB)."""
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import db
from app.main import app
from tests.conftest import bearer, make_token

pytestmark = pytest.mark.db
GITEA = "http://gitea.example.internal:3001"


@pytest.fixture
def api(ready_db):
    with TestClient(app, client=("127.0.0.1", 40000)) as c:
        yield c


@pytest.fixture
def admin(api):
    return bearer(make_token("admin"))


def _row(name: str):
    with db.engine.connect() as conn:
        return conn.execute(text("SELECT * FROM actors WHERE name = :n"), {"n": name}).mappings().one()


def _signed_in(html: str, name: str) -> bool:
    """The header shows the signed-in name in the account menu and still offers logout."""
    return f">{name}</strong>" in html and 'action="/logout"' in html


# --- Token-Auth ----------------------------------------------------------------------

def test_missing_and_invalid_token(api):
    r = api.get("/api/v1/agents/me")
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
    r = api.get("/api/v1/agents/me", headers=bearer("tm_falsch"))
    assert r.status_code == 401


def test_rate_limit_on_auth_failures(api):
    for _ in range(10):
        api.get("/api/v1/agents/me", headers=bearer("tm_falsch"))
    r = api.get("/api/v1/agents/me", headers=bearer(make_token("admin")))
    assert r.status_code == 429


def test_identity_from_token_and_last_seen(api):
    token = make_token("hermes")
    assert _row("hermes")["last_seen_at"] is None
    r = api.get("/api/v1/agents/me", headers=bearer(token))
    assert r.status_code == 200 and r.json()["name"] == "hermes"
    row = _row("hermes")
    assert row["last_seen_at"] is not None
    assert row["api_key_hash"] != token and len(row["api_key_hash"]) == 64
    assert row["api_key_prefix"] == token[:8]


def test_worker_cannot_use_admin_api(api):
    r = api.get("/api/v1/admin/actors", headers=bearer(make_token("codex")))
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"


# --- Actors ------------------------------------------------------------------------

def test_create_agent_returns_token_once(api, admin):
    r = api.post("/api/v1/admin/actors", headers=admin, json={
        "name": "testbot", "display_name": "Test-Bot", "actor_type": "agent", "agent_kind": "other",
        "runtime": "cloud"})
    assert r.status_code == 201, r.text
    token = r.json()["token"]
    assert token.startswith("tm_")
    assert "token" not in api.get("/api/v1/admin/actors/testbot", headers=admin).json()
    assert api.get("/api/v1/agents/me", headers=bearer(token)).json()["runtime"] == "cloud"


@pytest.mark.parametrize("body,status", [
    ({"name": "Ungültig!", "display_name": "x", "actor_type": "agent", "agent_kind": "other"}, 422),
    ({"name": "admin", "display_name": "x", "actor_type": "human"}, 409),
    ({"name": "bot2", "display_name": "x", "actor_type": "agent", "agent_kind": "other", "email": "a@b.c"}, 422),
    ({"name": "bot3", "display_name": "x", "actor_type": "agent"}, 422),  # Missing agent_kind.
    ({"name": "anna", "display_name": "x", "actor_type": "human", "password": "kurz"}, 422),
])
def test_create_validation(api, admin, body, status):
    assert api.post("/api/v1/admin/actors", headers=admin, json=body).status_code == status


def test_human_password_login_and_tailscale_mapping(api, admin, monkeypatch):
    from app.config import get_settings

    r = api.post("/api/v1/admin/actors", headers=admin, json={
        "name": "anna", "display_name": "Anna", "actor_type": "human", "role": "admin",
        "password": "sehr-geheim-123", "tailscale_login": "Anna@GitHub"})
    assert r.status_code == 201
    assert _row("anna")["password_hash"].startswith("$argon2")
    assert _row("anna")["tailscale_login"] == "anna@github"

    with TestClient(app) as ui:
        page = ui.get("/login").text
        csrf = page.split('name="csrf_token" value="')[1].split('"')[0]
        r = ui.post("/login", data={"username": "anna", "password": "falsch-falsch", "csrf_token": csrf})
        assert r.status_code == 401
        r = ui.post("/login", data={"username": "anna", "password": "sehr-geheim-123", "csrf_token": csrf},
                    follow_redirects=False)
        assert r.status_code == 303
        assert _signed_in(ui.get("/admin/users").text, "anna")

    monkeypatch.setattr(get_settings(), "ui_allowed_logins", "anna@github")
    with TestClient(app, client=("127.0.0.1", 5000)) as ui:
        assert _signed_in(ui.get("/admin/users", headers={"Tailscale-User-Login": "anna@github"}).text, "anna")


def test_bootstrap_admin_maps_to_admin_actor(api):
    from tests.conftest import login

    with TestClient(app) as ui:
        assert login(ui).status_code == 303
        assert _signed_in(ui.get("/admin/users").text, "admin")


def test_rotate_token_invalidates_old(api, admin):
    old = make_token("openclaw-agent")
    r = api.post("/api/v1/admin/actors/openclaw-agent/rotate-token", headers=admin)
    new = r.json()["token"]
    assert api.get("/api/v1/agents/me", headers=bearer(old)).status_code == 401
    assert api.get("/api/v1/agents/me", headers=bearer(new)).status_code == 200


def test_deactivate_invalidates_token_until_rotation(api, admin):
    token = make_token("opencode")
    assert api.patch("/api/v1/admin/actors/opencode", headers=admin, json={"is_active": False}).status_code == 200
    assert api.get("/api/v1/agents/me", headers=bearer(token)).status_code == 401
    assert api.post("/api/v1/admin/actors/opencode/rotate-token", headers=admin).status_code == 409
    api.patch("/api/v1/admin/actors/opencode", headers=admin, json={"is_active": True})
    assert api.get("/api/v1/agents/me", headers=bearer(token)).status_code == 401  # Old token remains invalid.
    new = api.post("/api/v1/admin/actors/opencode/rotate-token", headers=admin).json()["token"]
    assert api.get("/api/v1/agents/me", headers=bearer(new)).status_code == 200


def test_cannot_deactivate_self_or_last_admin(api, admin):
    r = api.patch("/api/v1/admin/actors/admin", headers=admin, json={"is_active": False})
    assert r.status_code == 409


def test_patch_rejects_name_and_unknown_fields(api, admin):
    assert api.patch("/api/v1/admin/actors/codex", headers=admin, json={"name": "x"}).status_code == 422
    r = api.patch("/api/v1/admin/actors/codex", headers=admin, json={"lease_minutes": 45, "role": "orchestrator"})
    assert r.status_code == 200 and r.json()["lease_minutes"] == 45 and r.json()["role"] == "orchestrator"


def test_deactivated_ui_session_is_logged_out(api, admin):
    api.post("/api/v1/admin/actors", headers=admin, json={
        "name": "bert", "display_name": "Bert", "actor_type": "human", "role": "admin",
        "password": "sehr-geheim-456"})
    with TestClient(app) as ui:
        csrf = ui.get("/login").text.split('name="csrf_token" value="')[1].split('"')[0]
        ui.post("/login", data={"username": "bert", "password": "sehr-geheim-456", "csrf_token": csrf})
        assert ui.get("/admin/users").status_code == 200
        api.patch("/api/v1/admin/actors/bert", headers=admin, json={"is_active": False})
        assert ui.get("/admin/users", follow_redirects=False).headers["location"] == "/login"


# --- Gitea credentials ------------------------------------------------------------------

def test_gitea_credentials_verified_and_encrypted(api, admin, respx_mock):
    respx_mock.get(f"{GITEA}/api/v1/user").mock(return_value=httpx.Response(
        200, json={"login": "hermes-bot", "email": "hermes@example.org", "is_admin": False}))
    r = api.put("/api/v1/admin/actors/hermes/gitea", headers=admin,
                json={"gitea_username": "hermes-bot", "gitea_token": "gitea-secret-token-1234"})
    assert r.status_code == 200, r.text
    assert r.json()["gitea_token_last4"] == "1234" and r.json()["gitea_email"] == "hermes@example.org"
    row = _row("hermes")
    assert "gitea-secret" not in row["gitea_token_enc"]
    assert respx_mock.calls.last.request.headers["Authorization"] == "token gitea-secret-token-1234"

    token = make_token("hermes")
    creds = api.get("/api/v1/agents/me/git-credentials", headers=bearer(token)).json()
    assert creds == {"username": "hermes-bot", "token": "gitea-secret-token-1234",
                     "email": "hermes@example.org", "gitea_url": GITEA}


@pytest.mark.parametrize("status,body,code", [
    (200, {"login": "jemand-anders"}, "gitea_user_mismatch"),
    (401, {"message": "unauthorized"}, "gitea_check_failed"),
])
def test_gitea_credentials_rejected(api, admin, respx_mock, status, body, code):
    respx_mock.get(f"{GITEA}/api/v1/user").mock(return_value=httpx.Response(status, json=body))
    r = api.put("/api/v1/admin/actors/hermes/gitea", headers=admin,
                json={"gitea_username": "hermes-bot", "gitea_token": "x" * 20})
    assert r.status_code == 422 and r.json()["error"]["code"] == code
    assert _row("hermes")["gitea_token_enc"] is None


def test_git_credentials_404_and_insecure_channel(api):
    token = make_token("codex")
    assert api.get("/api/v1/agents/me/git-credentials", headers=bearer(token)).status_code == 404
    with TestClient(app, client=("192.168.1.50", 40000)) as remote:
        r = remote.get("/api/v1/agents/me/git-credentials", headers=bearer(token))
        assert r.status_code == 403 and r.json()["error"]["code"] == "insecure_channel"
        r = remote.get("/api/v1/agents/me/git-credentials", headers={**bearer(token), "X-Forwarded-Proto": "https"})
        assert r.status_code == 403  # Proxy headers from other IPs do not count.


# --- Settings ---------------------------------------------------------------------------

def test_settings_secret_encrypted_and_masked(api, admin):
    r = api.put("/api/v1/admin/settings", headers=admin,
                json={"gitea_token": "service-token-abcd", "merge_style": "merge", "default_lease_minutes": "90"})
    assert r.status_code == 200
    view = r.json()
    assert view["gitea_token"]["value"] == "…abcd" and view["gitea_token"]["source"] == "db"
    assert view["merge_style"]["value"] == "merge"
    with db.engine.connect() as conn:
        raw = conn.execute(text("SELECT setting_value, is_secret FROM app_settings WHERE setting_key='gitea_token'")).one()
    assert raw.is_secret and "service-token" not in raw.setting_value

    view = api.put("/api/v1/admin/settings", headers=admin, json={"merge_style": None}).json()
    assert view["merge_style"]["value"] == "squash" and view["merge_style"]["source"] == "env"


@pytest.mark.parametrize("body", [{"merge_style": "yolo"}, {"default_lease_minutes": "abc"},
                                  {"unbekannt": "x"}, {"gitea_url": "ftp://x"}])
def test_settings_validation(api, admin, body):
    assert api.put("/api/v1/admin/settings", headers=admin, json=body).status_code == 422
