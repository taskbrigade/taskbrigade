"""MCP attachments (cli/attachments.py, attach_files/attach_text/create_task(attachments=…)): allowed roots,
refusals with a reason, upload through the existing API."""
import os

import pytest

from cli import attachments as att
from cli.client import ApiError
from cli.mcp_server import TaskBrigadeMCP
from tests.test_tm_prepare import Resp


class AttachClient:
    """Fake API: records uploads (name → bytes); `fail` makes the upload return an API error."""

    def __init__(self, fail: ApiError | None = None, deny_create: bool = False):
        self.fail, self.deny_create = fail, deny_create
        self.calls, self.uploads = [], []

    def request(self, method, path, **kw):
        self.calls.append((method, path))
        if path == "/tasks":
            if self.deny_create:
                raise ApiError(403, "forbidden", "no")
            return Resp(201, {"task_nr": "KET-0070", "title": kw["json"]["title"], "status": "defined",
                              "assigned_to": "codex"})
        if path.endswith("/attachments"):
            if self.fail:
                raise self.fail
            files = []
            for field, (name, content) in kw["files"]:
                assert field == "files"
                files.append((name, content if isinstance(content, bytes) else content.read()))
            self.uploads.append((path, files))
            nr = path.split("/")[2]
            return Resp(201, [{"path": f"tasks/{nr}/{n}", "size_bytes": len(c)} for n, c in files])
        raise AssertionError(path)


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    for d in ("Downloads", "Documents", "Desktop", ".ssh", "Private"):
        (h / d).mkdir(parents=True)
    (h / "Downloads" / "angebot.pdf").write_bytes(b"%PDF-1.4 x")
    (h / ".ssh" / "id_ed25519").write_text("KEY")
    (h / "Private" / "geheim.txt").write_text("secret")
    return h


def app_for(tmp_path, home, client=None, settings=None):
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    pol = att.policy(work, settings or {}, home=home)
    return TaskBrigadeMCP(client or AttachClient(), work, attach_policy=pol), work


def test_attach_files_uploads_allowed_files(tmp_path, home):
    fc = AttachClient()
    app, work = app_for(tmp_path, home, fc)
    (work / "notes.md").write_text("# Notizen\n")
    out = app.attach_files("ket-0070", [str(home / "Downloads" / "angebot.pdf"), "notes.md"])
    assert fc.uploads == [("/tasks/KET-0070/attachments",
                           [("angebot.pdf", b"%PDF-1.4 x"), ("notes.md", b"# Notizen\n")])]
    assert "Attached to KET-0070" in out and "tasks/KET-0070/angebot.pdf  (10 bytes)" in out
    assert "Refused" not in out


@pytest.mark.parametrize("make, reason", [
    (lambda h, w: h / "Private" / "geheim.txt", "outside the allowed folders"),
    (lambda h, w: h / ".ssh" / "id_ed25519", "outside the allowed folders"),
    (lambda h, w: h / "Downloads" / "missing.pdf", "file not found"),
])
def test_refused_outside_roots(tmp_path, home, make, reason):
    fc = AttachClient()
    app, work = app_for(tmp_path, home, fc)
    out = app.attach_files("KET-0070", [str(make(home, work))])
    assert reason in out and "Refused" in out and not fc.uploads


def test_refused_hidden_secret_folder_and_symlink(tmp_path, home):
    fc = AttachClient()
    app, work = app_for(tmp_path, home, fc)
    dl = home / "Downloads"
    (dl / ".ssh").mkdir()
    (dl / ".ssh" / "config").write_text("x")
    (dl / ".env").write_text("TOKEN=1")
    (dl / "id_ed25519").write_text("KEY")
    (dl / "server.PEM").write_text("KEY")
    (dl / "credentials.json").write_text("{}")
    (dl / "ordner").mkdir()
    (dl / "link.txt").symlink_to(home / "Private" / "geheim.txt")  # Symlink out of the roots.
    (dl / "harmlos.txt").symlink_to(home / ".ssh" / "id_ed25519")
    (dl / "id_rsa").symlink_to(dl / "angebot.pdf")  # Link name that looks like a key.
    (dl / "leer.txt").write_bytes(b"")
    cases = {".ssh/config": "hidden", ".env": "hidden", "id_ed25519": "key or secret", "server.PEM": "key or secret",
             "credentials.json": "key or secret", "ordner": "is a folder", "link.txt": "outside the allowed",
             "harmlos.txt": "outside the allowed", "id_rsa": "key or secret", "leer.txt": "empty"}
    for name, reason in cases.items():
        out = app.attach_files("KET-0070", [str(dl / name)])
        assert reason in out, (name, out)
    assert not fc.uploads


def test_refused_device_too_large_and_too_many(tmp_path, home):
    fc = AttachClient()
    app, work = app_for(tmp_path, home, fc, settings={"max_attachment_mb": 0.5})
    big = home / "Downloads" / "gross.bin"
    big.write_bytes(b"x" * 600_000)
    assert "larger than 0.5 MB" in app.attach_files("KET-0070", [str(big)])
    fifo = home / "Downloads" / "pipe"
    os.mkfifo(fifo)
    assert "not a regular file" in app.attach_files("KET-0070", [str(fifo)])
    out = app.attach_files("KET-0070", [str(home / "Downloads" / "angebot.pdf")] * 21)
    assert "21 files given – at most 20" in out
    assert not fc.uploads
    assert "No files given" in app.attach_files("KET-0070", [])


def test_mixed_uploads_good_files_and_reports_refused(tmp_path, home):
    fc = AttachClient()
    app, _ = app_for(tmp_path, home, fc)
    out = app.attach_files("KET-0070", [str(home / "Downloads" / "angebot.pdf"), str(home / ".ssh" / "id_ed25519")])
    assert [n for n, _ in fc.uploads[0][1]] == ["angebot.pdf"]
    assert "Attached to" in out and "Refused" in out and "id_ed25519" in out


def test_upload_error_is_reported(tmp_path, home):
    fc = AttachClient(fail=ApiError(422, "no_repo", "Attachments require a Gitea repository"))
    app, _ = app_for(tmp_path, home, fc)
    out = app.attach_files("KET-0070", [str(home / "Downloads" / "angebot.pdf")])
    assert "Upload failed" in out and "Gitea repository" in out


def test_create_task_with_attachments(tmp_path, home):
    fc = AttachClient()
    app, _ = app_for(tmp_path, home, fc)
    out = app.create_task("ket", "Angebot prüfen", "D", "A", "codex",
                          attachments=[str(home / "Downloads" / "angebot.pdf")])
    assert fc.calls == [("POST", "/tasks"), ("POST", "/tasks/KET-0070/attachments")]
    assert "KET-0070 created" in out and "tasks/KET-0070/angebot.pdf" in out and "exists anyway" not in out


def test_create_task_keeps_task_when_attaching_fails(tmp_path, home):
    fc = AttachClient(fail=ApiError(403, "forbidden", "Attachments: only admins"))
    app, _ = app_for(tmp_path, home, fc)
    out = app.create_task("ket", "T", "D", "A", "codex", attachments=[str(home / "Downloads" / "angebot.pdf")])
    assert "KET-0070 created" in out and "Upload failed" in out and "exists anyway" in out

    fc = AttachClient()
    app, _ = app_for(tmp_path, home, fc)
    out = app.create_task("ket", "T", "D", "A", "codex", attachments=[str(home / ".ssh" / "id_ed25519")])
    assert "KET-0070 created" in out and "Refused" in out and "exists anyway" in out and not fc.uploads

    fc = AttachClient(deny_create=True)
    app, _ = app_for(tmp_path, home, fc)
    assert "Task not created" in app.create_task("ket", "T", attachments=[str(home / "Downloads" / "angebot.pdf")])
    assert fc.calls == [("POST", "/tasks")]


def test_attach_text(tmp_path, home):
    fc = AttachClient()
    app, _ = app_for(tmp_path, home, fc)
    out = app.attach_text("ket-0070", "../../etc/Notiz ä.md", "# Hallo ä\n")
    assert fc.uploads == [("/tasks/KET-0070/attachments", [("Notiz ä.md", "# Hallo ä\n".encode())])]
    assert "Attached to KET-0070" in out
    app.attach_text("KET-0070", "..\\.hidden.csv", "a,b\n")
    assert fc.uploads[-1][1][0][0] == "hidden.csv"
    assert "filename is empty" in app.attach_text("KET-0070", "../..", "x")
    assert "content is empty" in app.attach_text("KET-0070", "a.md", "")
    assert "larger than 1 MB" in app.attach_text("KET-0070", "a.md", "x" * (att.MAX_TEXT_BYTES + 1))
    assert len(fc.uploads) == 2


def test_configured_attach_roots(tmp_path, home):
    work = tmp_path / "work"
    work.mkdir()
    shared = tmp_path / "Freigabe"
    shared.mkdir()
    (shared / "plan.md").write_text("plan")
    (work / ".taskbrigade.toml").write_text(f'url = "http://x"\ntoken = "t"\n\n[mcp]\nattach_roots = ["{shared}"]\n'
                                            'max_attachment_mb = 5\n')
    fc = AttachClient()
    app = TaskBrigadeMCP(fc, work)  # Reads [mcp] from the work directory's configuration.
    assert app.attach_policy.roots == [shared.resolve()] and app.attach_policy.max_bytes == 5 * 1024 * 1024
    assert "Attached to" in app.attach_files("KET-0070", [str(shared / "plan.md")])
    (work / "x.md").write_text("x")  # The list replaces the defaults – the work directory is not included.
    assert "outside the allowed folders" in app.attach_files("KET-0070", [str(work / "x.md")])


def test_default_roots(tmp_path, home):
    work = tmp_path / "work"
    work.mkdir()
    pol = att.policy(work, {}, home=home)
    assert pol.roots == [work.resolve(), *[(home / d).resolve() for d in ("Downloads", "Documents", "Desktop")]]
    wt = work / "KET" / ".worktrees" / "KET-0070"  # Task working folders stay allowed.
    wt.mkdir(parents=True)
    (wt / "result.csv").write_text("a\n")
    assert att.check_path(str(wt / "result.csv"), pol) == (wt / "result.csv").resolve()
    (wt / ".git").mkdir()
    (wt / ".git" / "config").write_text("x")
    with pytest.raises(att.AttachError, match="hidden"):
        att.check_path(str(wt / ".git" / "config"), pol)
