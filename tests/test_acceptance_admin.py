"""Admin acceptance (spec section 17): create agent in UI → API token and Gitea credentials →
`tm git-credential` returns them → `actors` does not store the Gitea token in plain text."""
import re

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from typer.testing import CliRunner

from app.db import db
from app.main import app
from cli import client as client_mod
from cli import tm
from tests.conftest import login

pytestmark = pytest.mark.db
GITEA = "http://gitea.example.internal:3001"


def _csrf(html: str) -> str:
    return html.split('name="csrf_token" value="')[1].split('"')[0]


def test_acceptance_admin(ready_db, respx_mock, monkeypatch):
    respx_mock.get(f"{GITEA}/api/v1/user").mock(return_value=httpx.Response(
        200, json={"login": "agent-neu", "email": "agent-neu@example.org"}))

    with TestClient(app) as ui:
        login(ui)
        form = ui.get("/admin/users/new").text
        r = ui.post("/admin/users/new", data={
            "csrf_token": _csrf(form), "name": "agent-neu", "display_name": "Neuer Agent", "actor_type": "agent",
            "agent_kind": "other", "role": "worker", "runtime": "local", "lease_minutes": "60",
            "max_parallel_tasks": "1"})
        assert r.status_code == 200 and "nur jetzt" in r.text
        token = re.search(r'id="token" readonly value="(tm_[^"]+)"', r.text)[1]
        assert f'token = "{token}"' in r.text  # config.toml-Snippet

        detail = ui.get("/admin/users/agent-neu").text
        assert token not in detail  # Token is never shown again.
        r = ui.post("/admin/users/agent-neu/gitea", data={
            "csrf_token": _csrf(detail), "gitea_username": "agent-neu", "gitea_token": "gitea-tok-9876",  # gitleaks:allow (test fixture)
            "gitea_email": ""})
        assert "geprüft und verschlüsselt gespeichert" in r.text and "…9876" in r.text

    with db.engine.connect() as conn:
        row = conn.execute(text("SELECT gitea_token_enc, api_key_hash FROM actors WHERE name='agent-neu'")).one()
    assert "gitea-tok-9876" not in row.gitea_token_enc and token not in row.api_key_hash

    # `tm git-credential get` on the agent host (HTTP client → in-process app, called from localhost).
    def fake_http_client(base_url, headers, **_):
        return TestClient(app, base_url=base_url, headers=headers, client=("127.0.0.1", 50000))

    monkeypatch.setattr(client_mod.httpx, "Client", fake_http_client)
    monkeypatch.setenv("TBR_URL", "http://testserver")
    monkeypatch.setenv("TBR_TOKEN", token)
    r = CliRunner().invoke(tm.app, ["git-credential", "get"], input="protocol=http\nhost=gitea.example.internal:3001\n\n")
    assert r.exit_code == 0, r.output
    assert r.stdout == "username=agent-neu\npassword=gitea-tok-9876\n"  # gitleaks:allow (test fixture)


def test_settings_ui_roundtrip(ready_db, respx_mock):
    respx_mock.get(f"{GITEA}/api/v1/version").mock(return_value=httpx.Response(200, json={"version": "1.26.1"}))
    with TestClient(app) as ui:
        login(ui)
        page = ui.get("/admin/settings").text
        csrf = _csrf(page)
        r = ui.post("/admin/settings", data={
            "csrf_token": csrf, "gitea_url": GITEA, "gitea_default_owner": "admin", "merge_style": "squash",
            "branch_protection": "1", "default_lease_minutes": "120", "telegram_chat_id": "",
            "gitea_token": "", "telegram_bot_token": ""})
        assert "Gespeichert." in r.text
        with db.engine.connect() as conn:
            keys = set(conn.execute(text("SELECT setting_key FROM app_settings")).scalars())
        assert keys == {"gitea_default_owner"}  # Unchanged .env defaults are not persisted.
        r = ui.post("/admin/settings/test-gitea", headers={"X-CSRF-Token": csrf})
        assert "Gitea 1.26.1 erreichbar, aber kein Service-Token gesetzt." in r.text


def test_tm_admin_reset_admin_password(ready_db):
    from cli import tm_admin

    r = CliRunner().invoke(tm_admin.app, ["reset-admin-password"], input="notfall-pw-123\nnotfall-pw-123\n")
    assert r.exit_code == 0, r.output
    assert "Passwort für admin gesetzt" in r.output
    db.configure(db.engine.url.render_as_string(hide_password=False))  # CLI discarded the global engine.
    with TestClient(app) as ui:
        page = ui.get("/login").text
        r = ui.post("/login", data={"username": "admin", "password": "notfall-pw-123", "csrf_token": _csrf(page)},
                    follow_redirects=False)
        assert r.status_code == 303
