"""Canonical YAML export/import of workflow template versions (research/konzept-workflows.md §1.6, WF 14).

The export is a stable, human-readable document for diffs, copying and tests – not a backup format. Import
creates an unpublished draft only (never auto-publishes); the same `check_publish` checks as the editor run
before the draft is stored, and the editor/`PUT …/draft` checks run again on publish.

Round trip (export → import) is lossless: import accepts exactly the fields the canonical export writes, and the
stored draft reproduces those fields. Unknown keys, wrong types and unsafe references are refused with a readable
message, never silently dropped.
"""
from __future__ import annotations

import json

import yaml
from sqlalchemy import select

from app.errors import Unprocessable
from app.i18n import _
from app.models import Actor
from app.services.workflows import publish as publish_svc

# Canonical field order of the exported document. Import tolerates a different order but checks the same keys.
STEP_FIELDS = ("key", "type", "title", "description", "acceptance", "role", "assignee", "data_class", "review",
               "reviewer", "model_tier", "timeout_minutes", "wait_reminder_hours", "max_retries", "retry_backoff_s",
               "optional", "outputs", "inputs", "config")
PARAM_FIELDS = ("name", "type", "required", "default", "values", "label", "help")
TOP_FIELDS = ("workflow", "version", "name", "description", "change_note", "params", "steps")
ROLE_PREFIX = "role:"
HUMAN_PREFIX = "human:"
# Defaults of the API's `StepDraft` (app/api/workflows.py): a field equal to its default is omitted from the
# document and reproduced by the default on import, so the round trip stays lossless.
DEFAULT_RETRY_BACKOFF_S = 300
DEFAULT_MAX_RETRIES = {"runner": 2}


class ImportError_(Unprocessable):
    """Raised for an unreadable or contradictory document; carries `code="workflow_import_invalid"`."""


def _fail(message: str) -> "ImportError_":
    return ImportError_(message, code="workflow_import_invalid")


# --- export -------------------------------------------------------------------------------------------------


def _step_document(step) -> dict:
    document: dict = {"key": step.step_key, "type": step.step_type, "title": step.title_tpl}
    if step.description_tpl:
        document["description"] = step.description_tpl
    if step.acceptance_tpl:
        document["acceptance"] = step.acceptance_tpl
    if step.agent_role:
        document["role"] = step.agent_role
    if step.assignee_id:
        document["assignee"] = step.assignee.name if step.assignee else step.assignee_id
    if step.data_class:
        document["data_class"] = step.data_class
    if step.review_mode and step.review_mode != "manual":
        document["review"] = step.review_mode
    if step.reviewer_id:
        document["reviewer"] = step.reviewer.name if step.reviewer else step.reviewer_id
    if step.model_tier:
        document["model_tier"] = step.model_tier
    if step.timeout_minutes:
        document["timeout_minutes"] = step.timeout_minutes
    if step.wait_reminder_hours:
        document["wait_reminder_hours"] = step.wait_reminder_hours
    if step.max_retries != DEFAULT_MAX_RETRIES.get(step.step_type, 0):
        document["max_retries"] = step.max_retries
    # Write any value that differs from the API/model default (300), including 0 – otherwise a step with
    # retry_backoff_s=0 re-imports as 300 and the round trip is not lossless.
    if step.retry_backoff_s is not None and step.retry_backoff_s != DEFAULT_RETRY_BACKOFF_S:
        document["retry_backoff_s"] = step.retry_backoff_s
    if step.optional:
        document["optional"] = True
    for field in ("outputs", "inputs", "config"):
        value = _load(getattr(step, field + "_json"))
        if value is not None:
            document[field] = value
    return document


def _param_document(param) -> dict:
    document: dict = {"name": param.name, "type": param.param_type}
    if not param.required:
        document["required"] = False
    if param.default_expr:
        document["default"] = param.default_expr
    if param.enum_values:
        document["values"] = _load(param.enum_values)
    if param.label:
        document["label"] = param.label
    if param.help:
        document["help"] = param.help
    return document


def _load(raw: str | None):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def document(template, version) -> dict:
    """Canonical dict of one template version, in the field order of the export."""
    steps = sorted(version.steps, key=lambda s: s.position)
    params = sorted(version.params, key=lambda p: (p.id or 0))
    return {"workflow": template.workflow_key, "version": version.version,
            "name": template.name, "description": template.description or None,
            "change_note": version.change_note or None,
            "params": [{k: v for k, v in _param_document(p).items()} for p in params],
            "steps": [_step_document(s) for s in steps]}


def export_yaml(template, version) -> str:
    """YAML document of a version (stable key order, block style, UTF-8, no anchors)."""
    return yaml.safe_dump(document(template, version), sort_keys=False, allow_unicode=True,
                          default_flow_style=False, width=100)


def export_filename(template, version) -> str:
    return f"{template.project.project_key}-{template.workflow_key}-v{version.version}.yaml"


# --- import -------------------------------------------------------------------------------------------------


def parse_yaml(text: str | bytes) -> dict:
    """Read a YAML document; a syntax error or a non-mapping root is an import error."""
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _fail(_("The file is not UTF-8 text")) from exc
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise _fail(_("The file is not valid YAML: %(error)s", error=str(exc).splitlines()[0])) from exc
    if raw is None:
        raise _fail(_("The file is empty"))
    if not isinstance(raw, dict):
        raise _fail(_("The YAML document must be a mapping"))
    return raw


def _reject_unknown(raw: dict, allowed: tuple[str, ...], where: str) -> None:
    unknown = [k for k in raw if k not in allowed]
    if unknown:
        raise _fail(_("Unknown field(s) in %(where)s: %(names)s", where=where, names=", ".join(sorted(map(str, unknown)))))


def _text(raw, where: str, field: str, *, required: bool = False, max_length: int | None = None) -> str | None:
    value = raw.get(field)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise _fail(_("%(where)s: %(field)s is required", where=where, field=field))
        return None
    if not isinstance(value, str):
        raise _fail(_("%(where)s: %(field)s must be text", where=where, field=field))
    if max_length is not None and len(value) > max_length:
        raise _fail(_("%(where)s: %(field)s is longer than %(num)d characters", where=where, field=field,
                      num=max_length))
    return value


def _integer(raw, where: str, field: str, *, minimum: int = 0, maximum: int | None = None) -> int | None:
    value = raw.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail(_("%(where)s: %(field)s must be a whole number", where=where, field=field))
    if value < minimum or (maximum is not None and value > maximum):
        raise _fail(_("%(where)s: %(field)s is out of range", where=where, field=field))
    return value


def _boolean(raw, field: str) -> bool | None:
    value = raw.get(field)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise _fail(_("%(field)s must be true or false", field=field))
    return value


def _actor(session, value, where: str, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        actor = session.get(Actor, value)
        if actor is None:
            raise _fail(_("%(where)s: unknown actor %(value)s in %(field)s", where=where, value=value, field=field))
        return actor.id
    if isinstance(value, str) and value.strip():
        name = value.strip()
        actor = session.scalar(select(Actor).where(Actor.name == name).order_by(Actor.id))
        if actor is None:
            raise _fail(_("%(where)s: unknown actor %(value)s in %(field)s", where=where, value=name, field=field))
        return actor.id
    raise _fail(_("%(where)s: %(field)s must be an actor name", where=where, field=field))


def _step_draft(session, raw, index: int) -> dict:
    where = _("Step %(num)d", num=index)
    if not isinstance(raw, dict):
        raise _fail(_("%(where)s must be a mapping", where=where))
    _reject_unknown(raw, STEP_FIELDS, where)
    key = _text(raw, where, "key", required=True, max_length=40)
    if not publish_svc.IDENT.fullmatch(key):
        raise _fail(_("%(where)s: key %(value)s is invalid", where=where, value=key))
    step_type = _text(raw, where, "type", required=True)
    if step_type not in publish_svc.STEP_TYPES:
        raise _fail(_("%(where)s: unknown type %(value)s", where=where, value=step_type))
    title = _text(raw, where, "title", required=True, max_length=200)
    review = _text(raw, where, "review") or "manual"
    if review not in publish_svc.REVIEW_MODES:
        raise _fail(_("%(where)s: unknown review mode %(value)s", where=where, value=review))
    data_class = _text(raw, where, "data_class")
    if data_class is not None and data_class not in publish_svc.CLASS_RANK:
        raise _fail(_("%(where)s: unknown data class %(value)s", where=where, value=data_class))
    model_tier = _text(raw, where, "model_tier")
    if model_tier is not None and model_tier not in ("simple", "standard", "complex"):
        raise _fail(_("%(where)s: unknown model tier %(value)s", where=where, value=model_tier))
    role = _text(raw, where, "role", max_length=40)
    optional = _boolean(raw, "optional") or False
    retries = _integer(raw, where, "max_retries", minimum=0, maximum=255)
    if retries is None:
        # Leave the field unset so `StepDraft`'s default applies (runner steps default to 2; a hand-written
        # runner step without max_retries must not be forced to 0 here).
        retries = DEFAULT_MAX_RETRIES.get(step_type, 0)
    assignee = _actor(session, raw.get("assignee"), where, "assignee")
    reviewer = _actor(session, raw.get("reviewer"), where, "reviewer")
    if role and assignee:
        raise _fail(_("%(where)s: choose a role or a fixed assignee, not both", where=where))
    if step_type in publish_svc.NEEDS_ASSIGNEE and not role and not assignee:
        if not (step_type == "approval" and reviewer):
            raise _fail(_("%(where)s: this step needs a role or an assignee", where=where))
    for field in ("outputs", "inputs", "config"):
        value = raw.get(field)
        if value is not None and not isinstance(value, (dict, list)):
            raise _fail(_("%(where)s: %(field)s must be a mapping or a list", where=where, field=field))
    return {"step_key": key, "step_type": step_type, "title_tpl": title,
            "description_tpl": _text(raw, where, "description"),
            "acceptance_tpl": _text(raw, where, "acceptance"),
            "assignee_id": assignee, "agent_role": role, "data_class": data_class, "review_mode": review,
            "reviewer_id": reviewer, "model_tier": model_tier,
            "timeout_minutes": _integer(raw, where, "timeout_minutes", minimum=1),
            "wait_reminder_hours": _integer(raw, where, "wait_reminder_hours", minimum=1, maximum=65535),
            "max_retries": retries,
            "retry_backoff_s": _integer(raw, where, "retry_backoff_s", minimum=0) if "retry_backoff_s" in raw else DEFAULT_RETRY_BACKOFF_S,
            "optional": optional,
            "outputs": raw.get("outputs"), "inputs": raw.get("inputs"), "config": raw.get("config")}


def _param_draft(raw, index: int) -> dict:
    where = _("Parameter %(num)d", num=index)
    if not isinstance(raw, dict):
        raise _fail(_("%(where)s must be a mapping", where=where))
    _reject_unknown(raw, PARAM_FIELDS, where)
    name = _text(raw, where, "name", required=True, max_length=40)
    if not publish_svc.IDENT.fullmatch(name):
        raise _fail(_("%(where)s: name %(value)s is invalid", where=where, value=name))
    param_type = _text(raw, where, "type") or "string"
    if param_type not in publish_svc.PARAM_TYPE_MAP:
        raise _fail(_("%(where)s: unknown type %(value)s", where=where, value=param_type))
    values = raw.get("values")
    if values is not None:
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise _fail(_("%(where)s: values must be a list of texts", where=where))
    label = _text(raw, where, "label", max_length=120) or name
    required = _boolean(raw, "required")
    return {"name": name, "param_type": param_type, "required": True if required is None else required,
            "default_expr": _text(raw, where, "default", max_length=200), "enum_values": values,
            "label": label, "help": _text(raw, where, "help", max_length=300)}


def parse_document(session, raw: dict) -> dict:
    """Read a canonical document into the API's `Draft` field names; the caller stores it."""
    _reject_unknown(raw, TOP_FIELDS, _("the document"))
    name = _text(raw, _("the document"), "name", max_length=120)
    description = _text(raw, _("the document"), "description")
    change_note = _text(raw, _("the document"), "change_note", max_length=300)
    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise _fail(_("The document needs at least one step"))
    steps = [_step_draft(session, item, i) for i, item in enumerate(steps_raw, 1)]
    keys = [s["step_key"] for s in steps]
    if len(set(keys)) != len(keys):
        raise _fail(_("Step keys must be unique"))
    params_raw = raw.get("params") or []
    if not isinstance(params_raw, list):
        raise _fail(_("params must be a list"))
    params = [_param_draft(item, i) for i, item in enumerate(params_raw, 1)]
    names = [p["name"] for p in params]
    if len(set(names)) != len(names):
        raise _fail(_("Parameter names must be unique"))
    return {"name": name, "description": description, "change_note": change_note, "steps": steps, "params": params}

