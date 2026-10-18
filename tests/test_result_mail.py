"""Work results by email (`app/services/result_mail.py`): send requests over API, CLI and MCP, recipient list,
limits and data classes, approval in task detail and inbox, `on_approve`, delivery with attachments from Gitea and
the 30-day clean-up. SMTP is `tests/smtp_fake.FakeSmtp`, Gitea `tests/gitea_fake.FakeGitea`; no real mail."""
import hashlib
import json
import logging
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db import db
from app.models import Actor, OutgoingMail, Task, TaskAttachment
from app.services import result_mail, settings_store
from app.services.actors import utcnow
from cli import client as client_mod
from cli.mcp_server import TaskBrigadeMCP
from cli.prepare import mail_note
from tests.gitea_fake import SERVICE_USER
from tests.test_email import PASSWORD, menv, smtp, verified  # noqa: F401 (fixtures)
from tests.test_ui_cli import post, tm_as, ui  # noqa: F401 (fixtures)
from tests.test_workflow import SHA, claim, env, gitea, mkproject, mktask  # noqa: F401 (fixtures)

CLIENT = "client@example.org"
PDF = b"%PDF-1.4 result " + bytes(range(256))


def _setting(**values) -> None:
    with db.session() as s:
        settings_store.update(s, {k: str(v) for k, v in values.items()}, None)
        s.commit()


def _recipient(address=CLIENT, mode="freigabe", project="KET") -> None:
    with db.session() as s:
        from app.services import projects

        result_mail.add_recipient(s, s.scalar(select(Actor).where(Actor.name == "admin")),
                                  projects.get(s, project), address, "Customer", mode)
        s.commit()


def _task(e, branch_files: dict | None = None) -> tuple[str, str]:
    """Task in KET assigned to claude-code and claimed; optional files on its pushed branch."""
    nr = mktask(e, assigned_to="claude-code")["task_nr"]
    branch = claim(e).json()["branch_name"]
    e["gitea"].push_branch(SERVICE_USER, "ket", branch)
    for path, content in (branch_files or {}).items():
        e["gitea"].put_branch_file(SERVICE_USER, "ket", branch, path, content)
    return nr, branch


def _send(e, nr, who="claude", **body):
    payload = {"to": [CLIENT], "subject": "Result KET", "body": "Hello,\n\nthe **report** is attached.", **body}
    return e["c"].post(f"/api/v1/tasks/{nr}/emails", headers=e[who], json=payload)


def _row(mail_id: int) -> OutgoingMail:
    with db.session() as s:
        row = s.get(OutgoingMail, mail_id)
        s.expunge(row)
        return row


def _deliver(now=None) -> dict:
    with db.session() as s:
        counts = result_mail.deliver(s, now)
        s.commit()
    return counts


def _comments(e, nr) -> list[str]:
    return [c["body"] for c in e["c"].get(f"/api/v1/tasks/{nr}", headers=e["admin"]).json()["comments"]
            if c["type"] == "system"]


def _code(r) -> str:
    return r.json()["error"]["code"]


# --- Request, approval in the task, delivery ------------------------------------------------------

def test_request_approve_in_task_and_send_with_attachments(menv, ui, caplog):  # noqa: F811
    e = menv
    caplog.set_level(logging.INFO, logger="admin_audit")
    r = post(ui, "/projects/KET/mail-recipients", address=CLIENT, label="Customer", mode="freigabe")
    assert r.status_code == 200 and CLIENT in r.text
    nr, branch = _task(e, {"out/result.pdf": PDF})
    r = e["c"].post(f"/api/v1/tasks/{nr}/attachments", headers=e["admin"],
                    files=[("files", ("brief.md", b"# Brief\n", "text/markdown"))])
    assert r.status_code == 201, r.text
    with db.session() as s:
        att_id = s.scalar(select(TaskAttachment.id).join(Task).where(Task.task_nr == nr))
    assert e["c"].get(f"/api/v1/tasks/{nr}", headers=e["claude"]).json()["mail_recipients"] == 1

    r = _send(e, nr, attachments=[f"attachment:{att_id}", "branch:out/result.pdf"], cc=["Alex.Doe@example.org"])
    assert r.status_code == 422 and _code(r) == "mail_recipient_not_allowed"  # cc is checked too
    r = _send(e, nr, attachments=[f"attachment:{att_id}", "branch:out/result.pdf"])
    assert r.status_code == 201, r.text
    m = r.json()
    assert m["status"] == "pending_approval" and m["needs_approval"] and m["reply_to"] is None
    assert [(a["name"], a["size"], a["sha256"]) for a in m["attachments"]] == [
        ("brief.md", 8, hashlib.sha256(b"# Brief\n").hexdigest()),
        ("result.pdf", len(PDF), hashlib.sha256(PDF).hexdigest())]
    assert any(f"#{m['id']}" in c and CLIENT in c for c in _comments(e, nr))
    assert _deliver()["sent"] == 0 and not e["smtp"].messages  # Waits for a human

    page = ui.get(f"/tasks/{nr}").text
    assert f'id="mail-{m["id"]}"' in page and "Result KET" in page and hashlib.sha256(PDF).hexdigest()[:16] in page
    assert f'/tasks/{nr}/emails/{m["id"]}/approve' in page
    r = post(ui, f"/tasks/{nr}/emails/{m['id']}/edit", to=CLIENT, cc="", subject="Result KET (final)",
             body="Hello,\n\nfinal report attached.")
    assert r.status_code == 200 and "Result KET (final)" in r.text
    verified("admin", "admin@example.org")
    r = post(ui, f"/tasks/{nr}/emails/{m['id']}/approve")
    assert r.status_code == 200
    row = _row(m["id"])
    assert row.status == "queued" and row.reply_to == "admin@example.org" and row.approved_by is not None

    assert _deliver()["sent"] == 1
    sender, rcpts, _raw = e["smtp"].messages[0]
    msg = e["smtp"].parsed()
    assert sender == "taskbrigade@example.org" and rcpts == [CLIENT]
    assert msg["Reply-To"] == "admin@example.org" and msg["Subject"] == "Result KET (final)"
    assert msg["To"] == CLIENT and msg["Auto-Submitted"] == "auto-generated"
    files = {p.get_filename(): p.get_content() for p in msg.iter_attachments()}
    assert files == {"brief.md": "# Brief\n", "result.pdf": PDF}
    assert "final report attached" in msg.get_body(("plain",)).get_content()
    row = _row(m["id"])
    assert row.status == "sent" and row.message_id == msg["Message-ID"] and row.sent_at
    assert any("Message-ID" in c and f"#{m['id']}" in c for c in _comments(e, nr))

    actions = [r.getMessage() for r in caplog.records if r.name == "admin_audit"]
    assert {"mail_requested", "mail_edited", "mail_approved", "mail_sent"} <= set(actions)
    for record in caplog.records:  # Audit never carries the body
        assert "final report attached" not in json.dumps(record.__dict__, default=str)
    sent = [r for r in caplog.records if r.getMessage() == "mail_sent"][0]
    assert sent.recipients == [CLIENT] and hashlib.sha256(PDF).hexdigest() in sent.attachment_hashes


def test_agents_never_see_smtp_credentials(menv):  # noqa: F811
    e = menv
    _recipient()
    nr, _branch = _task(e)
    assert _send(e, nr).status_code == 201
    for url in (f"/api/v1/tasks/{nr}", f"/api/v1/tasks/{nr}/emails", "/api/v1/projects/KET/mail-recipients"):
        r = e["c"].get(url, headers=e["claude"])
        assert r.status_code == 200 and PASSWORD not in r.text and "smtp_password" not in r.text
    assert e["c"].get("/api/v1/admin/settings", headers=e["claude"]).status_code == 403


# --- Refusals -------------------------------------------------------------------------------------

def test_refusals_recipients_class_types_size_limit_and_rights(menv):  # noqa: F811
    e = menv
    c = e["c"]
    _recipient()
    nr, _branch = _task(e, {"out/tool.exe": b"MZ", "out/big.pdf": b"x" * (1024 * 1024 + 10), "out/a.zip": b"PK"})
    assert _code(_send(e, nr, to=["stranger@example.org"])) == "mail_recipient_not_allowed"
    assert _code(_send(e, nr, to=["not an address"])) == "invalid_address"
    assert _code(_send(e, nr, attachments=["branch:out/tool.exe"])) == "mail_attachment_type"
    assert _code(_send(e, nr, attachments=["branch:out/missing.pdf"])) == "mail_attachment_missing"
    assert _code(_send(e, nr, attachments=["branch:../etc/passwd.pdf"])) == "mail_attachment_invalid"
    assert _code(_send(e, nr, attachments=["file:/etc/passwd"])) == "mail_attachment_invalid"
    assert _code(_send(e, nr, attachments=["merge:out/big.pdf"])) == "mail_attachment_missing"  # Not merged yet
    assert _code(_send(e, nr, attachments=["attachment:999999"])) == "mail_attachment_missing"
    _setting(mail_max_mb=1)
    assert _code(_send(e, nr, attachments=["branch:out/big.pdf"])) == "mail_too_large"
    r = _send(e, nr, who="codex")  # Not the assignee
    assert r.status_code == 403 and _code(r) == "mail_forbidden"

    # Daily limit per project (requests of the last 24 hours, refusals above do not count).
    _setting(mail_daily_limit_per_project=2)
    assert _send(e, nr).status_code == 201
    assert _send(e, nr, attachments=["branch:out/a.zip"]).status_code == 201
    r = _send(e, nr)
    assert r.status_code == 422 and _code(r) == "mail_daily_limit"

    # Data classes: local_only refused for now, sealed always.
    with db.session() as s:
        s.scalar(select(Task).where(Task.task_nr == nr)).data_class = "sealed"
        s.commit()
    _setting(mail_daily_limit_per_project=20)
    assert _code(_send(e, nr)) == "mail_sealed"
    other = mktask(e, project="OWN", title="Lokal")["task_nr"]
    r = c.post(f"/api/v1/tasks/{other}/emails", headers=e["admin"],
               json={"to": [CLIENT], "subject": "x", "body": "y"})
    assert r.status_code == 422 and _code(r) == "mail_local_only"

    with db.session() as s:
        s.scalar(select(Task).where(Task.task_nr == nr)).data_class = None
        s.commit()
    _setting(email_enabled="false")
    assert _code(_send(e, nr)) == "mail_disabled"


def test_internal_humans_without_approval_and_zip_needs_approval(menv):  # noqa: F811
    e = menv
    verified("alex", "alex@example.org")
    verified("admin", "owner@example.org")
    nr, _branch = _task(e, {"out/a.zip": b"PK\x03\x04", "out/r.png": b"\x89PNG"})
    r = _send(e, nr, to=["Alex@example.org"], attachments=["branch:out/r.png"])
    assert r.status_code == 201 and r.json()["status"] == "queued" and not r.json()["needs_approval"]
    assert r.json()["reply_to"] == "owner@example.org"  # Not approved by anyone: the project owner
    r = _send(e, nr, to=["alex@example.org"], attachments=["branch:out/a.zip"])
    assert r.json()["status"] == "pending_approval"  # zip only after approval
    _recipient("partner@example.org", "ohne_freigabe")
    assert _send(e, nr, to=["partner@example.org"]).json()["status"] == "queued"
    _recipient("alex@example.org", "freigabe")  # An entry on the list wins over the account default
    assert _send(e, nr, to=["alex@example.org"]).json()["status"] == "pending_approval"
    assert _deliver()["sent"] == 2
    assert [m[1] for m in e["smtp"].messages] == [["Alex@example.org"], ["partner@example.org"]]


# --- on_approve, reject, cancel ---------------------------------------------------------------------

def test_on_approve_sends_with_the_task_approval(menv, ui):  # noqa: F811
    e = menv
    c, g = e["c"], e["gitea"]
    _recipient()
    verified("admin", "admin@example.org")
    nr, branch = _task(e, {"out/result.pdf": PDF})
    r = _send(e, nr, attachments=["branch:out/result.pdf"], on_approve=True)
    m = r.json()
    assert m["status"] == "pending_approval" and m["on_approve"]
    r = c.post(f"/api/v1/tasks/{nr}/submit", headers=e["claude"],
               json={"branch_name": branch, "commit_sha": SHA, "summary": "Report fertig"})
    assert r.status_code == 200, r.text
    assert "mail-on-approve" in ui.get(f"/tasks/{nr}").text  # Review view shows the request
    inbox = ui.get("/").text
    assert f'id="m-{m["id"]}"' in inbox and "mail-on-approve" in inbox

    # After the merge the branch may be gone: the file is read from the merge commit (same hash).
    repo = g.repo(SERVICE_USER, "ket")
    g.commit_files["c" * 40] = {"out/result.pdf": PDF}
    r = c.post(f"/api/v1/tasks/{nr}/review", headers=e["admin"], json={"decision": "approve"})
    assert r.status_code == 200 and r.json()["status"] == "done", r.text
    row = _row(m["id"])
    assert row.status == "queued" and row.reply_to == "admin@example.org"
    repo.branch_files.pop(branch, None)
    assert _deliver()["sent"] == 1
    assert {p.get_filename(): p.get_content() for p in e["smtp"].parsed().iter_attachments()} == {"result.pdf": PDF}


def test_agent_approval_leaves_on_approve_mail_for_a_human(menv):  # noqa: F811
    e = menv
    _recipient()
    nr, _branch = _task(e)
    m = _send(e, nr, on_approve=True).json()
    with db.session() as s:
        t = s.scalar(select(Task).where(Task.task_nr == nr))
        agent = s.scalar(select(Actor).where(Actor.name == "codex"))
        result_mail.on_task_status(s, t, agent, "approve")
        s.commit()
    assert _row(m["id"]).status == "pending_approval"
    assert any(f"#{m['id']}" in c for c in _comments(e, nr)[-1:])


def test_reject_in_inbox_and_cancel_rejects_pending(menv, ui):  # noqa: F811
    e = menv
    _recipient()
    nr, _branch = _task(e)
    first, second = _send(e, nr).json(), _send(e, nr, subject="Second").json()
    page = ui.get("/").text
    assert f'hx-post="/inbox/emails/{first["id"]}/reject"' in page and "Second" in page
    r = ui.post(f"/inbox/emails/{first['id']}/reject", data={"reason": "Not yet"},
                headers={"X-CSRF-Token": ui.csrf})
    assert r.status_code == 200 and f"#{first['id']}" in r.text
    row = _row(first["id"])
    assert row.status == "rejected" and row.decided_note == "Not yet"
    r = ui.post(f"/inbox/emails/{first['id']}/approve", headers={"X-CSRF-Token": ui.csrf})
    assert r.status_code == 422  # Already decided
    r = e["c"].post(f"/api/v1/tasks/{nr}/transition", headers=e["admin"], json={"to": "cancelled"})
    assert r.status_code == 200, r.text
    assert _row(second["id"]).status == "rejected"
    assert e["c"].get(f"/api/v1/tasks/{nr}/emails", headers=e["claude"]).json()["items"][1]["status"] == "rejected"


def test_changed_attachment_fails_instead_of_sending_something_else(menv, ui):  # noqa: F811
    e = menv
    _recipient()
    nr, branch = _task(e, {"out/result.pdf": PDF})
    m = _send(e, nr, attachments=["branch:out/result.pdf"]).json()
    post(ui, f"/tasks/{nr}/emails/{m['id']}/approve")
    e["gitea"].put_branch_file(SERVICE_USER, "ket", branch, "out/result.pdf", PDF + b"changed")
    assert _deliver()["failed"] == 1 and not e["smtp"].messages
    row = _row(m["id"])
    assert row.status == "failed" and "SHA-256" in row.smtp_error


def test_smtp_errors_retry_and_fail(menv, ui):  # noqa: F811
    e = menv
    _recipient()
    nr, _branch = _task(e)
    m = _send(e, nr).json()
    post(ui, f"/tasks/{nr}/emails/{m['id']}/approve")
    e["smtp"].fail_rcpt = (451, "4.3.0 Try again later")
    assert _deliver()["retry"] == 1
    row = _row(m["id"])
    assert row.status == "queued" and row.attempts == 1 and row.next_attempt_at
    e["smtp"].fail_rcpt = (550, "5.1.1 No such user")
    assert _deliver(utcnow() + timedelta(hours=1))["failed"] == 1
    assert _row(m["id"]).status == "failed" and any("550" in c for c in _comments(e, nr))


# --- Retention ------------------------------------------------------------------------------------

def test_body_and_attachment_refs_cleared_after_30_days(menv, ui):  # noqa: F811
    e = menv
    _recipient()
    nr, _branch = _task(e, {"out/result.pdf": PDF})
    sent = _send(e, nr, attachments=["branch:out/result.pdf"]).json()
    pending = _send(e, nr, subject="Old").json()
    post(ui, f"/tasks/{nr}/emails/{sent['id']}/approve")
    assert _deliver()["sent"] == 1
    assert _deliver(utcnow() + timedelta(days=29))["cleared"] == 0
    assert _deliver(utcnow() + timedelta(days=31))["cleared"] == 2
    row = _row(sent["id"])
    assert row.body is None and row.cleared_at and row.status == "sent" and row.subject == "Result KET"
    entry = json.loads(row.attachments_json)[0]
    assert "ref" not in entry and entry["sha256"] == hashlib.sha256(PDF).hexdigest()
    old = _row(pending["id"])
    assert old.status == "rejected" and old.body is None
    assert "30" in ui.get(f"/tasks/{nr}").text


# --- CLI, MCP, TASK.md, project page -----------------------------------------------------------------

def test_cli_and_mcp(menv, tm_as):  # noqa: F811
    e = menv
    _recipient()
    nr, _branch = _task(e, {"out/r.md": "# R"})
    r = tm_as(e["claude"], "mail", "send", nr, "--to", CLIENT, "--subject", "Via CLI", "--body", "Text",
              "--attach", "branch:out/r.md")
    assert r.exit_code == 0, r.output
    assert "approve" in r.stdout and "Via CLI" in r.stdout and "r.md" in r.stdout
    r = tm_as(e["claude"], "mail", "send", nr, "--to", "x@example.org", "--subject", "S", "--body", "B")
    assert r.exit_code != 0 and "mail_recipient_not_allowed" in r.output
    r = tm_as(e["claude"], "mail", "list", nr)
    assert r.exit_code == 0 and "Via CLI" in r.stdout and "waiting for a human's approval" in r.stdout
    r = tm_as(e["claude"], "mail", "recipients", "KET")
    assert r.exit_code == 0 and CLIENT in r.stdout and "needs approval" in r.stdout

    mcp = TaskBrigadeMCP(client_mod.Client(client_mod.Config("http://testserver",
                                                              e["claude"]["Authorization"].split()[1])), None)
    out = mcp.send_email(nr, [CLIENT], "Via MCP", "Body", attachments=["branch:out/r.md"], on_approve=True)
    assert "approve" in out and "Via MCP" in out and "sent when the task is approved" in out
    assert "Via MCP" in mcp.list_emails(nr) and "Via CLI" in mcp.list_emails(nr)
    assert CLIENT in mcp.list_mail_recipients("KET")
    assert "Failed" in mcp.send_email(nr, ["x@example.org"], "S", "B")
    # Another agent sees only its own requests; a non-member may not read the list.
    assert e["c"].get(f"/api/v1/tasks/{nr}/emails", headers=e["codex"]).json()["items"] == []
    assert e["c"].get("/api/v1/projects/KET/mail-recipients", headers=e["codex"]).status_code == 403


def test_task_md_section_and_project_page(menv, ui):  # noqa: F811
    e = menv
    nr, _branch = _task(e)
    task = e["c"].get(f"/api/v1/tasks/{nr}", headers=e["claude"]).json()
    assert task["mail_recipients"] == 0
    note = mail_note(task, "tbr")
    assert "## Sending results by email" in note and f"tbr mail send {nr}" in note and "branch:<path>" in note
    page = ui.get("/projects/KET").text
    assert 'action="/projects/KET/mail-recipients"' in page
    post(ui, "/projects/KET/mail-recipients", address="team@example.org", label="Team", mode="ohne_freigabe")
    page = ui.get("/projects/KET").text
    assert "team@example.org" in page and "ohne Freigabe" in page
    with db.session() as s:
        rid = s.scalar(select(result_mail.ProjectMailRecipient.id))
    r = post(ui, f"/projects/KET/mail-recipients/{rid}/delete")
    assert r.status_code == 200 and "team@example.org" not in r.text
    r = post(ui, "/projects/KET/mail-recipients", address="bad address", mode="freigabe")
    assert r.status_code == 422


@pytest.mark.parametrize("ref", ["branch:", "nothing", "attachment:abc"])
def test_reference_format_is_checked_without_gitea(ref):
    from types import SimpleNamespace

    from app.errors import Unprocessable

    task = SimpleNamespace(id=1, task_nr="KET-0001", branch_name="b", merge_commit_sha=None)
    project = SimpleNamespace(default_branch="main")

    class _S:
        def get(self, *_a):
            return None
    with pytest.raises(Unprocessable):
        result_mail._source(_S(), task, project, ref)
