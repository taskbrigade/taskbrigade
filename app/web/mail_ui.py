"""UI: approve, edit and reject result mails (`app/services/result_mail.py`) in the task detail and the inbox."""
from fastapi import APIRouter, Depends, Form, Request

from app.auth import UiUser, csrf_protect
from app.errors import AppError
from app.i18n import N_
from app.i18n import gettext as _
from app.models import Task
from app.services import result_mail
from app.web.common import render, ui_actor, web_session
from app.web.tasks_ui import _run, require_user

router = APIRouter()
POST = [Depends(csrf_protect)]
DONE = {"approve": N_("Email request #%(id)s approved – it is sent shortly."),
        "reject": N_("Email request #%(id)s rejected.")}


def _row(s, nr: str, mail_id: int):
    row = result_mail.get(s, mail_id, for_update=True)
    task = s.get(Task, row.task_id)
    if task is None or task.task_nr != nr:
        raise AppError(_("Email request #%(id)s not found", id=mail_id), code="not_found")
    return row


@router.post("/tasks/{nr}/emails/{mail_id}/approve", dependencies=POST)
def approve(request: Request, nr: str, mail_id: int, user: UiUser = Depends(require_user)):
    return _run(request, user, nr, lambda s, me: result_mail.approve(s, me, _row(s, nr, mail_id)),
                _(DONE["approve"], id=mail_id))


@router.post("/tasks/{nr}/emails/{mail_id}/reject", dependencies=POST)
def reject(request: Request, nr: str, mail_id: int, user: UiUser = Depends(require_user), reason: str = Form("")):
    return _run(request, user, nr, lambda s, me: result_mail.reject(s, me, _row(s, nr, mail_id), reason),
                _(DONE["reject"], id=mail_id))


@router.post("/tasks/{nr}/emails/{mail_id}/edit", dependencies=POST)
def edit(request: Request, nr: str, mail_id: int, user: UiUser = Depends(require_user), to: str = Form(""),
         cc: str = Form(""), subject: str = Form(""), body: str = Form("")):
    return _run(request, user, nr, lambda s, me: result_mail.edit(
        s, me, _row(s, nr, mail_id), to=[to], cc=[cc], subject=subject, body=body),
        _("Email request #%(id)s saved – approve it to send it.", id=mail_id))


@router.post("/inbox/emails/{mail_id}/{action}", dependencies=POST)
def inbox_action(request: Request, mail_id: int, action: str, user: UiUser = Depends(require_user),
                 reason: str = Form("")):
    """Inbox inline decision (HTMX): replace the item with the result."""
    if action not in DONE:
        raise AppError(_("Unknown action"))
    try:
        with web_session() as s:
            me = ui_actor(s, user)
            row = result_mail.get(s, mail_id, for_update=True)
            if action == "approve":
                result_mail.approve(s, me, row, via="inbox")
            else:
                result_mail.reject(s, me, row, reason, via="inbox")
    except AppError as e:
        return render(request, "_flash.html", 422, message=f"#{mail_id}: {e.message}", kind="error")
    return render(request, "_flash.html", message=_(DONE[action], id=mail_id), kind="ok")
