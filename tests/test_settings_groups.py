"""Admin → Settings in groups (UI 13): every setting in exactly one group, secrets never reach the page,
replace/reset of secrets, every key saves through the form, Gitea test without the token."""
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.db import db
from app.main import app
from app.models import AppSetting
from app.services import settings_store
from app.web import admin_settings
from app.web.common import templates
from tests.conftest import login
from tests.test_i18n import _request

GITEA = "http://gitea.example.internal:3001"
SECRET = "ghp-very-secret-token-WXYZ"  # gitleaks:allow (test fixture)


def test_every_setting_in_exactly_one_group():
    keys = [key for g in settings_store.GROUPS for row in g.rows for key in row]
    assert sorted(keys) == sorted(settings_store.DEFS)  # No key missing, none twice.
    assert all(1 <= len(row) <= 2 for g in settings_store.GROUPS for row in g.rows)
    assert len({g.key for g in settings_store.GROUPS}) == len(settings_store.GROUPS)
    for g in settings_store.GROUPS:
        assert not g.toggle or (settings_store.DEFS[g.toggle].kind == "bool" and settings_store.GROUP_OF[g.toggle] == g.key)
    assert [g.key for g in settings_store.GROUPS][:9] == ["general", "gitea", "agents", "telegram", "email", "data",
                                                         "runner", "gate", "sftp"]


def _view(source: str | None) -> dict[str, dict]:
    return {key: {"label": d.label, "secret": d.secret, "kind": d.kind, "choices": list(d.choices), "group": "",
                  "value": (SECRET if d.secret else (d.choices[0] if d.choices else "false" if d.kind == "bool" else "5"))
                  if source else None, "is_set": bool(source), "source": source}
            for key, d in settings_store.DEFS.items()}


@pytest.mark.parametrize("source", ["db", "env", None])
def test_page_never_contains_secret_or_suffix(source):
    view = admin_settings.browser_view(_view(source))
    assert all(view[k]["value"] is None for k, d in settings_store.DEFS.items() if d.secret)
    html = templates.get_template("admin/settings.html").render(
        request=_request("/admin/settings"), user=SimpleNamespace(name="admin", is_admin=True), db_ready=True,
        groups=admin_settings.group_view(view), csrf="c", workflow_tick_status={}, zone_findings=[])
    assert SECRET not in html and "WXYZ" not in html
    for key in settings_store.DEFS:
        assert f'name="{key}"' in html
    label = {"db": "gesetzt · in der DB gespeichert", "env": "gesetzt · aus .env", None: "nicht gesetzt"}[source]
    assert html.count(label) == sum(d.secret for d in settings_store.DEFS.values())
    assert ('name="reset_gitea_token"' in html) == (source == "db")  # Only a DB value can go back to .env.
    # Disabled areas carry an "off" pill in the group navigation; every group has its card.
    assert html.count('data-group-link="') == len(settings_store.GROUPS)
    assert all(f'id="settings-{g.key}"' in html for g in settings_store.GROUPS)


def test_reset_controls_are_small_buttons():
    """A stored value shows an extra-small secondary "Reset" button (a visually hidden checkbox inside, so saving
    posts reset_<key> as before); secrets reset to .env, other settings to the .env default."""
    html = templates.get_template("admin/settings.html").render(
        request=_request("/admin/settings"), user=SimpleNamespace(name="admin", is_admin=True), db_ready=True,
        groups=admin_settings.group_view(admin_settings.browser_view(_view("db"))), csrf="c",
        workflow_tick_status={}, zone_findings=[])
    toggles = html.count('<label class="ui-reset-toggle"')
    assert toggles == len(settings_store.DEFS)
    assert html.count('<input type="checkbox" class="ui-visually-hidden" name="reset_') == toggles
    secrets = sum(d.secret for d in settings_store.DEFS.values())
    assert html.count("Auf .env zurücksetzen</label>") == secrets  # _request renders German.
    assert html.count("Auf .env-Standard zurücksetzen</label>") == toggles - secrets


# --- With database ---------------------------------------------------------------------------------------

def _csrf(html: str) -> str:
    return html.split('name="csrf_token" value="')[1].split('"')[0]


def _new_value(key: str, d: settings_store.SettingDef, current: str | None) -> str:
    special = {"app_url": "https://taskbrigade.example.org", "gitea_url": "http://gitea2.example.internal:3001",
               "gitea_public_url": "https://gitea.example.org", "sftp_host": "nas.example.internal",
               "sftp_host_key": "SHA256:" + "A" * 43, "sftp_base_dir": "/exchange",
               "sftp_path_pattern": "{base}/{task_nr}", "smtp_host": "smtp.example.org",
               "mail_from_address": "taskbrigade@example.org"}
    if key in special:
        return special[key]
    if d.kind == "bool":
        return "false" if current == "true" else "true"
    if d.kind == "choice":
        return next(c for c in d.choices if c != current)
    if d.kind == "int":
        return "7" if current != "7" else "8"
    return f"value-{key}"


@pytest.fixture
def ui(ready_db):
    with TestClient(app) as c:
        login(c)
        yield c


@pytest.mark.db
def test_every_key_saves_through_the_form_and_resets(ui):
    page = ui.get("/admin/settings").text
    with db.session() as s:
        before = {key: settings_store.get(s, key) for key in settings_store.DEFS}
    expected = {key: _new_value(key, d, before[key]) for key, d in settings_store.DEFS.items()}
    form = {"csrf_token": _csrf(page)}
    for key, d in settings_store.DEFS.items():
        if d.kind != "bool" or expected[key] == "true":  # An unchecked switch is simply not sent.
            form[key] = "1" if d.kind == "bool" else expected[key]
    r = ui.post("/admin/settings", data=form)
    assert r.status_code == 200 and "Saved." in r.text  # ui_language changed from de to en.
    with db.session() as s:
        assert {key: settings_store.get(s, key) for key in settings_store.DEFS} == expected
        assert {row.setting_key for row in s.query(AppSetting)} == set(settings_store.DEFS)
    assert not any(v in r.text for k, v in expected.items() if settings_store.DEFS[k].secret)

    # "Reset to .env"/"Reset to default" for every key removes the stored value again.
    form = {"csrf_token": _csrf(r.text), **{f"reset_{key}": "1" for key in settings_store.DEFS}}
    assert 'name="reset_smtp_password"' in r.text and 'name="reset_merge_style"' in r.text
    r = ui.post("/admin/settings", data=form)
    assert r.status_code == 200
    with db.session() as s:
        assert s.query(AppSetting).count() == 0


@pytest.mark.db
def test_secret_replace_keep_and_reset(ui):
    page = ui.get("/admin/settings").text
    r = ui.post("/admin/settings", data={"csrf_token": _csrf(page), "gitea_token": SECRET})
    assert "Gespeichert." in r.text and SECRET not in r.text and "WXYZ" not in r.text
    assert "gesetzt · in der DB gespeichert" in r.text
    r = ui.post("/admin/settings", data={"csrf_token": _csrf(page), "gitea_token": ""})  # Empty = unchanged.
    with db.session() as s:
        assert settings_store.get(s, "gitea_token") == SECRET
    r = ui.post("/admin/settings", data={"csrf_token": _csrf(page), "gitea_token": "other-token",
                                         "reset_gitea_token": "1"})  # Reset wins over a typed value.
    with db.session() as s:
        assert s.get(AppSetting, "gitea_token") is None
    assert "gesetzt · in der DB gespeichert" not in r.text


@pytest.mark.db
def test_gitea_test_connection_never_shows_the_token(ui, respx_mock):
    page = ui.get("/admin/settings").text
    ui.post("/admin/settings", data={"csrf_token": _csrf(page), "gitea_token": SECRET, "gitea_url": GITEA})
    respx_mock.get(f"{GITEA}/api/v1/version").mock(return_value=httpx.Response(200, json={"version": "1.26.1"}))
    respx_mock.get(f"{GITEA}/api/v1/user").mock(return_value=httpx.Response(
        200, json={"login": "tbr-service", "is_admin": True}))
    r = ui.post("/admin/settings/test-gitea", headers={"X-CSRF-Token": _csrf(page)})
    assert "Gitea 1.26.1" in r.text and "tbr-service" in r.text and SECRET not in r.text
    respx_mock.get(f"{GITEA}/api/v1/version").mock(return_value=httpx.Response(
        500, json={"message": f"token {SECRET} is broken"}))
    r = ui.post("/admin/settings/test-gitea", headers={"X-CSRF-Token": _csrf(page)})
    assert 'class="banner error"' in r.text and "HTTP 500" in r.text and SECRET not in r.text
