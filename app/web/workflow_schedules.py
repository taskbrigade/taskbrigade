"""Schedule editor attached to a workflow template (WF 6)."""
import json
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.auth import UiUser, csrf_protect
from app.config import get_settings
from app.errors import AppError, NotFound
from app.i18n import _
from app.models import WorkflowCalendar, WorkflowSchedule, WorkflowScheduleFire
from app.services import projects, settings_store
from app.services.workflows import api, schedules
from app.services.workflows import ui as wf
from app.services.workflows.schedule import preview
from app.web.common import render, ui_actor, web_session
from app.web.tasks_ui import require_user

router = APIRouter()


def _template(s, me, project, key):
    row = api.template_get(s, projects.get(s, project), key)
    api.require_read(s, me, row.project)
    return row


@router.get("/workflows/schedules")
def listing(request: Request, user: UiUser = Depends(require_user)):
    with web_session() as s:
        me = ui_actor(s, user)
        templates = wf.visible_templates(s, me)
        entries = []
        for template in templates:
            for row in s.scalars(select(WorkflowSchedule).where(WorkflowSchedule.template_id == template.id)):
                entries.append((template, row, preview(row, datetime.now(UTC))))
        return render(request, "workflow_schedules.html", templates=templates, entries=entries,
                      enabled=settings_store.get_bool(s, "workflows_enabled"),
                      wf_counts=wf.tab_counts(s, me))


@router.get("/workflows/{project}/{key}/schedules")
def editor(request: Request, project: str, key: str, schedule_id: int | None = None,
           user: UiUser = Depends(require_user)):
    with web_session() as s:
        me = ui_actor(s, user)
        template = _template(s, me, project, key)
        row = s.get(WorkflowSchedule, schedule_id) if schedule_id else None
        if schedule_id and (row is None or row.template_id != template.id):
            raise NotFound(_("Workflow schedule not found"))
        return _page(s, request, me, template, row)


def _page(s, request, me, template, row=None, error=None, next_fires=None, status=200):
    now = datetime.now(UTC)
    if row is None:
        row = WorkflowSchedule(name="", kind="cron", cron_expr="0 7 * * mon-fri",
            timezone=get_settings().tz_display, workday_rule_json='{"nth":3,"of":"month","time":"07:00"}',
            params_json="{}", holiday_shift="skip", overlap_policy="skip", misfire_policy="run_late",
            misfire_grace_min=360, enabled=True)
    try:
        rule = json.loads(row.workday_rule_json or '{}')
        if not isinstance(rule, dict):
            rule = {}
    except ValueError:
        rule = {}
    existing = list(s.scalars(select(WorkflowSchedule).where(WorkflowSchedule.template_id == template.id)))
    fires = list(s.scalars(select(WorkflowScheduleFire).join(WorkflowSchedule).where(
        WorkflowSchedule.template_id == template.id).order_by(WorkflowScheduleFire.fired_at.desc(),
        WorkflowScheduleFire.id.desc()).limit(50)))
    from app.services.workflows.tick import status as tick_status
    state = tick_status(s, now)
    last_tick, stale = state["last"], state["enabled"] and state["stale"]
    return render(request, "workflow_schedule_edit.html", status, template=template, row=row,
        schedules=existing, fires=fires, outcome_labels=schedules.OUTCOME_LABELS, calendars=list(s.scalars(select(WorkflowCalendar))),
        can_edit=schedules.may_edit(me, template) and api.visible_class(s, me, template.project.privacy),
        rule=rule, params_text=row.params_json if api.visible_class(s, me, template.project.privacy) else "***", error=error,
        next_fires=next_fires if next_fires is not None else preview(row, now),
        enabled=settings_store.get_bool(s, "workflows_enabled"), last_tick=last_tick, stale_tick=stale)


@router.post("/workflows/{project}/{key}/schedules", dependencies=[Depends(csrf_protect)])
def save(request: Request, project: str, key: str, user: UiUser = Depends(require_user),
         schedule_id: int | None = Form(None), name: str = Form(""), kind: str = Form("cron"),
         cron_expr: str = Form(""), workday_rule_json: str = Form(""), timezone: str = Form(""),
         calendar_id: int | None = Form(None), holiday_shift: str = Form("skip"), params_json: str = Form("{}"),
         overlap_policy: str = Form("skip"), misfire_policy: str = Form("run_late"),
         workday_mode: str = Form(""), workday_nth: int = Form(3), workday_day: int = Form(15),
         workday_of: str = Form("month"), workday_time: str = Form("07:00"), workday_shift: str = Form("next_workday"),
         misfire_grace_min: int = Form(360), enabled: bool = Form(False), action: str = Form("save")):
    if kind == "workday" and workday_mode:
        rule = {"of": workday_of, "time": workday_time}
        if workday_mode == "day":
            rule.update(day=workday_day, shift=workday_shift)
        else:
            rule["nth"] = workday_nth
        workday_rule_json = json.dumps(rule)
    values = dict(name=name, kind=kind, cron_expr=cron_expr, workday_rule_json=workday_rule_json,
        timezone=timezone, calendar_id=calendar_id, holiday_shift=holiday_shift, params_json=params_json,
        overlap_policy=overlap_policy, misfire_policy=misfire_policy,
        misfire_grace_min=misfire_grace_min, enabled=enabled)
    with web_session() as s:
        me = ui_actor(s, user)
        template = _template(s, me, project, key)
        schedules.require_edit(me, template)
        try:
            row = schedules.save(s, me, template, values, schedule_id, persist=action != "preview")
        except AppError as exc:
            if exc.status != 422:
                raise
            row = WorkflowSchedule(**values)
            row.id = schedule_id
            return _page(s, request, me, template, row, error=exc.message, next_fires=[], status=422)
        if action == "preview":
            row.id = schedule_id
            return _page(s, request, me, template, row)
        return RedirectResponse(f"/workflows/{project}/{key}/schedules?schedule_id={row.id}", status_code=303)
