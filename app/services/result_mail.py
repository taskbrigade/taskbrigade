"""Work results by email (docs/konzept-email.md §3, stages 2–3). Agents never get SMTP credentials: they file a send
request (MCP `send_email`, `tbr mail send`), the server checks it and sends it with the `email_deliver` job.

Checks on request: the requester (assignee, admin/orchestrator or project owner), email enabled, the task's effective
data class (`local_only` requires released aggregates and mail approval, `sealed` never), every address on the project's
list `project_mail_recipients` or a human with an account and a confirmed address, attachment sources and types, the
size limit `mail_max_mb` and `mail_daily_limit_per_project` (requests of the last 24 hours).

A request needs a human's approval (`pending_approval`) unless every recipient is marked "without approval" (internal
humans are by default), no attachment is a zip file, the mail is cloud_ok and `on_approve` is off; it is then `queued` at once. `on_approve`
requests are approved together with the task (human approve only). `Reply-To` is the approving human, otherwise the
project owner. Attachments reference Gitea files or released aggregates (`result:<id>`); the server
reads them with their SHA-256 at request time and again when sending – changed content fails the mail.
Body and attachment references are cleared after `KEEP_DAYS`; audit events (`mail_requested/approved/rejected/sent/
failed`) carry task, recipients and hashes, never the body.
"""
import hashlib
import json
import logging
import mimetypes
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, make_msgid
from pathlib import PurePosixPath

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.errors import Conflict, Forbidden, NotFound, Unprocessable
from app.i18n import N_, default_language, use_language
from app.i18n import gettext as _
from app.logging_setup import audit
from app.models import Actor, OutgoingMail, Project, ProjectMailRecipient, RunResult, Task, TaskAttachment, WorkflowRunStep
from app.services import access, mail, notifications, settings_store, zones
from app.services.actors import utcnow
from app.services.attachments import safe_filename
from app.services.gitea import GiteaError
from app.services.state_machine import add_comment, add_event

log = logging.getLogger(__name__)

KEEP_DAYS = 30
MAX_RECIPIENTS = 20
MAX_ATTACHMENTS = 10
MAX_BODY_CHARS = 100_000
MAX_SENDS_PER_RUN = 10
# Concept §3: pdf, docx, xlsx, pptx, csv, md, png, jpg; zip only with a human's approval.
ALLOWED_TYPES = frozenset({"pdf", "docx", "xlsx", "pptx", "csv", "md", "png", "jpg", "jpeg"})
APPROVAL_TYPES = frozenset({"zip"})
SOURCES = ("attachment", "branch", "merge", "result", "output")
MODES = {"freigabe": N_("with approval"), "ohne_freigabe": N_("without approval")}
STATUS_LABELS = {"pending_approval": N_("waiting for approval"), "queued": N_("queued"), "sent": N_("sent"),
                 "failed": N_("failed"), "rejected": N_("rejected")}


# --- Recipients ---------------------------------------------------------------------------------

def recipients(session: Session, project: Project) -> list[ProjectMailRecipient]:
    return list(session.scalars(select(ProjectMailRecipient).where(ProjectMailRecipient.project_id == project.id)
                                .order_by(ProjectMailRecipient.address)))


def recipient_count(session: Session, project: Project) -> int:
    return session.scalar(select(func.count(ProjectMailRecipient.id))
                          .where(ProjectMailRecipient.project_id == project.id)) or 0


def _internal(session: Session, address: str) -> Actor | None:
    """Active human whose confirmed address this is."""
    return session.scalar(select(Actor).where(
        Actor.actor_type == "human", Actor.is_active.is_(True), Actor.email_verified_at.is_not(None),
        func.lower(Actor.email) == address.lower()).limit(1))


def recipient_mode(session: Session, project: Project, address: str) -> str | None:
    """`freigabe`/`ohne_freigabe` for an allowed address, None if it may not receive results of this project.
    An entry on the list wins; humans with an account and a confirmed address need no approval by default."""
    entry = session.scalar(select(ProjectMailRecipient).where(
        ProjectMailRecipient.project_id == project.id, func.lower(ProjectMailRecipient.address) == address.lower()))
    if entry is not None:
        return entry.mode
    return "ohne_freigabe" if _internal(session, address) else None


def add_recipient(session: Session, actor: Actor, project: Project, address: str, label: str | None = None,
                  mode: str = "freigabe") -> ProjectMailRecipient:
    access.require_manage_project(project, actor)
    try:
        address = mail.check_address(address)
    except ValueError:
        raise Unprocessable(_("Invalid email address"), code="invalid_address") from None
    if mode not in MODES:
        raise Unprocessable(_("Unknown mode '%(mode)s'", mode=mode), code="invalid_mode")
    exists = session.scalar(select(ProjectMailRecipient).where(
        ProjectMailRecipient.project_id == project.id, func.lower(ProjectMailRecipient.address) == address.lower()))
    if exists is not None:
        exists.label, exists.mode = (label or "").strip()[:100] or None, mode
        row = exists
    else:
        row = ProjectMailRecipient(project_id=project.id, address=address, label=(label or "").strip()[:100] or None,
                                   mode=mode, created_by=actor.id)
        session.add(row)
    session.flush()
    audit(actor.name, "mail_recipient_set", project=project.project_key, address=address, mode=mode)
    return row


def remove_recipient(session: Session, actor: Actor, project: Project, recipient_id: int) -> None:
    access.require_manage_project(project, actor)
    row = session.get(ProjectMailRecipient, recipient_id)
    if row is None or row.project_id != project.id:
        raise NotFound(_("Recipient not found"))
    audit(actor.name, "mail_recipient_removed", project=project.project_key, address=row.address)
    session.delete(row)
    session.flush()


def recipients_view(session: Session, project: Project) -> dict:
    return {"project": project.project_key,
            "recipients": [{"address": r.address, "label": r.label, "mode": r.mode,
                            "approval": r.mode == "freigabe"} for r in recipients(session, project)],
            "internal": _("Humans with a TaskBrigade account and a confirmed address may be sent to without "
                          "approval."),
            "data_class": project.privacy}


# --- Checks -------------------------------------------------------------------------------------

def _limits(session: Session) -> tuple[int, int]:
    return (int(settings_store.get(session, "mail_max_mb") or 10),
            int(settings_store.get(session, "mail_daily_limit_per_project") or 20))


def _addresses(values, field: str) -> list[str]:
    out, seen = [], set()
    for value in values or []:
        for part in str(value).replace(";", ",").split(","):
            if not part.strip():
                continue
            try:
                address = mail.check_address(part)
            except ValueError:
                raise Unprocessable(_("%(field)s: invalid email address '%(address)s'", field=field,
                                      address=part.strip()[:200]), code="invalid_address") from None
            if address.lower() not in seen:
                seen.add(address.lower())
                out.append(address)
    return out


def check_recipients(session: Session, project: Project, to, cc) -> tuple[list[str], list[str], bool]:
    """Normalised (to, cc) and whether any of them needs approval. Raises for addresses outside the list."""
    to, cc = _addresses(to, "to"), _addresses(cc, "cc")
    cc = [a for a in cc if a.lower() not in {t.lower() for t in to}]
    if not to:
        raise Unprocessable(_("At least one recipient (to) is required"), code="missing_field")
    if len(to) + len(cc) > MAX_RECIPIENTS:
        raise Unprocessable(_("At most %(num)s recipients per mail", num=MAX_RECIPIENTS), code="too_many_recipients")
    modes = {a: recipient_mode(session, project, a) for a in to + cc}
    refused = [a for a, m in modes.items() if m is None]
    if refused:
        raise Unprocessable(_("Not on the recipient list of project %(project)s: %(addresses)s – the project owner adds "
                              "allowed recipients under Projects → %(project)s → Email recipients.",
                              project=project.project_key, addresses=", ".join(refused)),
                            code="mail_recipient_not_allowed")
    return to, cc, any(m == "freigabe" for m in modes.values())


def mail_class(project: Project, task: Task, requested: str | None = None) -> str:
    ranks = {"cloud_ok": 0, "local_only": 1, "sealed": 2}
    return max((project.privacy, zones.effective_class(project, task), requested or "cloud_ok"), key=ranks.__getitem__)


def check_class(project: Project, task: Task, refs=None, requested: str | None = None) -> str:
    cls = mail_class(project, task, requested)
    if cls == "sealed":
        raise Unprocessable(_("Results of sealed tasks are never sent by email."), code="mail_sealed")
    if cls == "local_only" and (not refs or any(not str(ref).startswith("result:") for ref in refs)):
        raise Unprocessable(_("Local-only email requires attachments released by the output gate."),
                            code="mail_local_only")
    return cls


def may_request(session: Session, actor: Actor, task: Task) -> bool:
    project = session.get(Project, task.project_id)
    workflow = session.get(WorkflowRunStep, task.workflow_run_step_id) if task.workflow_run_step_id else None
    return (actor.id == task.assigned_to or actor.role in ("admin", "orchestrator") or access.owns(project, actor)
            or (workflow is not None and workflow.run.template.owner_id == actor.id and actor.is_active))


def may_decide(session: Session, actor: Actor | None, task: Task) -> bool:
    """Approve, edit, reject: humans only – human admins, the project owner and the task's human reviewer."""
    if actor is None or actor.actor_type != "human" or not actor.is_active:
        return False
    project = session.get(Project, task.project_id)
    return actor.role == "admin" or access.owns(project, actor) or task.reviewer_id == actor.id


# --- Attachments --------------------------------------------------------------------------------

class AttachmentError(Exception):
    """An attachment cannot be read or changed since the request (sending fails permanently)."""


@dataclass
class Source:
    ref: str
    path: str
    git_refs: list[str]  # Tried in order (a task branch may be deleted after the merge)
    name: str
    result_id: int | None = None


def _clean_path(value: str) -> str:
    path = (value or "").strip().replace("\\", "/")
    parts = PurePosixPath(path).parts
    if not path or path.startswith("/") or ".." in parts or len(path) > 500:
        raise Unprocessable(_("Invalid attachment path '%(path)s'", path=value[:200]), code="mail_attachment_invalid")
    return "/".join(p for p in parts if p != ".")


def _source(session: Session, task: Task, project: Project, ref: str, at_send: bool = False) -> Source:
    kind, _sep, value = (ref or "").strip().partition(":")
    kind = kind.lower()
    if kind not in SOURCES or not value.strip():
        raise Unprocessable(_("Attachment '%(ref)s': use attachment:<id>, branch:<path>, merge:<path> or result:<id>",
                              ref=(ref or "")[:200]), code="mail_attachment_invalid")
    if kind == "result":
        row = session.get(RunResult, int(value)) if value.isdigit() else None
        origin = session.get(Task, row.task_id) if row else None
        if (row is None or row.status != "released" or not row.released_json or row.task_kind != "run"
                or origin is None or origin.project_id != project.id):
            raise Unprocessable(_("Only released results from this project may be attached."),
                                code="mail_result_not_released")
        return Source(f"result:{row.id}", "", [], f"result-{row.id}.md", row.id)
    if kind == "output":
        from app.services.workflows.contracts import parse_output_contract

        step_id, sep, name = value.partition(":")
        origin = session.get(WorkflowRunStep, int(step_id)) if step_id.isdigit() else None
        anchor = session.get(WorkflowRunStep, task.workflow_run_step_id) if task.workflow_run_step_id else None
        if (not sep or origin is None or anchor is None or origin.run_id != anchor.run_id
                or origin.position > anchor.position or origin.status != "succeeded" or not origin.task):
            raise Unprocessable(_("Invalid workflow email attachment."), code="mail_attachment_invalid")
        spec = parse_output_contract(origin.step.outputs_json).get(name)
        value = json.loads(origin.output_json or "{}").get("output", {}).get(name)
        if (not isinstance(value, dict) or not value.get("path") or not value.get("commit")
                or value["commit"] != origin.task.merge_commit_sha or spec is None or spec.type != "file"):
            raise Unprocessable(_("Invalid workflow email attachment."), code="mail_attachment_invalid")
        if mail_class(project, origin.task, origin.output_class or origin.step.data_class) != "cloud_ok":
            raise Unprocessable(_("Local-only email requires attachments released by the output gate."),
                                code="mail_local_only")
        path = _clean_path(value["path"])
        return Source(ref, path, [value["commit"]], PurePosixPath(path).name)
    if kind == "attachment":
        row = session.get(TaskAttachment, int(value)) if value.strip().isdigit() else None
        if row is None or row.task_id != task.id:
            raise Unprocessable(_("Attachment '%(ref)s' is not an attachment of %(task_nr)s", ref=ref,
                                  task_nr=task.task_nr), code="mail_attachment_missing")
        return Source(f"attachment:{row.id}", row.repo_path, [r for r in (row.commit_sha, project.default_branch) if r],
                      row.filename)
    path = _clean_path(value)
    if kind == "branch":
        refs = [r for r in (task.branch_name, task.merge_commit_sha if at_send else None) if r]
        if not refs:
            raise Unprocessable(_("%(task_nr)s has no task branch yet", task_nr=task.task_nr),
                                code="mail_attachment_missing")
    else:
        if not task.merge_commit_sha:
            raise Unprocessable(_("%(task_nr)s is not merged yet – use branch:<path> before the approval",
                                  task_nr=task.task_nr), code="mail_attachment_missing")
        refs = [task.merge_commit_sha]
    return Source(f"{kind}:{path}", path, refs, PurePosixPath(path).name)


def _fetch(session: Session, project: Project, source: Source) -> bytes | None:
    if source.result_id is not None:
        row = session.get(RunResult, source.result_id)
        if row is None or row.status != "released" or not row.released_json:
            return None
        # Never read quarantine files or logs. Export exactly the gate's transformed, released document.
        return ("```json\n" + json.dumps(json.loads(row.released_json), ensure_ascii=False, sort_keys=True, indent=2)
                + "\n```\n").encode()
    if not project.gitea_repo:
        raise Unprocessable(_("Project has no Gitea repository"), code="no_repo")
    client = settings_store.gitea_client(session)
    for ref in source.git_refs:
        data = client.raw_bytes(project.gitea_owner, project.gitea_repo, source.path, ref)
        if data is not None:
            return data
    return None


def extension(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def resolve_attachments(session: Session, task: Task, project: Project, refs) -> tuple[list[dict], int, bool]:
    """Read each referenced file once: entries {ref, name, size, sha256, content_type}, total size and whether an
    attachment type needs approval."""
    refs = [r for r in (refs or []) if str(r).strip()]
    if len(refs) > MAX_ATTACHMENTS:
        raise Unprocessable(_("At most %(num)s attachments per mail", num=MAX_ATTACHMENTS),
                            code="too_many_attachments")
    entries, total, approval, names = [], 0, False, set()
    for ref in refs:
        source = _source(session, task, project, str(ref))
        name = safe_filename(source.name)
        ext = extension(name)
        if ext not in ALLOWED_TYPES | APPROVAL_TYPES:
            raise Unprocessable(_("%(name)s: file type .%(ext)s cannot be sent (allowed: %(types)s)", name=name,
                                  ext=ext or "?", types=", ".join(sorted(ALLOWED_TYPES | APPROVAL_TYPES))),
                                code="mail_attachment_type")
        approval = approval or ext in APPROVAL_TYPES
        try:
            data = _fetch(session, project, source)
        except GiteaError as e:
            raise Unprocessable(_("Gitea unreachable: %(error)s", error=e), code="gitea_error") from e
        if data is None:
            raise Unprocessable(_("Attachment '%(ref)s' not found", ref=source.ref), code="mail_attachment_missing")
        stem, i = name, 2
        while name in names:
            name = f"{stem.rsplit('.', 1)[0]}-{i}.{ext}" if ext else f"{stem}-{i}"
            i += 1
        names.add(name)
        total += len(data)
        entries.append({"ref": source.ref, "name": name, "size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "content_type": mimetypes.guess_type(name)[0] or "application/octet-stream"})
    return entries, total, approval


# --- Requests -----------------------------------------------------------------------------------

def _loads(value: str | None) -> list:
    try:
        return json.loads(value) if value else []
    except ValueError:
        return []


def _owner_address(session: Session, project: Project) -> str | None:
    owner = session.get(Actor, project.owner_id)
    return owner.email if owner is not None and owner.email else None


def _flat(value: str | None) -> str:
    return " ".join("".join(c if c.isprintable() else " " for c in (value or "")).split())


def request(session: Session, actor: Actor, task: Task, *, to, cc=None, subject: str, body: str,
            attachments=None, on_approve: bool = False, via: str = "api",
            data_class: str | None = None) -> OutgoingMail:
    """File a send request. Returns the row (`pending_approval` or `queued`); raises with a code on refusal."""
    project = session.scalar(select(Project).where(Project.id == task.project_id).with_for_update())
    if not may_request(session, actor, task):
        raise Forbidden(_("Email requests: only the assignee, admins, orchestrators and the project owner"),
                        code="mail_forbidden")
    if task.status in ("draft", "cancelled"):
        raise Conflict(_("No email requests for tasks in status '%(status)s'", status=task.status),
                       code="invalid_status")
    if not mail.enabled(session):
        raise Unprocessable(_("Email is not enabled on this server (Admin → Settings → Email)"), code="mail_disabled")
    cls = check_class(project, task, attachments, data_class)
    if cls == "local_only":
        on_approve = False  # Approval of the task never substitutes for a separate local-only mail decision.
    to, cc, needs = check_recipients(session, project, to, cc)
    subject = _flat(subject)
    if not subject:
        raise Unprocessable(_("Subject is required"), code="missing_field")
    if len(subject) > 200:
        raise Unprocessable(_("Subject: at most 200 characters"), code="subject_too_long")
    body = (body or "").strip()
    if not body:
        raise Unprocessable(_("Body is required"), code="missing_field")
    if len(body) > MAX_BODY_CHARS:
        raise Unprocessable(_("Body: at most %(num)s characters", num=MAX_BODY_CHARS), code="body_too_long")
    max_mb, daily = _limits(session)
    now = utcnow()
    today = session.scalar(select(func.count(OutgoingMail.id)).where(
        OutgoingMail.project_id == project.id, OutgoingMail.created_at >= now - timedelta(days=1))) or 0
    if today >= daily:
        raise Unprocessable(_("Daily limit reached: project %(project)s already has %(num)s email requests in the "
                              "last 24 hours", project=project.project_key, num=today), code="mail_daily_limit")
    entries, size, zip_approval = resolve_attachments(session, task, project, attachments)
    if size + len(body.encode()) > max_mb * 1024 * 1024:
        raise Unprocessable(_("The mail is larger than %(size)s MB (attachments %(attached)s)", size=max_mb,
                              attached=_size(size)), code="mail_too_large")
    pending = bool(on_approve or needs or zip_approval or cls == "local_only")
    row = OutgoingMail(task_id=task.id, project_id=project.id, requested_by=actor.id, to_json=json.dumps(to),
                       cc_json=json.dumps(cc) if cc else None, subject=subject, body=body,
                       attachments_json=json.dumps(entries) if entries else None,
                       status="pending_approval" if pending else "queued", on_approve=bool(on_approve),
                       created_at=now, data_class=cls)
    if not pending:
        row.reply_to = _owner_address(session, project)
    session.add(row)
    session.flush()
    add_event(session, task, actor, "mail_requested", None, row.status, f"#{row.id}")
    add_comment(session, task, actor, "system", _request_note(row, to + cc, entries))
    audit(actor.name, "mail_requested", task=task.task_nr, mail_id=row.id, recipients=to + cc,
          attachment_hashes=[e["sha256"] for e in entries], mail_status=row.status, on_approve=bool(on_approve),
          via=via)
    return row


def _size(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB" if n >= 1024 * 1024 else f"{max(1, round(n / 1024))} KB"


def _request_note(row: OutgoingMail, addresses: list[str], entries: list[dict]) -> str:
    files = ", ".join(f"{e['name']} ({_size(e['size'])}, SHA-256 {e['sha256'][:12]}…)" for e in entries)
    if row.status == "queued":
        text = _("Email request #%(id)s to %(to)s: “%(subject)s” – queued (recipients without approval).", id=row.id,
                 to=", ".join(addresses), subject=row.subject)
    elif row.on_approve:
        text = _("Email request #%(id)s to %(to)s: “%(subject)s” – will be sent when the task is approved.",
                 id=row.id, to=", ".join(addresses), subject=row.subject)
    else:
        text = _("Email request #%(id)s to %(to)s: “%(subject)s” – waiting for approval.", id=row.id,
                 to=", ".join(addresses), subject=row.subject)
    return text + (" " + _("Attachments: %(files)s", files=files) if files else "")


def get(session: Session, mail_id: int, for_update: bool = False) -> OutgoingMail:
    stmt = select(OutgoingMail).where(OutgoingMail.id == mail_id)
    row = session.scalar(stmt.with_for_update() if for_update else stmt)
    if row is None:
        raise NotFound(_("Email request #%(id)s not found", id=mail_id))
    return row


def _require_decide(session: Session, actor: Actor, row: OutgoingMail) -> Task:
    task = session.get(Task, row.task_id)
    if not may_decide(session, actor, task):
        raise Forbidden(_("Email requests are approved by human admins, the project owner or the task's reviewer"),
                        code="mail_forbidden")
    if row.status != "pending_approval":
        raise Conflict(_("Email request #%(id)s is not waiting for approval (%(status)s)", id=row.id,
                         status=_(STATUS_LABELS[row.status])), code="mail_not_pending")
    return task


def approve(session: Session, actor: Actor, row: OutgoingMail, via: str = "web") -> OutgoingMail:
    """Queue a pending request. Recipients and data class are checked again (list or class may have changed)."""
    task = _require_decide(session, actor, row)
    project = session.get(Project, task.project_id)
    check_class(project, task, [e["ref"] for e in _loads(row.attachments_json)], row.data_class)
    try:
        _files(session, row, task, project)
    except AttachmentError as exc:
        raise Unprocessable(str(exc), code="mail_attachment_changed") from exc
    except GiteaError as exc:
        raise Unprocessable(_("Gitea unreachable: %(error)s", error=exc), code="gitea_error") from exc
    check_recipients(session, project, _loads(row.to_json), _loads(row.cc_json))
    row.status, row.approved_by, row.approved_at = "queued", actor.id, utcnow()
    row.reply_to = actor.email or _owner_address(session, project)
    row.next_attempt_at = None
    session.flush()
    add_event(session, task, actor, "mail_approved", "pending_approval", "queued", f"#{row.id}")
    add_comment(session, task, actor, "system", _("Email request #%(id)s approved by %(name)s – it is sent shortly.",
                                                  id=row.id, name=actor.name))
    audit(actor.name, "mail_approved", task=task.task_nr, mail_id=row.id, recipients=addresses(row),
          attachment_hashes=[e["sha256"] for e in _loads(row.attachments_json)], via=via)
    return row


def reject(session: Session, actor: Actor, row: OutgoingMail, reason: str | None = None,
           via: str = "web") -> OutgoingMail:
    task = _require_decide(session, actor, row)
    row.status, row.decided_note = "rejected", (reason or "").strip()[:500] or None
    row.approved_by, row.approved_at = actor.id, utcnow()
    session.flush()
    add_event(session, task, actor, "mail_rejected", "pending_approval", "rejected", f"#{row.id}")
    note = _("Email request #%(id)s rejected by %(name)s.", id=row.id, name=actor.name)
    add_comment(session, task, actor, "system", note + (f" {row.decided_note}" if row.decided_note else ""))
    audit(actor.name, "mail_rejected", task=task.task_nr, mail_id=row.id, recipients=addresses(row), via=via)
    return row


def edit(session: Session, actor: Actor, row: OutgoingMail, *, to, cc, subject: str, body: str,
         via: str = "web") -> OutgoingMail:
    """Change recipients, subject or body of a pending request (attachments stay)."""
    task = _require_decide(session, actor, row)
    project = session.get(Project, task.project_id)
    to, cc, _needs = check_recipients(session, project, to, cc)
    subject, body = _flat(subject), (body or "").strip()
    if not subject or not body:
        raise Unprocessable(_("Subject and body are required"), code="missing_field")
    if len(subject) > 200 or len(body) > MAX_BODY_CHARS:
        raise Unprocessable(_("Subject: at most 200 characters"), code="subject_too_long")
    row.to_json, row.cc_json = json.dumps(to), json.dumps(cc) if cc else None
    row.subject, row.body = subject, body
    session.flush()
    add_event(session, task, actor, "mail_edited", None, None, f"#{row.id}")
    audit(actor.name, "mail_edited", task=task.task_nr, mail_id=row.id, recipients=to + cc, via=via)
    return row


def addresses(row: OutgoingMail) -> list[str]:
    return _loads(row.to_json) + _loads(row.cc_json)


def on_task_status(session: Session, task: Task, actor: Actor | None, action: str) -> None:
    """Approve: queue the task's `on_approve` requests when a human approved (Reply-To = that human); a reviewer
    agent's approval leaves them for a human. Cancel: reject the pending requests."""
    pending = list(session.scalars(select(OutgoingMail).where(
        OutgoingMail.task_id == task.id, OutgoingMail.status == "pending_approval").order_by(OutgoingMail.id)))
    if not pending:
        return
    if task.status == "cancelled":
        for row in pending:
            row.status, row.decided_note = "rejected", _("task cancelled")
            audit(actor.name if actor else "system", "mail_rejected", task=task.task_nr, mail_id=row.id,
                  recipients=addresses(row), reason="task_cancelled")
        add_comment(session, task, actor or session.get(Actor, task.created_by), "system",
                    _("Pending email requests rejected because the task was cancelled: %(ids)s",
                      ids=", ".join(f"#{r.id}" for r in pending)))
        return
    if action != "approve":
        return
    waiting = [r for r in pending if r.on_approve]
    if not waiting:
        return
    if not may_decide(session, actor, task):
        add_comment(session, task, actor, "system", _(
            "Email requests %(ids)s are sent on approval, but they need a human's approval – approve them in the "
            "task's Emails section.", ids=", ".join(f"#{r.id}" for r in waiting)))
        return
    for row in waiting:
        try:
            with session.begin_nested():
                approve(session, actor, row, via="approve")
        except (Unprocessable, Conflict, Forbidden) as e:
            add_comment(session, task, actor, "system", _("Email request #%(id)s was not sent: %(error)s",
                                                          id=row.id, error=e.message))


# --- Views --------------------------------------------------------------------------------------

def view(session: Session, row: OutgoingMail, names: dict[int, str] | None = None) -> dict:
    names = names if names is not None else {}

    def name(actor_id):
        if actor_id is None:
            return None
        if actor_id not in names:
            a = session.get(Actor, actor_id)
            names[actor_id] = a.name if a else None
        return names[actor_id]

    task = session.get(Task, row.task_id)
    return {"id": row.id, "task_nr": task.task_nr if task else None, "status": row.status,
            "status_label": _(STATUS_LABELS[row.status]), "to": _loads(row.to_json), "cc": _loads(row.cc_json),
            "reply_to": row.reply_to, "subject": row.subject, "body": row.body,
            "attachments": [{k: e.get(k) for k in ("ref", "name", "size", "sha256", "content_type")}
                            for e in _loads(row.attachments_json)],
            "on_approve": bool(row.on_approve), "requested_by": name(row.requested_by),
            "approved_by": name(row.approved_by), "approved_at": row.approved_at, "decided_note": row.decided_note,
            "message_id": row.message_id, "smtp_error": row.smtp_error, "attempts": row.attempts,
            "created_at": row.created_at, "sent_at": row.sent_at, "cleared": row.cleared_at is not None}


def listing(session: Session, task: Task, actor: Actor | None = None) -> list[dict]:
    """Requests of a task; an agent sees its own requests only."""
    stmt = select(OutgoingMail).where(OutgoingMail.task_id == task.id)
    if actor is not None and actor.actor_type == "agent":
        stmt = stmt.where(OutgoingMail.requested_by == actor.id)
    names: dict[int, str] = {}
    return [view(session, r, names) for r in session.scalars(stmt.order_by(OutgoingMail.id))]


def pending_for(session: Session, actor: Actor | None) -> list[dict]:
    """Inbox group "Email approvals": pending requests the human may decide."""
    if actor is None or actor.actor_type != "human":
        return []
    stmt = (select(OutgoingMail).join(Task, Task.id == OutgoingMail.task_id)
            .join(Project, Project.id == OutgoingMail.project_id)
            .where(OutgoingMail.status == "pending_approval"))
    if actor.role != "admin":
        stmt = stmt.where((Project.owner_id == actor.id) | (Task.reviewer_id == actor.id))
    names: dict[int, str] = {}
    out = []
    for row in session.scalars(stmt.order_by(OutgoingMail.id).limit(100)):
        item = view(session, row, names)
        item["task_title"] = session.get(Task, row.task_id).title
        out.append(item)
    return out


def on_approve_pending(session: Session, task: Task) -> list[OutgoingMail]:
    return list(session.scalars(select(OutgoingMail).where(
        OutgoingMail.task_id == task.id, OutgoingMail.status == "pending_approval", OutgoingMail.on_approve.is_(True))))


# --- Delivery -----------------------------------------------------------------------------------

def build_message(session: Session, cfg: mail.Config, row: OutgoingMail, task: Task, project: Project,
                  files: list[tuple[dict, bytes]]) -> EmailMessage:
    to, cc = _loads(row.to_json), _loads(row.cc_json)
    with use_language(default_language()):
        footer = [_("Sent by TaskBrigade for task %(task_nr)s (project %(project)s).", task_nr=task.task_nr,
                    project=project.project_key)]
        if row.reply_to:
            footer.append(_("Replies go to %(address)s – TaskBrigade itself does not read replies.",
                            address=row.reply_to))
        else:
            footer.append(_("TaskBrigade does not read replies to this mail."))
    msg = EmailMessage()
    msg["From"] = formataddr((mail._flat(cfg.from_name, 100), mail.check_address(cfg.from_address)))
    msg["To"] = ", ".join(mail.check_address(a) for a in to)
    if cc:
        msg["Cc"] = ", ".join(mail.check_address(a) for a in cc)
    if row.reply_to:
        msg["Reply-To"] = mail.check_address(row.reply_to)
    msg["Subject"] = mail._flat(row.subject)
    msg["Date"] = format_datetime(datetime.now().astimezone())
    msg["Message-ID"] = make_msgid(domain=mail._domain(session, cfg))
    msg["Auto-Submitted"] = "auto-generated"
    msg["X-Auto-Response-Suppress"] = "All"
    body = row.body or ""
    msg.set_content(body + "\n\n-- \n" + "\n".join(footer) + "\n")
    msg.add_alternative(mail._html(body, footer), subtype="html")
    for entry, data in files:
        maintype, _sep, subtype = (entry.get("content_type") or "application/octet-stream").partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype or "octet-stream", filename=entry["name"])
    return msg


def _files(session: Session, row: OutgoingMail, task: Task, project: Project) -> list[tuple[dict, bytes]]:
    """Fetch the attachments again and check their SHA-256 against the request (what was approved is sent)."""
    out = []
    for entry in _loads(row.attachments_json):
        if not entry.get("ref"):
            raise AttachmentError(_("attachment references were cleared"))
        try:
            data = _fetch(session, project, _source(session, task, project, entry["ref"], at_send=True))
        except Unprocessable as e:
            raise AttachmentError(e.message) from None
        if data is None:
            raise AttachmentError(_("%(name)s no longer exists", name=entry["name"]))
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise AttachmentError(_("%(name)s changed since the request (SHA-256 differs)", name=entry["name"]))
        out.append((entry, data))
    return out


def _finish(session: Session, row: OutgoingMail, task: Task, status: str, now: datetime,
            error: str | None = None, message_id: str | None = None) -> None:
    row.status, row.smtp_error, row.next_attempt_at = status, (error or None) and error[:500], None
    author = session.get(Actor, row.approved_by or row.requested_by)
    hashes = [e["sha256"] for e in _loads(row.attachments_json)]
    if status == "sent":
        row.sent_at, row.message_id = now, (message_id or "")[:200] or None
        text = _("Email #%(id)s sent to %(to)s (Message-ID %(message_id)s).", id=row.id, to=", ".join(addresses(row)),
                 message_id=row.message_id)
        if error:
            text += " " + _("Refused by the server: %(error)s", error=error)
        add_comment(session, task, author, "system", text)
        audit(author.name if author else "system", "mail_sent", task=task.task_nr, mail_id=row.id,
              recipients=addresses(row), attachment_hashes=hashes, message_id=row.message_id)
    else:
        add_comment(session, task, author, "system", _("Email #%(id)s could not be sent: %(error)s", id=row.id,
                                                       error=error))
        audit(author.name if author else "system", "mail_failed", task=task.task_nr, mail_id=row.id,
              recipients=addresses(row), attachment_hashes=hashes, error=error)


def cleanup(session: Session, now: datetime | None = None) -> int:
    """After `KEEP_DAYS`: clear body and attachment references (metadata stays); requests still waiting expire."""
    now = now or utcnow()
    rows = list(session.scalars(select(OutgoingMail).where(
        OutgoingMail.created_at < now - timedelta(days=KEEP_DAYS), OutgoingMail.cleared_at.is_(None)).limit(500)))
    for row in rows:
        if row.status in ("pending_approval", "queued"):
            expired = "rejected" if row.status == "pending_approval" else "failed"
            row.status = expired
            if expired == "rejected":
                row.decided_note = _("expired after %(days)s days", days=KEEP_DAYS)
            else:
                row.smtp_error = _("expired after %(days)s days", days=KEEP_DAYS)
            audit("system", f"mail_{expired}", mail_id=row.id, recipients=addresses(row), reason="expired")
        row.body = None
        entries = _loads(row.attachments_json)
        if entries:
            row.attachments_json = json.dumps([{k: v for k, v in e.items() if k != "ref"} for e in entries])
        row.cleared_at = now
    return len(rows)


def deliver(session: Session, now: datetime | None = None) -> dict:
    """Send queued result mails (job `email_deliver`, after the notifications) with the notification throttle."""
    now = now or utcnow()
    counts = {"sent": 0, "failed": 0, "retry": 0, "throttled": 0, "cleared": cleanup(session, now)}
    if not mail.enabled(session):
        return counts
    due = list(session.scalars(select(OutgoingMail).where(
        OutgoingMail.status == "queued",
        OutgoingMail.next_attempt_at.is_(None) | (OutgoingMail.next_attempt_at <= now))
        .order_by(OutgoingMail.id).limit(100)))
    if not due:
        return counts
    cfg = mail.config(session)
    budget = min(MAX_SENDS_PER_RUN, cfg.rate_per_hour - mail.sent_last_hour(session, now))
    conn: mail.Connection | None = None
    sends = 0
    try:
        for row in due:
            if sends >= budget:
                counts["throttled"] += 1
                continue
            task, project = session.get(Task, row.task_id), session.get(Project, row.project_id)
            try:
                cls = check_class(project, task, [e["ref"] for e in _loads(row.attachments_json)], row.data_class)
                if cls == "local_only" and row.approved_at is None:
                    raise Unprocessable(_("Local-only email always requires human approval."), code="mail_local_only")
                files = _files(session, row, task, project)
                msg = build_message(session, cfg, row, task, project, files)
            except (AttachmentError, Unprocessable) as e:
                _finish(session, row, task, "failed", now, str(e) if isinstance(e, AttachmentError) else e.message)
                counts["failed"] += 1
                continue
            except GiteaError as e:  # Gitea down: retry like a connection problem.
                row.attempts += 1
                row.smtp_error, row.next_attempt_at = str(e)[:500], now + notifications._backoff(row.attempts)
                counts["retry"] += 1
                continue
            except ValueError:
                _finish(session, row, task, "failed", now, _("invalid address"))
                counts["failed"] += 1
                continue
            sends += 1
            try:
                conn = conn or mail.Connection(cfg)
                refused = conn.send(msg, addresses(row))
            except mail.MailError as e:
                row.attempts += 1
                if e.permanent or row.attempts >= notifications.MAX_ATTEMPTS:
                    _finish(session, row, task, "failed", now, str(e))
                    counts["failed"] += 1
                else:
                    row.smtp_error, row.next_attempt_at = str(e)[:500], now + notifications._backoff(row.attempts)
                    counts["retry"] += 1
                log.warning("result_mail_failed", extra={"error": str(e), "mail_id": row.id})
                if e.connection:
                    return counts
                continue
            partial = ", ".join(f"{a} ({code})" for a, (code, _reply) in (refused or {}).items()) or None
            _finish(session, row, task, "sent", now, partial, msg["Message-ID"])
            counts["sent"] += 1
    finally:
        if conn is not None:
            conn.close()
    return counts
