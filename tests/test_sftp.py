"""SFTP storage: path and host-key protection of `cli/sftp.py` against a fake asyncssh server, `tbr sftp`, MCP tools,
doctor, TASK.md note; with a test DB also settings, per-agent login, `/agents/me/sftp-credentials` (only over secure
peers, audited), task note for uploads and the admin UI."""
import json
import logging

import httpx
import pytest
from typer.testing import CliRunner

from cli import sftp
from cli import tm
from tests.sftp_fake import FakeSftpServer

PW = "sftp-secret-pw-1234"
BASE = "/agent exchange"
SERVER = "https://tm.example"
API = f"{SERVER}/api/v1"
runner = CliRunner()


@pytest.fixture
def server(tmp_path):
    root = tmp_path / "nas"
    (root / "agent exchange").mkdir(parents=True)
    s = FakeSftpServer(root, {"bot": PW}).start()
    yield s
    s.stop()


def target(server, **kw) -> sftp.Target:
    return sftp.Target(**{"host": "127.0.0.1", "port": server.port, "host_key": server.fingerprint,
                          "base_dir": BASE, "username": "bot", "password": PW, **kw})


# --- Paths and settings values ---------------------------------------------------------------------

@pytest.mark.parametrize("path,expected", [
    (None, BASE), ("", BASE), ("DEMO/DEMO-0001", f"{BASE}/DEMO/DEMO-0001"), (f"{BASE}/x.txt", f"{BASE}/x.txt"),
    (BASE + "/", BASE), ("a//b/./c", f"{BASE}/a/b/c"),
])
def test_inside_accepts_paths_below_the_base(path, expected):
    assert sftp.inside(BASE, path) == expected


@pytest.mark.parametrize("path", ["..", "../x", "DEMO/../../etc", "/etc/passwd", "/agent exchange2/x", "/",
                                  "a\\..\\b"])
def test_inside_refuses_breakouts(path):
    with pytest.raises(sftp.SftpError):
        sftp.inside(BASE, path)


def test_task_dir_and_pattern_checks():
    assert sftp.task_dir(sftp.DEFAULT_PATTERN, BASE, "DEMO", "DEMO-0007") == f"{BASE}/DEMO/DEMO-0007"
    assert sftp.task_dir("{base}/results/{task_nr}/", BASE, "DEMO", "DEMO-0007") == f"{BASE}/results/DEMO-0007"
    assert sftp.check_pattern("{base}/{project}/{task_nr}/") == "{base}/{project}/{task_nr}"
    for bad in ("/tmp/{task_nr}", "{base}/{user}", "{base}/../{task_nr}", "{base}/{0}"):
        with pytest.raises(ValueError):
            sftp.check_pattern(bad)


def test_fingerprint_and_base_normalisation():
    fp = "SHA256:" + "a" * 43
    assert sftp.normalize_fingerprint(fp) == fp
    assert sftp.normalize_fingerprint("a" * 43 + "=") == fp
    assert sftp.normalize_fingerprint("sha256:" + "a" * 43) == fp
    assert sftp.normalize_fingerprint("") == ""
    for bad in ("MD5:aa:bb", "SHA256:short"):
        with pytest.raises(ValueError):
            sftp.normalize_fingerprint(bad)
    assert sftp.normalize_base("/agent exchange/") == BASE
    for bad in ("relative", "/a/../b", ""):
        with pytest.raises(ValueError):
            sftp.normalize_base(bad)


# --- Transfers against the fake server ----------------------------------------------------------------

def test_put_ls_get_roundtrip(server, tmp_path):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "report.txt").write_text("hello")
    (tmp_path / "out" / "sub").mkdir()
    (tmp_path / "out" / "sub" / "data.csv").write_text("a,b\n")
    single = tmp_path / "single.txt"
    single.write_text("one")
    dest = f"{BASE}/DEMO/DEMO-0001"
    done = sftp.put(target(server), sftp.plan_upload([single, tmp_path / "out"], dest))
    assert [d["path"] for d in done] == [f"{dest}/single.txt", f"{dest}/out/report.txt", f"{dest}/out/sub/data.csv"]
    assert done[0]["size"] == 3 and done[0]["sha256"] == sftp.file_digest(single)
    assert (server.root / "agent exchange/DEMO/DEMO-0001/out/sub/data.csv").read_text() == "a,b\n"

    entries = sftp.ls(target(server), "DEMO/DEMO-0001")
    assert [(e["name"], e["is_dir"]) for e in entries] == [("out", True), ("single.txt", False)]
    assert sftp.ls(target(server), f"{dest}/single.txt")[0]["size"] == 3

    sftp.get(target(server), f"{dest}/out", tmp_path / "back")
    assert (tmp_path / "back" / "sub" / "data.csv").read_text() == "a,b\n"


def test_paths_outside_the_base_are_refused_before_connecting(server, tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("x")
    for call in (lambda: sftp.put(target(server), [(f, "/etc/x.txt")]),
                 lambda: sftp.put(target(server), [(f, f"{BASE}/../x.txt")]),
                 lambda: sftp.ls(target(server), "/"),
                 lambda: sftp.get(target(server), "../secret", tmp_path / "s")):
        with pytest.raises(sftp.SftpError):
            call()
    assert server.logins == []
    assert not (server.root / "x.txt").exists()


def test_wrong_host_key_is_refused_without_sending_the_password(server):
    with pytest.raises(sftp.SftpError, match="does not match the stored fingerprint"):
        sftp.check(target(server, host_key="SHA256:" + "A" * 43))
    with pytest.raises(sftp.SftpError, match="No host key fingerprint"):
        sftp.check(target(server, host_key=""))
    assert server.logins == []


def test_wrong_password_and_access_check(server):
    with pytest.raises(sftp.SftpError, match="refused"):
        sftp.check(target(server, password="wrong"))
    assert "write test" in sftp.check(target(server))
    assert list((server.root / "agent exchange").iterdir()) == []  # Test file deleted again.
    assert sftp.server_fingerprint("127.0.0.1", server.port) == server.fingerprint
    with pytest.raises(sftp.SftpError, match="Base folder"):
        sftp.check(target(server, base_dir="/missing"))


def test_password_is_not_in_the_repr(server):
    assert PW not in repr(target(server))


# --- tbr sftp / MCP / doctor / TASK.md with a mocked API ---------------------------------------------------

def _api(respx_mock, server, task_dir=f"{BASE}/DEMO/DEMO-0001"):
    info = {"host": "127.0.0.1", "port": server.port, "host_key": server.fingerprint, "base_dir": BASE,
            "pattern": sftp.DEFAULT_PATTERN, "username": "bot"}
    respx_mock.get(f"{API}/agents/me/sftp", params={"task_nr": "DEMO-0001"}).mock(
        return_value=httpx.Response(200, json={**info, "task_dir": task_dir}))
    respx_mock.get(f"{API}/agents/me/sftp").mock(return_value=httpx.Response(200, json=info))
    respx_mock.get(f"{API}/agents/me/sftp-credentials").mock(
        return_value=httpx.Response(200, json={**info, "password": PW}))
    return respx_mock.post(f"{API}/tasks/DEMO-0001/sftp-uploads").mock(
        return_value=httpx.Response(201, json={"task_nr": "DEMO-0001", "body": "noted"}))


@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TBR_URL", SERVER)
    monkeypatch.setenv("TBR_TOKEN", "tm_test")
    monkeypatch.chdir(tmp_path)


def test_cli_put_ls_get_and_task_note(cli_env, respx_mock, server, tmp_path):
    note = _api(respx_mock, server)
    (tmp_path / "result.md").write_text("# Result")
    r = runner.invoke(tm.app, ["sftp", "put", "demo-0001", "result.md", "--to", "final"])
    assert r.exit_code == 0, r.output
    assert f"{BASE}/DEMO/DEMO-0001/final/result.md" in r.output and PW not in r.output
    assert (server.root / "agent exchange/DEMO/DEMO-0001/final/result.md").read_text() == "# Result"
    files = json.loads(note.calls.last.request.content)["files"]
    assert files == [{"path": f"{BASE}/DEMO/DEMO-0001/final/result.md", "size": 8,
                      "sha256": sftp.file_digest(tmp_path / "result.md")}]

    r = runner.invoke(tm.app, ["sftp", "ls", "DEMO-0001"])
    assert r.exit_code == 0 and "final/" in r.output, r.output
    r = runner.invoke(tm.app, ["sftp", "get", "DEMO/DEMO-0001/final/result.md"])
    assert r.exit_code == 1 and "--force" in r.output  # result.md exists in the current folder.
    r = runner.invoke(tm.app, ["sftp", "get", "DEMO/DEMO-0001/final/result.md", "copy.md"])
    assert r.exit_code == 0 and (tmp_path / "copy.md").read_text() == "# Result"

    r = runner.invoke(tm.app, ["sftp", "info"])
    assert r.exit_code == 0 and "bot" in r.output and PW not in r.output


@pytest.mark.parametrize("args", [["put", "DEMO-0001", "result.md", "--to", "../other"],
                                  ["ls", "/etc"], ["get", "../../etc/passwd", "x"]])
def test_cli_refuses_paths_outside(cli_env, respx_mock, server, tmp_path, args):
    note = _api(respx_mock, server)
    (tmp_path / "result.md").write_text("x")
    r = runner.invoke(tm.app, ["sftp", *args])
    assert r.exit_code == 1 and "Error:" in r.output
    assert not note.called and server.logins == []


def test_cli_wrong_host_key(cli_env, respx_mock, server, tmp_path):
    info = {"host": "127.0.0.1", "port": server.port, "host_key": "SHA256:" + "B" * 43, "base_dir": BASE,
            "username": "bot"}
    respx_mock.get(f"{API}/agents/me/sftp").mock(return_value=httpx.Response(200, json=info))
    respx_mock.get(f"{API}/agents/me/sftp-credentials").mock(
        return_value=httpx.Response(200, json={**info, "password": PW}))
    r = runner.invoke(tm.app, ["sftp", "ls"])
    assert r.exit_code == 1 and "does not match" in r.output and server.logins == []


def test_mcp_sftp_tools(respx_mock, server, tmp_path):
    from cli.client import Client, Config
    from cli.mcp_server import TaskBrigadeMCP

    note = _api(respx_mock, server)
    respx_mock.get(f"{API}/tasks/DEMO-0001").mock(return_value=httpx.Response(200, json={
        "task_nr": "DEMO-0001", "project_info": {"key": "DEMO"}}))
    wt = tmp_path / "work" / "DEMO" / ".worktrees" / "DEMO-0001"
    wt.mkdir(parents=True)
    (wt / "out.txt").write_text("data")
    (tmp_path / "secret.txt").write_text("no")
    mcp = TaskBrigadeMCP(Client(Config(SERVER, "tm_test")), tmp_path / "work")

    assert "not a file in the working folder" in mcp.sftp_put("DEMO-0001", ["../../../../secret.txt"])
    assert not note.called
    out = mcp.sftp_put("demo-0001", ["out.txt"])
    assert "Uploaded and noted in DEMO-0001" in out and note.called
    assert (server.root / "agent exchange/DEMO/DEMO-0001/out.txt").read_text() == "data"
    assert "out.txt" in mcp.sftp_ls("DEMO-0001")
    assert "Failed" in mcp.sftp_ls("/etc")


def test_doctor_sftp_check(respx_mock, server):
    from cli.client import Client, Config
    from cli.doctor import sftp_check

    client = Client(Config(SERVER, "tm_test"))
    _api(respx_mock, server)
    check = sftp_check(client)
    assert check.status == "ok" and "readable" in check.detail
    respx_mock.get(f"{API}/agents/me/sftp-credentials").mock(return_value=httpx.Response(200, json={
        "host": "127.0.0.1", "port": server.port, "host_key": server.fingerprint, "base_dir": BASE,
        "username": "bot", "password": "wrong"}))
    assert sftp_check(client).status == "error"
    respx_mock.get(f"{API}/agents/me/sftp").mock(return_value=httpx.Response(404, json={
        "error": {"code": "sftp_not_configured", "message": "no login"}}))
    assert sftp_check(client).status == "warn"
    respx_mock.get(f"{API}/agents/me/sftp").mock(return_value=httpx.Response(404, json={
        "error": {"code": "sftp_disabled", "message": "off"}}))
    assert sftp_check(client) is None


def test_task_md_note_only_with_sftp(respx_mock):
    from cli.client import Client, Config
    from cli.prepare import sftp_available, sftp_note

    client = Client(Config(SERVER, "tm_test"))
    route = respx_mock.get(f"{API}/agents/me/sftp").mock(return_value=httpx.Response(404, json={
        "error": {"code": "sftp_disabled", "message": "off"}}))
    assert not sftp_available(client)
    route.mock(return_value=httpx.Response(200, json={"host": "h"}))
    assert sftp_available(client)
    text = sftp_note({"task_nr": "DEMO-0001"}, "/usr/bin/tbr")
    assert "sftp put DEMO-0001" in text and "only when the task asks for it" in text


# --- Server with a test DB ------------------------------------------------------------------------

def _enable(server, host_key=True):
    from app.db import db
    from app.services import settings_store

    with db.session() as s:
        settings_store.update(s, {"sftp_enabled": "true", "sftp_host": "127.0.0.1", "sftp_port": str(server.port),
                                  **({"sftp_host_key": server.fingerprint} if host_key else {})}, None)
        s.commit()


@pytest.fixture
def senv(env, server):  # noqa: F811
    from tests.test_workflow import mkproject

    _enable(server)
    r = env["c"].put("/api/v1/admin/actors/claude-code/sftp", headers=env["admin"],
                     json={"sftp_username": "bot", "sftp_password": PW})
    assert r.status_code == 200 and r.json()["sftp_password_last4"] == "1234", r.text
    mkproject(env)
    return env


from tests.test_workflow import env, gitea  # noqa: E402,F401 (fixtures)
from tests.test_ui_cli import tm_as, ui  # noqa: E402,F401 (fixtures)


def senv_port(c, e) -> int:
    return c.get("/api/v1/agents/me/sftp", headers=e["claude"]).json()["port"]


@pytest.mark.db
def test_credentials_encrypted_and_only_for_secure_peers(senv, caplog):
    from fastapi.testclient import TestClient
    from sqlalchemy import text

    from app.db import db
    from app.main import app

    with db.engine.connect() as conn:
        row = conn.execute(text("SELECT * FROM actors WHERE name='claude-code'")).mappings().one()
    assert row["sftp_username"] == "bot" and PW not in row["sftp_password_enc"]

    c = senv["c"]
    info = c.get("/api/v1/agents/me/sftp", headers=senv["claude"]).json()
    assert info["username"] == "bot" and "password" not in info and info["base_dir"] == BASE
    caplog.set_level(logging.INFO, logger="audit")
    creds = c.get("/api/v1/agents/me/sftp-credentials", headers=senv["claude"]).json()
    assert creds["password"] == PW and creds["username"] == "bot" and creds["port"] == senv_port(c, senv)
    fetched = [r for r in caplog.records if getattr(r, "audit_action", None) == "sftp_credentials_fetched"]
    assert fetched and fetched[-1].audit_actor == "claude-code"
    assert all(PW not in r.getMessage() and PW not in str(r.__dict__) for r in caplog.records)

    # Every agent only gets its own login: another agent without one gets 404.
    r = c.get("/api/v1/agents/me/sftp-credentials", headers=senv["codex"])
    assert r.status_code == 404 and r.json()["error"]["code"] == "sftp_not_configured"
    with TestClient(app, client=("192.168.1.50", 40000)) as remote:
        r = remote.get("/api/v1/agents/me/sftp-credentials", headers=senv["claude"])
        assert r.status_code == 403 and r.json()["error"]["code"] == "insecure_channel"
        r = remote.get("/api/v1/agents/me/sftp-credentials",
                       headers={**senv["claude"], "X-Forwarded-Proto": "https"})
        assert r.status_code == 403


@pytest.mark.db
def test_disabled_storage_and_settings_validation(senv):
    c, admin = senv["c"], senv["admin"]
    r = c.put("/api/v1/admin/settings", headers=admin, json={"sftp_enabled": "false"})
    assert r.status_code == 200
    r = c.get("/api/v1/agents/me/sftp", headers=senv["claude"])
    assert r.status_code == 404 and r.json()["error"]["code"] == "sftp_disabled"
    for key, value in (("sftp_port", "70000"), ("sftp_host_key", "MD5:aa"), ("sftp_base_dir", "relative"),
                       ("sftp_path_pattern", "/tmp/{task_nr}"), ("sftp_host", "a b")):
        r = c.put("/api/v1/admin/settings", headers=admin, json={key: value})
        assert r.status_code == 422, (key, r.text)
    r = c.put("/api/v1/admin/settings", headers=admin, json={"sftp_path_pattern": "{base}/results/{task_nr}/"})
    assert r.status_code == 200


@pytest.mark.db
def test_tbr_sftp_put_end_to_end_with_task_note(senv, tm_as, server, tmp_path, monkeypatch):  # noqa: F811
    from tests.test_workflow import claim, mktask

    nr = mktask(senv, assigned_to="claude-code")["task_nr"]
    assert claim(senv, task_nr=nr).status_code == 200
    monkeypatch.chdir(tmp_path)
    (tmp_path / "report.txt").write_text("result")
    r = tm_as(senv["claude"], "sftp", "put", nr, "report.txt")
    assert r.exit_code == 0, r.output
    stored = server.root / "agent exchange" / "KET" / nr / "report.txt"
    assert stored.read_text() == "result"
    comments = senv["c"].get(f"/api/v1/tasks/{nr}", headers=senv["admin"]).json()["comments"]
    note = [c for c in comments if c["type"] == "system" and "SFTP" in c["body"]][-1]
    assert f"/agent exchange/KET/{nr}/report.txt" in note["body"] and sftp.file_digest(tmp_path / "report.txt") in note["body"]
    assert note["author"] == "claude-code"

    r = tm_as(senv["claude"], "sftp", "ls", nr)
    assert r.exit_code == 0 and "report.txt" in r.output

    # Only the assignee notes uploads in a task; paths outside the base folder are refused.
    files = [{"path": f"{BASE}/KET/{nr}/x", "size": 1, "sha256": "0" * 64}]
    r = senv["c"].post(f"/api/v1/tasks/{nr}/sftp-uploads", headers=senv["codex"], json={"files": files})
    assert r.status_code == 403
    r = senv["c"].post(f"/api/v1/tasks/{nr}/sftp-uploads", headers=senv["claude"],
                       json={"files": [{**files[0], "path": "/etc/passwd"}]})
    assert r.status_code == 422


@pytest.mark.db
def test_admin_ui_login_test_access_and_host_key(env, ui, server):  # noqa: F811
    from tests.test_ui_cli import post

    _enable(server, host_key=False)
    r = post(ui, "/admin/users/hermes/sftp", sftp_username="bot", sftp_password=PW)
    assert r.status_code == 200 and "…1234" in r.text and PW not in r.text
    r = post(ui, "/admin/users/hermes/sftp/check")
    assert r.status_code == 422 and "zuerst bestätigen" in r.text  # No host key confirmed yet.

    r = ui.post("/admin/settings/test-sftp", headers={"X-CSRF-Token": ui.csrf})
    assert server.fingerprint in r.text and "Fingerabdruck übernehmen" in r.text
    r = post(ui, "/admin/settings/sftp-host-key", fingerprint="SHA256:" + "C" * 43)
    assert r.status_code == 422
    r = post(ui, "/admin/settings/sftp-host-key", fingerprint=server.fingerprint)
    assert r.status_code == 200 and "gespeichert" in r.text
    r = ui.post("/admin/settings/test-sftp", headers={"X-CSRF-Token": ui.csrf})
    assert "passt zum hinterlegten Fingerabdruck" in r.text

    r = post(ui, "/admin/users/hermes/sftp/check")
    assert r.status_code == 200 and "SFTP-Zugang funktioniert" in r.text
    assert list((server.root / "agent exchange").iterdir()) == []
    r = post(ui, "/admin/users/hermes/sftp/delete")
    assert r.status_code == 200 and "SFTP-Zugang entfernt" in r.text

    r = post(ui, "/admin/users/admin/sftp", sftp_username="x", sftp_password="y")
    assert r.status_code == 422  # Humans have no SFTP login.
