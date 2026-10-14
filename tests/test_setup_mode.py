"""Setup mode: app starts without DB, /admin/database is reachable, API returns 503."""
from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import csrf_of, login


def test_app_starts_without_db(client):
    r = client.get("/healthz")
    assert r.status_code == 503
    assert r.json()["db"] == "unavailable"
    assert "db_error" not in r.json() and "gitea_error" not in r.json()


def test_api_returns_503(client):
    for language, message in (("en", "Database unavailable (setup mode)"),
                              ("de", "Datenbank nicht verfügbar (Setup-Modus)")):
        r = client.get("/api/v1/projects", headers={"Cookie": f"tm_lang={language}"})
        assert r.status_code == 503
        assert r.json()["error"] == {"code": "db_unavailable", "message": message}


def test_pages_redirect_to_admin_database(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/database"


def test_admin_database_requires_login(client):
    r = client.get("/admin/database", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_bootstrap_login_and_admin_page(client):
    assert login(client).status_code == 303
    r = client.get("/admin/database")
    assert r.status_code == 200
    assert "Setup-Modus" in r.text and "Verbindung testen" in r.text
    assert 'value="taskadmin"' in r.text  # Default DB user.
    assert 'name="password" autocomplete="new-password"\n             placeholder=' in r.text  # Never prefilled.


def test_wrong_password_and_rate_limit(client):
    for _ in range(5):
        assert login(client, "falsch").status_code == 401
    assert login(client).status_code == 429  # Locked out even with the correct password.


def test_login_requires_csrf(client):
    client.get("/login")
    r = client.post("/login", data={"username": "admin", "password": "x"})
    assert r.status_code == 403


def test_tailscale_login_only_from_loopback(monkeypatch, settings):
    monkeypatch.setattr(settings, "ui_allowed_logins", "admin@github")
    hdr = {"Tailscale-User-Login": "admin@github"}
    with TestClient(app, client=("127.0.0.1", 5000)) as c:
        assert c.get("/admin/database", headers=hdr).status_code == 200
    with TestClient(app, client=("192.168.1.50", 5000)) as c:
        assert c.get("/admin/database", headers=hdr, follow_redirects=False).status_code == 303
    with TestClient(app, client=("127.0.0.1", 5000)) as c:
        r = c.get("/admin/database", headers={"Tailscale-User-Login": "fremd@github"}, follow_redirects=False)
        assert r.status_code == 303


def test_db_test_fails_and_save_refused(client, state_dir):
    login(client)
    csrf = csrf_of(client)
    form = {"host": "127.0.0.1", "port": "1", "database": "x", "user": "u", "password": "pw"}
    r = client.post("/admin/database/test", data=form, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200 and "❌" in r.text and "Speichern" not in r.text
    r = client.post("/admin/database/save", data=form, headers={"X-CSRF-Token": csrf})
    assert "Nicht gespeichert" in r.text
    assert not (state_dir / "db.json").exists()


def test_db_post_requires_csrf(client):
    login(client)
    r = client.post("/admin/database/test", data={"host": "h", "port": "1", "database": "d", "user": "u"})
    assert r.status_code == 403


def test_branding_assets_served(client):
    """TaskBrigade logo: favicon links and header/login logo, including in setup mode."""
    html = client.get("/login").text
    for ref in ('rel="icon" type="image/svg+xml"', 'rel="apple-touch-icon"', 'class="tb-logo"', "/static/brand/logo@2x.png"):
        assert ref in html, ref
    for path, ctype in (("/static/brand/favicon.svg", "image/svg+xml"), ("/static/brand/favicon-32.png", "image/png"),
                        ("/static/brand/apple-touch-icon.png", "image/png"),
                        ("/static/brand/logo.png", "image/png"), ("/static/brand/logo@2x.png", "image/png"),
                        ("/static/brand/mark.svg", "image/svg+xml")):
        r = client.get(path)
        assert r.status_code == 200 and r.headers["content-type"].startswith(ctype), path
    assert "fonts.googleapis" not in client.get("/static/app.css").text  # No external CDN assets.
