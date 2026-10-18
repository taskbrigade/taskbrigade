"""Result mails over the API (`app/services/result_mail.py`): agents file send requests and read their status; the
recipient list of a project is readable for its members. Approval is a human's click in the web UI, not an API call."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import SessionDep, current_actor
from app.errors import Forbidden
from app.i18n import _
from app.models import Actor, ProjectMember
from app.services import access, projects, result_mail
from app.services import tasks as svc

router = APIRouter(tags=["mail"])


class SendMailIn(BaseModel):
    to: list[str] = Field(min_length=1, max_length=result_mail.MAX_RECIPIENTS)
    cc: list[str] = Field(default_factory=list, max_length=result_mail.MAX_RECIPIENTS)
    subject: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=result_mail.MAX_BODY_CHARS)
    attachments: list[str] = Field(default_factory=list, max_length=result_mail.MAX_ATTACHMENTS)
    on_approve: bool = False


@router.post("/tasks/{task_nr}/emails", status_code=201)
def send_email(task_nr: str, body: SendMailIn, session: Session = SessionDep, me: Actor = Depends(current_actor)):
    """File a send request; the answer names the status and whether a human must approve it."""
    task = svc.get(session, task_nr, for_update=True)
    row = result_mail.request(session, me, task, to=body.to, cc=body.cc, subject=body.subject, body=body.body,
                              attachments=body.attachments, on_approve=body.on_approve)
    out = result_mail.view(session, row)
    out["needs_approval"] = row.status == "pending_approval"
    return out


@router.get("/tasks/{task_nr}/emails")
def list_emails(task_nr: str, session: Session = SessionDep, me: Actor = Depends(current_actor)):
    """Requests of the task (agents: their own) with status, Message-ID and SMTP error."""
    task = svc.get(session, task_nr)
    return {"task_nr": task.task_nr, "items": result_mail.listing(session, task, me)}


@router.get("/projects/{key}/mail-recipients")
def mail_recipients(key: str, session: Session = SessionDep, me: Actor = Depends(current_actor)):
    """Allowed recipients of result mails: project members, admins and orchestrators."""
    project = projects.get(session, key)
    member = session.scalar(select(ProjectMember).where(ProjectMember.project_id == project.id,
                                                        ProjectMember.actor_id == me.id))
    if member is None and me.role not in ("admin", "orchestrator") and not access.owns(project, me):
        raise Forbidden(_("Project %(project)s: members only", project=project.project_key))
    return result_mail.recipients_view(session, project)
