"""Human-owned event-trigger configuration (research/konzept-workflows.md §5.1–5.2)."""
import json

from sqlalchemy import select

from app.errors import Forbidden, NotFound, Unprocessable
from app.i18n import _
from app.models import TRIGGER_KINDS, WorkflowTemplate, WorkflowTrigger
from app.services.workflows import api
from app.services.workflows.events import FILTER_KEYS
from app.services.workflows.placeholders import Placeholder, classify_path, parse_template

_MAX_JSON = 4000


def may_edit(actor, template):
    return actor.is_active and actor.actor_type == "human" and (
        actor.role == "admin" or actor.id in template.project.owner_ids | {template.owner_id})


def require_edit(actor, template):
    if not may_edit(actor, template):
        raise Forbidden(_("Only human admins and workflow or project owners may manage triggers"))


def _json_object(text, label):
    try:
        value = json.loads(text if text not in (None, "") else "{}")
    except ValueError as exc:
        raise Unprocessable(_("Invalid event trigger"), code="workflow_trigger_invalid") from exc
    if not isinstance(value, dict) or len(text or "") > _MAX_JSON:
        raise Unprocessable(_("Invalid event trigger"), code="workflow_trigger_invalid")
    return value


def _check_filter(raw):
    if any(key not in FILTER_KEYS or not isinstance(value, str) or not value.strip() or len(value) > 200
           for key, value in raw.items()):
        raise Unprocessable(_("Invalid event trigger"), code="workflow_trigger_invalid")


def _check_params(raw):
    for key, value in raw.items():
        if not isinstance(key, str) or not key.strip() or len(key) > 40:
            raise Unprocessable(_("Invalid event trigger"), code="workflow_trigger_invalid")
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise Unprocessable(_("Invalid event trigger"), code="workflow_trigger_invalid")
        if not isinstance(value, str):
            continue
        try:
            parts = parse_template(value)
        except Exception as exc:
            raise Unprocessable(_("Invalid event trigger"), code="workflow_trigger_invalid") from exc
        for part in parts:
            if not isinstance(part, Placeholder):
                continue
            ref = classify_path(part.path)
            if ref.kind == "invalid" or ref.kind not in {"event", "run", "workflow", "project", "param"}:
                raise Unprocessable(_("Invalid event trigger"), code="workflow_trigger_invalid")


def save(session, actor, template, values, trigger_id=None):
    require_edit(actor, template)
    if not api.visible_class(session, actor, template.project.privacy):
        raise Forbidden(_("Trigger parameters are hidden by the data class"))
    session.scalar(select(WorkflowTemplate).where(WorkflowTemplate.id == template.id).with_for_update())
    row = session.get(WorkflowTrigger, trigger_id) if trigger_id else None
    if trigger_id and (row is None or row.template_id != template.id):
        raise NotFound(_("Workflow trigger not found"))
    kind = values.get("kind")
    if kind not in TRIGGER_KINDS:
        raise Unprocessable(_("Invalid event trigger"), code="workflow_trigger_invalid")
    filt = _json_object(values.get("filter_json") or "{}", "filter")
    params = _json_object(values.get("params_json") or "{}", "params")
    _check_filter(filt)
    _check_params(params)
    enabled = bool(values.get("enabled"))
    if row is None:
        row = WorkflowTrigger(template_id=template.id, created_by=actor.id, kind=kind,
                              filter_json=json.dumps(filt), params_json=json.dumps(params), enabled=enabled)
        session.add(row)
    else:
        row.kind = kind
        row.filter_json = json.dumps(filt)
        row.params_json = json.dumps(params)
        row.enabled = enabled
    session.flush()
    return row
