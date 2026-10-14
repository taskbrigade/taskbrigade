"""Attachments: filenames, API/UI uploads, repo storage, permissions, limits (test DB and fake Gitea)."""
import pytest

from app.services.attachments import _unique, safe_filename


@pytest.mark.parametrize("raw,expected", [
    ("Entwurf.pdf", "Entwurf.pdf"),
    ("../../etc/passwd", "passwd"),
    ("C:\\Users\\alex\\Grüße & Pläne.docx", "Gruesse-Plaene.docx"),
    ("  .env", "env"),
    ("", "datei"),
    ("bild (1).PNG", "bild-1.PNG"),
    ("a" * 300 + ".txt", "a" * 120 + ".txt"),
    ("ohne_endung", "ohne_endung"),
])
def test_safe_filename(raw, expected):
    assert safe_filename(raw) == expected


def test_unique():
    assert _unique("a.pdf", {"a.pdf", "a-2.pdf"}) == "a-3.pdf"
    assert _unique("readme", {"readme"}) == "readme-2"


db = pytest.mark.db
from tests.test_workflow import env, gitea, mkproject, mktask  # noqa: E402,F401
from tests.gitea_fake import SERVICE_USER  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4  # Binary, not UTF-8.


def upload(env, nr, who="admin", files=None):
    files = files or [("files", ("Skizze.png", PNG, "image/png")), ("files", ("notiz.txt", b"Hallo", "text/plain"))]
    return env["c"].post(f"/api/v1/tasks/{nr}/attachments", headers=env[who], files=files)


@db
def test_upload_lands_in_repo_and_payload(env):
    mkproject(env)
    nr = mktask(env, assigned_to="claude-code")["task_nr"]
    r = upload(env, nr)
    assert r.status_code == 201, r.text
    assert [a["path"] for a in r.json()] == [f"tasks/{nr}/Skizze.png", f"tasks/{nr}/notiz.txt"]
    repo = env["gitea"].repo(SERVICE_USER, "ket")
    assert repo.files[f"tasks/{nr}/Skizze.png"] == PNG and repo.files[f"tasks/{nr}/notiz.txt"] == "Hallo"
    t = env["c"].get(f"/api/v1/tasks/{nr}", headers=env["claude"]).json()
    a = t["attachments"][0]
    assert a["size_bytes"] == len(PNG) and a["uploaded_by"] == "admin" and len(a["commit_sha"]) == 40
    assert a["url"].endswith(f"/src/branch/main/tasks/{nr}/Skizze.png") and "/raw/branch/main/" in a["raw_url"]
    assert t["events"][-1]["type"] == "attachment_added"
    # Same name again: add a suffix instead of overwriting.
    r = upload(env, nr, files=[("files", ("notiz.txt", b"zweite", "text/plain"))])
    assert [x["filename"] for x in r.json()] == ["Skizze.png", "notiz.txt", "notiz-2.txt"]


@db
def test_existing_repo_file_is_not_overwritten(env):
    mkproject(env)
    nr = mktask(env)["task_nr"]
    env["gitea"].repo(SERVICE_USER, "ket").files[f"tasks/{nr}/plan.md"] = "vom Agenten"
    r = upload(env, nr, files=[("files", ("plan.md", b"neu", "text/markdown"))])
    assert r.json()[0]["filename"] == "plan-2.md"
    assert env["gitea"].repo(SERVICE_USER, "ket").files[f"tasks/{nr}/plan.md"] == "vom Agenten"


@db
def test_attachment_permissions_and_limits(env, monkeypatch):
    mkproject(env)
    nr = mktask(env, assigned_to="claude-code")["task_nr"]
    assert upload(env, nr, who="hermes").status_code == 403       # Neither creator nor assignee.
    assert upload(env, nr, who="claude").status_code == 201       # Assigned agent.
    own = mktask(env, who="hermes")["task_nr"]
    assert upload(env, own, who="hermes").status_code == 201      # Creator.
    idea = mkproject(env, key="IDEE", mode="none", status="idea")
    t = env["c"].post("/api/v1/tasks", headers=env["admin"], json={"project": "IDEE", "title": "x",
                                                                    "status": "draft"}).json()
    r = upload(env, t["task_nr"])
    assert r.status_code == 422 and r.json()["error"]["code"] == "no_repo"
    assert idea["gitea_repo"] is None
    monkeypatch.setattr("app.services.attachments.MAX_BYTES", 10)
    r = upload(env, nr, files=[("files", ("gross.bin", b"x" * 11, "application/octet-stream"))])
    assert r.status_code == 422 and r.json()["error"]["code"] == "attachment_too_large"


@db
def test_ui_create_with_attachments_and_task_md(env, monkeypatch):
    from fastapi.testclient import TestClient
    from typer.testing import CliRunner

    from app.main import app
    from tests.conftest import login
    from tests.test_ui_cli import tm_as  # noqa: F401

    mkproject(env)
    with TestClient(app) as ui:
        login(ui)
        csrf = ui.get("/tasks/new").text.split('name="csrf_token" value="')[1].split('"')[0]
        r = ui.post("/tasks/new", data={"csrf_token": csrf, "project": "KET", "title": "Mit Anhang", "prio": "3",
                                        "assigned_to": "claude-code", "status": "defined", "description": "Mockup prüfen",
                                        "acceptance_criteria": "- passt", "due_date": "", "parent_task_nr": "",
                                        "depends_on": ""},
                    files=[("attachments", ("Mockup.png", PNG, "image/png"))], follow_redirects=True)
        assert r.status_code == 200 and "im Repo abgelegt" in r.text and "Mockup.png" in r.text
        nr = r.url.path.rsplit("/", 1)[1]
        r = ui.post(f"/tasks/{nr}/attachments", data={"csrf_token": csrf},
                    files=[("attachments", ("Nachtrag.txt", b"spaeter", "text/plain"))], follow_redirects=True)
        assert "Nachtrag.txt" in r.text
        r = ui.post(f"/tasks/{nr}/attachments", files=[("attachments", ("x.txt", b"x", "text/plain"))])
        assert r.status_code == 403  # Without CSRF token.

    from cli import client as client_mod
    from cli import tm
    monkeypatch.setattr(client_mod.httpx, "Client",
                        lambda base_url, headers, **_: TestClient(app, base_url=base_url, headers=headers,
                                                                  client=("127.0.0.1", 50000)))
    monkeypatch.setenv("TBR_URL", "http://testserver")
    monkeypatch.setenv("TBR_TOKEN", env["claude"]["Authorization"].split()[1])
    out = CliRunner().invoke(tm.app, ["show", nr]).stdout
    assert "## Attachments" in out and f"`tasks/{nr}/Mockup.png`" in out and f"tasks/{nr}/Nachtrag.txt" in out
