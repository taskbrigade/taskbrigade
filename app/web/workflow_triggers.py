"""Event-trigger editor on a workflow template (WF 12)."""
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.auth import UiUser, csrf_protect
from app.errors import AppError, NotFound
from app.i18n import _
from app.models import TRIGGER_KINDS, WorkflowTrigger, WorkflowTriggerFire
from app.services import projects
from app.services.workflows import api, triggers
from app.services.workflows import ui as wf
from app.services.workflows.schedules import OUTCOME_LABELS
from app.web.common import render, ui_actor, web_session
from app.web.tasks_ui import require_user

router = APIRouter()


def _template(s, me, project, key):
    row = api.template_get(s, projects.get(s, project), key)
    api.require_read(s, me, row.project)
    return row


def _base(project: str, key: str) -> str:
    return f"/workflows/{quote(project)}/{quote(key)}"


def _page(s, request, me, template, row=None, error=None, status=200):
    if row is None:
        row = WorkflowTrigger(kind="dataset_ready", filter_json="{}", params_json="{}", enabled=True)
    existing = list(s.scalars(select(WorkflowTrigger).where(WorkflowTrigger.template_id == template.id)
                              .order_by(WorkflowTrigger.id)))
    fires = list(s.scalars(select(WorkflowTriggerFire).join(WorkflowTrigger).where(
        WorkflowTrigger.template_id == template.id).order_by(WorkflowTriggerFire.id.desc()).limit(50)))
    visible = api.visible_class(s, me, template.project.privacy)
    return render(request, "workflow_trigger_edit.html", status, template=template, row=row,
                  triggers=existing, fires=fires, kinds=TRIGGER_KINDS, outcome_labels=OUTCOME_LABELS,
                  can_edit=triggers.may_edit(me, template) and visible, error=error,
                  filter_text=row.filter_json if visible else "***",
                  params_text=row.params_json if visible else "***",
                  base=_base(template.project.project_key, template.workflow_key))


@router.get("/workflows/triggers")
def listing(request: Request, user: UiUser = Depends(require_user)):
    with web_session() as s:
        me = ui_actor(s, user)
        templates = wf.visible_templates(s, me)
        entries = []
        for template in templates:
            for row in s.scalars(select(WorkflowTrigger).where(WorkflowTrigger.template_id == template.id)
                                 .order_by(WorkflowTrigger.id)):
                entries.append((template, row))
        return render(request, "workflow_triggers.html", templates=templates, entries=entries,
                      wf_counts=wf.tab_counts(s, me))


@router.get("/workflows/{project}/{key}/triggers")
def editor(request: Request, project: str, key: str, trigger_id: int | None = None,
           user: UiUser = Depends(require_user)):
    with web_session() as s:
        me = ui_actor(s, user)
        template = _template(s, me, project, key)
        row = s.get(WorkflowTrigger, trigger_id) if trigger_id else None
        if trigger_id and (row is None or row.template_id != template.id):
            raise NotFound(_("Workflow trigger not found"))
        return _page(s, request, me, template, row)


@router.post("/workflows/{project}/{key}/triggers", dependencies=[Depends(csrf_protect)])
def save(request: Request, project: str, key: str, user: UiUser = Depends(require_user),
         trigger_id: int | None = Form(None), kind: str = Form("dataset_ready"),
         filter_json: str = Form("{}"), params_json: str = Form("{}"), enabled: bool = Form(False)):
    values = dict(kind=kind, filter_json=filter_json, params_json=params_json, enabled=enabled)
    with web_session() as s:
        me = ui_actor(s, user)
        template = _template(s, me, project, key)
        triggers.require_edit(me, template)
        try:
            row = triggers.save(s, me, template, values, trigger_id)
        except AppError as exc:
            if exc.status == 404:
                raise
            row = WorkflowTrigger(kind=kind if kind in TRIGGER_KINDS else "dataset_ready",
                                  filter_json=filter_json, params_json=params_json, enabled=enabled)
            row.id = trigger_id
            code = 422 if exc.status == 422 else exc.status
            return _page(s, request, me, template, row, error=exc.message, status=code)
        return RedirectResponse(f"/workflows/{project}/{key}/triggers?trigger_id={row.id}", status_code=303)
