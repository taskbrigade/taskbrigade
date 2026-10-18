"""Email notify steps reuse the result-mail approval boundary and fake SMTP delivery."""
import json

import pytest
from sqlalchemy import func, select

from app.errors import Unprocessable
from app.models import OutgoingMail, RunResult
from app.services import result_mail, settings_store, state_machine as sm
from app.services.workflows import engine as wf
from tests.test_email import PASSWORD, USER, smtp  # noqa: F401
from tests.test_workflow_engine import finish, graph, latest  # noqa: F401

pytestmark = pytest.mark.db
ADDRESS = "recipient@example.org"
DOCUMENT = {"format": "tbr-aggregate/1", "tables": [
    {"name": "Totals", "columns": ["month", "n", "total"], "rows": [["2026-09", 10, 123.45]]}], "metrics": {}}


def configure(s, owner, server, project):
    settings_store.update(s, {"email_enabled": "true", "smtp_host": "127.0.0.1", "smtp_port": str(server.port),
                              "smtp_security": "starttls", "smtp_user": USER, "smtp_password": PASSWORD,
                              "mail_from_address": "taskbrigade@example.org"}, owner.id)
    result_mail.add_recipient(s, owner, project, ADDRESS, mode="ohne_freigabe")


def email_step(step, **config):
    step.config_json = json.dumps({"channel": "email", "to": [ADDRESS], "subject": "Report {{run.seq}}",
                                   "body": "The released report is attached.", **config})


def complete_until_email(s, owner, run):
    finish(s, owner, latest(s, run, 1).task)
    sm.transition(s, latest(s, run, 2).task, "approve", owner)


def test_workflow_ends_with_email_and_pinned_file(graph, smtp, monkeypatch):  # noqa: F811
    s, owner, agent, other, template, steps = graph
    configure(s, owner, smtp, template.project)
    steps[0].outputs_json = '{"report":{"type":"file"}}'
    email_step(steps[2], attachments=["{{steps.write.output.report}}"])
    run = wf.start(s, owner, template)
    first = latest(s, run, 1)
    finish(s, owner, first.task)
    first.task.merge_commit_sha = "c" * 40
    first.output_json = json.dumps({"output": {"report": {"path": "reports/final.pdf", "commit": "c" * 40}}})
    fetched = []
    class Repo:
        def raw_bytes(self, owner, repo, path, ref):
            fetched.append((path, ref))
            return b"%PDF approved report"
    template.project.gitea_repo, template.project.gitea_owner = "demo", "service"
    monkeypatch.setattr(settings_store, "gitea_client", lambda session: Repo())
    sm.transition(s, latest(s, run, 2).task, "approve", owner)
    assert run.status == "succeeded" and latest(s, run, 3).task_id is None
    mail_id = json.loads(latest(s, run, 3).output_json)["mail_id"]
    row = s.get(OutgoingMail, mail_id)
    assert row.status == "queued" and row.task_id == latest(s, run, 2).task_id
    wf.advance(s, run)
    assert s.scalar(select(func.count()).select_from(OutgoingMail)) == 1
    assert result_mail.deliver(s)["sent"] == 1
    msg = smtp.parsed()
    assert msg["Subject"] == "Report 1" and msg["To"] == ADDRESS
    assert next(msg.iter_attachments()).get_content() == b"%PDF approved report"
    assert fetched == [("reports/final.pdf", "c" * 40)] * 2


@pytest.mark.parametrize("status", ["released", "quarantine", "checks_failed", "pending_approval", "rejected", "kept_sealed"])
def test_local_only_workflow_requires_released_result_and_separate_mail_approval(graph, smtp, status):  # noqa: F811
    s, owner, agent, other, template, steps = graph
    configure(s, owner, smtp, template.project)
    template.project.privacy = "local_only"
    steps[0].agent_role, steps[0].assignee_id = None, other.id
    steps[0].outputs_json = '{"aggregate":{"type":"result"}}'
    email_step(steps[2], attachments=["{{steps.write.output.aggregate}}"])
    run = wf.start(s, owner, template)
    first = latest(s, run, 1)
    finish(s, owner, first.task)
    result = RunResult(task_id=first.task.id, job_id=f"mail-{status}", task_kind="run", status=status,
                       released_json=json.dumps(DOCUMENT) if status == "released" else None)
    s.add(result); s.flush()
    first.output_json = json.dumps({"output": {"aggregate": str(result.id)}})
    sm.transition(s, latest(s, run, 2).task, "approve", owner)
    if status != "released":
        assert run.status == "paused" and run.pause_reason == "mail_result_not_released"
        assert s.scalar(select(func.count()).select_from(OutgoingMail)) == 0
        assert not smtp.messages
        return
    row = s.scalar(select(OutgoingMail))
    assert run.status == "succeeded" and row.status == "pending_approval" and not row.on_approve
    assert result_mail.deliver(s)["sent"] == 0
    result_mail.approve(s, owner, row)
    assert result_mail.deliver(s)["sent"] == 1
    content = next(smtp.parsed().iter_attachments()).get_content()
    assert json.loads(content.split("```json", 1)[1].split("```", 1)[0]) == DOCUMENT


@pytest.mark.parametrize("kind", ["sealed", "raw", "text", "json_status", "task_class", "unknown_recipient"])
def test_workflow_refuses_unsafe_email(graph, smtp, kind):  # noqa: F811
    s, owner, agent, other, template, steps = graph
    configure(s, owner, smtp, template.project)
    email_step(steps[2])
    if kind == "sealed":
        steps[2].data_class = "sealed"
    elif kind == "raw":
        steps[2].data_class = "local_only"
        email_step(steps[2], attachments=["branch:raw.csv"])
    elif kind in ("text", "json_status"):
        steps[0].data_class, steps[1].data_class, steps[2].data_class = ("local_only",) * 3
        steps[0].agent_role, steps[0].assignee_id = None, other.id
        email_step(steps[2], body="{{steps.write.output.private.status}}" if kind == "json_status"
                   else "{{steps.write.summary}}")
        if kind == "json_status":
            steps[0].outputs_json = '{"private":{"type":"json"}}'
    elif kind == "task_class":
        email_step(steps[2], body="{{steps.write.summary}}")
    else:
        email_step(steps[2], to=["stranger@example.org"])
    run = wf.start(s, owner, template)
    latest(s, run, 1).task.result_summary = "Private contents"
    if kind == "task_class":
        latest(s, run, 1).task.data_class = "local_only"
    if kind == "json_status":
        latest(s, run, 1).output_json = json.dumps({"output": {"private": {"status": "Private contents"}}})
    complete_until_email(s, owner, run)
    assert run.status == "paused"
    assert run.pause_reason == {"sealed": "mail_sealed", "raw": "mail_local_only", "text": "mail_local_only",
                                "json_status": "mail_local_only", "task_class": "mail_local_only",
                                "unknown_recipient": "mail_recipient_not_allowed"}[kind]
    assert s.scalar(select(func.count()).select_from(OutgoingMail)) == 0 and not smtp.messages


@pytest.mark.parametrize("change", ["revoked", "changed", "class", "unapproved"])
def test_delivery_rechecks_release_hash_class_and_human_approval(graph, smtp, change):  # noqa: F811
    s, owner, agent, other, template, steps = graph
    configure(s, owner, smtp, template.project)
    run = wf.start(s, owner, template)
    task = latest(s, run, 1).task
    result = RunResult(task_id=task.id, job_id="direct", task_kind="run", status="released",
                       released_json=json.dumps(DOCUMENT))
    s.add(result); s.flush()
    task.data_class = "local_only"
    row = result_mail.request(s, owner, task, to=[ADDRESS], subject="Report", body="Approved result",
                              attachments=[f"result:{result.id}"], on_approve=True)
    assert row.status == "pending_approval"
    assert not row.on_approve
    result_mail.approve(s, owner, row)
    if change == "revoked":
        result.status = "deleted"
    elif change == "changed":
        result.released_json = json.dumps({**DOCUMENT, "metrics": {"altered": 1}})
    elif change == "class":
        task.data_class = "sealed"
    else:
        task.data_class = "cloud_ok"  # Lowering the task must not bypass the saved local-only mail policy.
        row.approved_at = None
    assert result_mail.deliver(s)["failed"] == 1 and not smtp.messages


def test_cross_project_result_refused(graph, smtp):  # noqa: F811
    s, owner, agent, other, template, steps = graph
    configure(s, owner, smtp, template.project)
    run = wf.start(s, owner, template)
    task = latest(s, run, 1).task
    result = RunResult(task_id=task.id, job_id="foreign", task_kind="run", status="released",
                       released_json=json.dumps(DOCUMENT))
    s.add(result); s.flush()
    from types import SimpleNamespace
    with pytest.raises(Unprocessable) as exc:
        result_mail._source(s, task, SimpleNamespace(id=template.project_id + 1), f"result:{result.id}")
    assert exc.value.code == "mail_result_not_released"


def test_mail_approval_rechecks_result_release(graph, smtp):  # noqa: F811
    s, owner, agent, other, template, steps = graph
    configure(s, owner, smtp, template.project)
    run = wf.start(s, owner, template)
    task = latest(s, run, 1).task
    result = RunResult(task_id=task.id, job_id="approval-recheck", task_kind="run", status="released",
                       released_json=json.dumps(DOCUMENT))
    s.add(result); s.flush()
    task.data_class = "local_only"
    row = result_mail.request(s, owner, task, to=[ADDRESS], subject="Report", body="Review the aggregate",
                              attachments=[f"result:{result.id}"])
    result.status = "deleted"
    with pytest.raises(Unprocessable):
        result_mail.approve(s, owner, row)
    assert row.status == "pending_approval" and not smtp.messages
