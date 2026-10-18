"""DZ 2: refuse spreadsheet/archive attachments in local_only, and report existing ones."""
from types import SimpleNamespace

import pytest

from app.services.attachments import attachment_class, is_blocked_attachment
from tests.test_workflow import env, gitea, mkproject, mktask  # noqa: F401
from tests.gitea_fake import SERVICE_USER  # noqa: F401

db = pytest.mark.db
BLOCKED = ("report.xlsx", "alt.XLS", "macro.xlsm", "table.csv", "cols.tsv", "sheet.ods", "pack.ZIP")


def test_blocked_names_and_effective_class():
    for name in ("a.xlsx", "B.XLS", "c.xlsm", "d.csv", "e.tsv", "f.ods", "g.ZIP", "dir/h.xlsx", "C:\\i.csv"):
        assert is_blocked_attachment(name), name
    assert not is_blocked_attachment("notes.txt")
    assert not is_blocked_attachment("report.xlsx.txt")
    cloud, local = SimpleNamespace(privacy="cloud_ok"), SimpleNamespace(privacy="local_only")
    assert attachment_class(SimpleNamespace(), cloud) == "cloud_ok"
    assert attachment_class(SimpleNamespace(data_class=None), local) == "local_only"
    assert attachment_class(SimpleNamespace(data_class="sealed"), cloud) == "sealed"
    assert attachment_class(SimpleNamespace(data_class="cloud_ok"), local) == "cloud_ok"


def _as_client(monkeypatch, token: str):
    from fastapi.testclient import TestClient

    from app.main import app
    from cli import client as client_mod

    monkeypatch.setattr(
        client_mod.httpx, "Client",
        lambda base_url, headers, **_: TestClient(app, base_url=base_url, headers=headers,
                                                  client=("127.0.0.1", 50000)))
    monkeypatch.setenv("TBR_URL", "http://testserver")
    monkeypatch.setenv("TBR_TOKEN", token)


def _upload(env, nr, name, body=b"x"):
    from tests.test_attachments import upload
    return upload(env, nr, files=[("files", (name, body, "application/octet-stream"))])


@db
def test_local_only_blocks_spreadsheets_on_every_entry(env, monkeypatch, tmp_path):
    """API, web UI, CLI and MCP refuse the blocked types; cloud_ok still accepts them."""
    from fastapi.testclient import TestClient
    from typer.testing import CliRunner

    from app.db import db as database
    from app.main import app
    from app.models import Actor, Task, TaskAttachment
    from cli import attachments as att
    from cli import tm
    from cli.client import Client, Config
    from cli.mcp_server import TaskBrigadeMCP
    from cli.tm_admin import app as admin_app
    from tests.conftest import login

    mkproject(env, privacy="local_only")
    nr = mktask(env)["task_nr"]
    for name in BLOCKED:
        r = _upload(env, nr, name)
        body = r.json()["error"]
        assert r.status_code == 422 and body["code"] == "use_dataset_upload", r.text
        assert "Datenablage" in body["message"]
    mixed = env["c"].post(
        f"/api/v1/tasks/{nr}/attachments", headers=env["admin"],
        files=[("files", ("both.xlsx", b"x", "application/octet-stream")),
               ("files", ("ok.txt", b"hi", "text/plain"))])
    assert mixed.status_code == 422 and mixed.json()["error"]["code"] == "use_dataset_upload"
    assert env["c"].get(f"/api/v1/tasks/{nr}", headers=env["admin"]).json()["attachments"] == []
    names = env["gitea"].repo(SERVICE_USER, "ket").files
    assert not any(path.endswith((".xlsx", ".txt")) for path in names)
    assert _upload(env, nr, "notes.txt", b"hi").status_code == 201

    mkproject(env, key="OKP", privacy="cloud_ok")
    other = mktask(env, project="OKP")["task_nr"]
    ok = _upload(env, other, "ok.xlsx", b"PK")
    assert ok.status_code == 201 and ok.json()[0]["filename"] == "ok.xlsx"

    with TestClient(app) as ui:
        login(ui)
        page = ui.get(f"/tasks/{nr}")
        assert "Datenablage" in page.text
        csrf = page.text.split('name="csrf_token" value="')[1].split('"')[0]
        refused = ui.post(f"/tasks/{nr}/attachments", data={"csrf_token": csrf},
                          files=[("attachments", ("ui.csv", b"a,b", "text/csv"))], follow_redirects=True)
        assert "Anhänge nicht gespeichert" in refused.text and "Datenablage" in refused.text
        created = ui.post(
            "/tasks/new",
            data={"csrf_token": csrf, "project": "KET", "title": "Mit Tabelle", "prio": "3", "assigned_to": "",
                  "status": "defined", "description": "x", "acceptance_criteria": "- x", "due_date": "",
                  "parent_task_nr": "", "depends_on": ""},
            files=[("attachments", ("neu.xlsx", b"PK", "application/octet-stream"))], follow_redirects=True)
        assert created.status_code == 200 and "Datenablage" in created.text
        new_nr = created.url.path.rsplit("/", 1)[1]
    stored = env["c"].get(f"/api/v1/tasks/{new_nr}", headers=env["admin"]).json()["attachments"]
    assert stored == []
    still = env["c"].get(f"/api/v1/tasks/{nr}", headers=env["admin"]).json()["attachments"]
    assert [a["filename"] for a in still] == ["notes.txt"]

    _as_client(monkeypatch, env["admin"]["Authorization"].split()[1])
    sheet = tmp_path / "cli.ods"
    sheet.write_bytes(b"ods")
    cli = CliRunner().invoke(tm.app, ["attach", nr, str(sheet)])
    assert cli.exit_code == 1 and "use_dataset_upload" in (cli.stdout + cli.stderr)
    work = tmp_path / "work"
    work.mkdir()
    (work / "chat.zip").write_bytes(b"PK")
    token = env["admin"]["Authorization"].split()[1]
    mcp = TaskBrigadeMCP(Client(Config("http://testserver", token)), work, attach_policy=att.policy(work, {}))
    assert "use_dataset_upload" in mcp.attach_files(nr, [str(work / "chat.zip")])
    assert "use_dataset_upload" in mcp.attach_text(nr, "pasted.csv", "a,b\n1,2\n")
    allowed = mcp.attach_text(nr, "readme.md", "# ok\n")
    assert "use_dataset_upload" not in allowed and "Attached" in allowed

    with database.session() as s:
        task = s.query(Task).filter_by(task_nr=nr).one()
        admin = s.query(Actor).filter_by(name="admin").one()
        s.add(TaskAttachment(task_id=task.id, filename="legacy.xlsx", repo_path=f"tasks/{nr}/legacy.xlsx",
                             size_bytes=3, commit_sha="c" * 40, uploaded_by=admin.id))
        s.commit()
        before = s.query(TaskAttachment).count()
    report = CliRunner().invoke(admin_app, ["data", "scan-attachments"])
    assert report.exit_code == 0, report.stdout + report.stderr
    assert f"KET {nr} legacy.xlsx tasks/{nr}/legacy.xlsx {SERVICE_USER}/ket" in report.stdout
    assert "notes.txt" not in report.stdout and "ok.xlsx" not in report.stdout
    assert "nothing deleted" in report.stdout
    with database.session() as s:
        assert s.query(TaskAttachment).count() == before
