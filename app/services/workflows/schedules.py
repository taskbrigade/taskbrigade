"""Human-owned schedule configuration shared by UI and read transports."""
import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfoNotFoundError

from sqlalchemy import select

from app.config import get_settings
from app.errors import Forbidden, NotFound, Unprocessable
from app.i18n import _, N_
from app.models import WorkflowCalendar, WorkflowSchedule, WorkflowTemplate
from app.services.workflows import api
from app.services.workflows.schedule import next_fire, preview, utc, validate


def may_edit(actor, template):
    return actor.is_active and actor.actor_type == "human" and (
        actor.role == "admin" or actor.id in template.project.owner_ids | {template.owner_id})


def require_edit(actor, template):
    if not may_edit(actor, template):
        raise Forbidden(_("Only human admins and workflow or project owners may manage schedules"))


def save(session, actor, template, values, schedule_id=None, now=None, persist=True):
    require_edit(actor, template)
    if not api.visible_class(session, actor, template.project.privacy):
        raise Forbidden(_("Schedule parameters are hidden by the data class"))
    # Match tick/start lock ordering: template, then schedule.
    if persist:
        session.scalar(select(WorkflowTemplate).where(WorkflowTemplate.id == template.id).with_for_update())
    row = session.get(WorkflowSchedule, schedule_id) if schedule_id else None
    if schedule_id and (row is None or row.template_id != template.id):
        raise NotFound(_("Workflow schedule not found"))
    # Validate a transient candidate before mutating a persistent row or doing a preview.
    candidate = WorkflowSchedule(template_id=template.id, created_by=actor.id,
        name=str(values.get("name", "")).strip(), kind=values.get("kind", "cron"),
        cron_expr=values.get("cron_expr") or None, workday_rule_json=values.get("workday_rule_json") or None,
        timezone=values.get("timezone") or get_settings().tz_display,
        calendar_id=values.get("calendar_id") or None, holiday_shift=values.get("holiday_shift", "skip"),
        params_json=values.get("params_json") or "{}", overlap_policy=values.get("overlap_policy", "skip"),
        misfire_policy=values.get("misfire_policy", "run_late"),
        misfire_grace_min=values.get("misfire_grace_min", 360), enabled=values.get("enabled", False))
    if candidate.calendar_id:
        candidate.calendar = session.get(WorkflowCalendar, candidate.calendar_id)
        if candidate.calendar is None:
            raise Unprocessable(_("Workflow calendar not found"))
    try:
        if not candidate.name or len(candidate.name) > 80 or len(candidate.timezone) > 40:
            raise ValueError
        if len(candidate.cron_expr or "") > 100 or len(candidate.workday_rule_json or "") > 300:
            raise ValueError
        validate(candidate)
        candidate.last_fire_at = utc(now or datetime.now(UTC))
        candidate.next_fire_at = next_fire(candidate, candidate.last_fire_at)
    except (ValueError, TypeError, KeyError, ZoneInfoNotFoundError, NotImplementedError) as exc:
        raise Unprocessable(_("Invalid workflow schedule or calendar"), code="workflow_schedule_invalid") from exc
    if not persist:
        return candidate
    if row:
        for attr in ("name", "kind", "cron_expr", "workday_rule_json", "timezone", "calendar_id", "holiday_shift",
                     "params_json", "overlap_policy", "misfire_policy", "misfire_grace_min", "enabled",
                     "last_fire_at", "next_fire_at"):
            setattr(row, attr, getattr(candidate, attr))
        row.calendar = candidate.calendar
        row.last_outcome = None
    else:
        row = candidate
        session.add(row)
    session.flush()
    return row


def info(session, actor, row, now=None):
    can_see = api.visible_class(session, actor, row.template.project.privacy)
    return {"id": row.id, "name": row.name, "enabled": row.enabled, "kind": row.kind,
            "cron_expr": row.cron_expr, "workday_rule": json.loads(row.workday_rule_json) if row.workday_rule_json else None,
            "calendar_id": row.calendar_id, "holiday_shift": row.holiday_shift,
            "misfire_grace_min": row.misfire_grace_min, "last_fire_at": row.last_fire_at,
            "timezone": row.timezone, "next_fire_at": row.next_fire_at, "last_outcome": row.last_outcome,
            "overlap_policy": row.overlap_policy, "misfire_policy": row.misfire_policy,
            "params": json.loads(row.params_json or "{}") if can_see else "***",
            "preview": preview(row, now or datetime.now(UTC), 5)}


OUTCOME_LABELS = {"started": N_("Started"), "queued": N_("Queued"),
                  "skipped_overlap": N_("Skipped (overlap)"), "skipped_misfire": N_("Skipped (misfire)"),
                  "failed": N_("Failed")}


def describe(row) -> str:
    """Human-readable rule for the schedule list: the cron expression, or the workday rule in words."""
    if row.kind != "workday":
        return row.cron_expr or ""
    rule = json.loads(row.workday_rule_json or "{}")
    if not isinstance(rule, dict):
        rule = {}
    t = rule.get("time", "")
    if "day" in rule:
        return _("%(day)s. day of the month (workday) · %(time)s", day=rule.get("day"), time=t)
    period = {"week": _("Week"), "month": _("Month"), "quarter": _("Quarter")}.get(rule.get("of", "month"), "")
    return _("%(nth)s. workday · %(period)s · %(time)s", nth=rule.get("nth"), period=period, time=t)
