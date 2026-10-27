"""View data for the Workflows menu (research/konzept-workflows.md §8, guide docs/workflows-ui.md).

Template list, pipeline run history, the run chain (shared by run detail and task detail), the editor form and
its publish checks, and version diffs. Reads only; writes go through `workflows.api` and `workflows.engine`.
"""
import difflib
import json
from datetime import timedelta

from pydantic import ValidationError
from sqlalchemy import func, select

from app.errors import AppError, Unprocessable
from app.i18n import N_
from app.i18n import gettext as _
from app.models import (Actor, Project, Task, WorkflowRun, WorkflowRunStep, WorkflowStep, WorkflowTemplate,
                        WorkflowTemplateVersion)
from app.services import settings_store, state_machine as sm
from app.services.workflows import api, engine, roles
from app.services.workflows.placeholders import BUILTIN_FIELDS, RUN_FIELDS, STEP_TYPES_WITH_TASK
from app.services.workflows.publish import check_publish

RUN_STATUS_LABELS = {"queued": N_("Queued"), "running": N_("Running"), "waiting": N_("Waiting"),
                     "paused": N_("Paused"), "succeeded": N_("Succeeded"), "failed": N_("Failed"),
                     "cancelled": N_("Cancelled")}
STEP_STATUS_LABELS = {"pending": N_("Pending"), "active": N_("Active"), "waiting": N_("Waiting for a human"),
                      "succeeded": N_("Succeeded"), "failed": N_("Failed"), "skipped": N_("Skipped"),
                      "cancelled": N_("Cancelled")}
# Symbol next to the colour, so a cell never depends on colour alone.
STEP_SYMBOLS = {"pending": "○", "active": "●", "waiting": "◷", "succeeded": "✓", "failed": "✗", "skipped": "–",
                "cancelled": "▫"}
TRIGGER_LABELS = {"manual": N_("manual"), "api": N_("API"), "schedule": N_("schedule"), "event": N_("event"),
                  "workflow": N_("workflow")}
STEP_TYPES = ("agent", "approval", "notify", "script", "runner", "gate")
STEP_TYPE_LABELS = {"agent": N_("Agent"), "approval": N_("Approval"), "notify": N_("Notify"),
                    "script": N_("Script"), "runner": N_("Runner"), "gate": N_("Gate")}
PARAM_TYPES = ("string", "text", "int", "decimal", "bool", "date", "month", "enum", "dataset", "task")
REVIEW_LABELS = {"manual": N_("manual"), "agent": N_("reviewer agent"), "auto": N_("auto")}
ISSUE_LABELS = {"invalid_contract": N_("Invalid definition"), "invalid_form": N_("Invalid input"),
                "unknown_placeholder": N_("Unknown placeholder"), "placeholder_syntax": N_("Placeholder syntax"),
                "filter_mismatch": N_("Filter does not fit the value"), "undeclared_output": N_("Undeclared output"),
                "no_assignee_rule": N_("Assignment"), "role_unresolvable": N_("Role without candidate"),
                "auto_review_not_allowed": N_("Auto review not allowed"),
                "runner_without_data_zones": N_("Needs data zones"),
                "zone_boundary_without_gate": N_("Zone boundary without gate"),
                "workflow_runner_invalid": N_("Invalid workflow runner"),
                "workflow_gate_invalid": N_("Invalid workflow gate"),
                "workflow_condition_invalid": N_("Invalid step condition"),
                "workflow_parallel_invalid": N_("Invalid parallel group"),
                "workflow_data_unreleased": N_("Workflow data awaits release"),
                "gate_checks_failed": N_("Output gate checks failed"),
                "gate_rejected": N_("Output gate rejected"),
                "param_default_invalid": N_("Parameter default")}
JSON_FIELDS = ("outputs", "inputs", "config")
HISTORY_LIMIT = 50
LAST_RUNS = 5


# --- Lists -----------------------------------------------------------------------------------------------------

def visible_templates(session, me: Actor, project: str | None = None, *, include_archived=False) -> list[WorkflowTemplate]:
    """Templates of projects `me` may read (admin, human project owner, member), by project and name."""
    query = (select(WorkflowTemplate).join(Project, Project.id == WorkflowTemplate.project_id)
             .order_by(Project.project_key, WorkflowTemplate.name))
    if project:
        query = query.where(Project.project_key == project.strip().upper())
    if not include_archived:
        query = query.where(WorkflowTemplate.status != "archived")
    return [t for t in session.scalars(query) if api.can_read(session, me, t.project)]


def tab_counts(session, me: Actor) -> dict[str, int]:
    """Numbers behind the tabs: templates, runs, schedules and event triggers `me` may read."""
    from app.models import WorkflowSchedule, WorkflowTrigger

    templates = visible_templates(session, me, include_archived=True)
    ids = [t.id for t in templates]
    if not ids:
        return {"templates": 0, "runs": 0, "schedules": 0, "triggers": 0}

    def count(model):
        visible_ids = ids if model is WorkflowRun else [t.id for t in templates if t.status != "archived"]
        return session.scalar(select(func.count()).select_from(model).where(model.template_id.in_(visible_ids))) or 0

    return {"templates": sum(t.status != "archived" for t in templates), "runs": count(WorkflowRun), "schedules": count(WorkflowSchedule),
            "triggers": count(WorkflowTrigger)}


def edit_projects(session, me: Actor) -> list[Project]:
    """Projects in which `me` may create and edit workflows (`api.require_edit`)."""
    query = select(Project).where(Project.status != "archived").order_by(Project.project_key)
    if not (me.is_active and me.role in ("admin", "orchestrator")):
        if me.actor_type != "human":
            return []
        query = query.where(Project.owned_by(me.id))
    return list(session.scalars(query))


def may_edit(me: Actor, project: Project) -> bool:
    try:
        api.require_edit(me, project)
    except AppError:
        return False
    return True


def may_start(session, me: Actor, template: WorkflowTemplate) -> bool:
    try:
        engine.require_start(session, me, template)
    except AppError:
        return False
    return True


def may_control(me: Actor, run: WorkflowRun) -> bool:
    try:
        engine._require_control(run, me)
    except AppError:
        return False
    return True


def last_runs(session, template_ids: list[int], n: int = LAST_RUNS) -> dict[int, list[WorkflowRun]]:
    """The newest `n` runs per template, newest first."""
    if not template_ids:
        return {}
    rank = func.row_number().over(partition_by=WorkflowRun.template_id, order_by=WorkflowRun.run_seq.desc())
    ranked = select(WorkflowRun.id, rank.label("rn")).where(WorkflowRun.template_id.in_(template_ids)).subquery()
    runs = session.scalars(select(WorkflowRun).join(ranked, ranked.c.id == WorkflowRun.id)
                           .where(ranked.c.rn <= n).order_by(WorkflowRun.template_id, WorkflowRun.run_seq.desc()))
    result: dict[int, list[WorkflowRun]] = {}
    for run in runs:
        result.setdefault(run.template_id, []).append(run)
    return result


def template_rows(session, templates: list[WorkflowTemplate]) -> list[dict]:
    """Rows of the template list (§8.1). Schedules arrive with WF 6, so every template is started manually."""
    runs = last_runs(session, [t.id for t in templates])
    drafts = set(session.scalars(select(WorkflowTemplateVersion.template_id).where(
        WorkflowTemplateVersion.template_id.in_([t.id for t in templates]),
        WorkflowTemplateVersion.state == "draft"))) if templates else set()
    rows = []
    for t in templates:
        latest = runs.get(t.id, [])
        rows.append({"template": t, "project": t.project.project_key, "key": t.workflow_key,
                     "steps": len(t.current_version.steps) if t.current_version else 0,
                     "version": t.current_version.version if t.current_version else None,
                     "draft": t.id in drafts, "runs": list(reversed(latest)), "last": latest[0] if latest else None})
    return rows


# --- Run cells: history matrix and chain ---------------------------------------------------------------------

def _cells(session, run_ids: list[int]) -> dict[int, dict[str, dict]]:
    """Latest attempt per run and step key: status, task, attempts and whether a failed attempt was repaired."""
    if not run_ids:
        return {}
    rows = session.execute(
        select(WorkflowRunStep, WorkflowStep.step_key, WorkflowStep.step_type, Task.task_nr, Task.status)
        .join(WorkflowStep, WorkflowStep.id == WorkflowRunStep.step_id)
        .outerjoin(Task, Task.id == WorkflowRunStep.task_id)
        .where(WorkflowRunStep.run_id.in_(run_ids))
        .order_by(WorkflowRunStep.run_id, WorkflowRunStep.position, WorkflowRunStep.attempt))
    result: dict[int, dict[str, dict]] = {}
    warning_before = sm.utcnow() - timedelta(days=int(settings_store.get(session, "workflow_paused_max_days") or 30))
    for row, key, kind, task_nr, task_status in rows:
        cells = result.setdefault(row.run_id, {})
        earlier = cells.get(key)
        failed_before = bool(earlier and (earlier["status"] == "failed" or earlier["failed_before"]))
        duration = None
        if row.activated_at and row.finished_at:
            duration = int((row.finished_at - row.activated_at).total_seconds())
        cells[key] = {"key": key, "type": kind, "position": row.position, "attempt": row.attempt,
                      "status": row.status, "symbol": STEP_SYMBOLS.get(row.status, "?"), "task_nr": task_nr,
                      "task_status": task_status, "failed_before": failed_before,
                      "repaired": failed_before and row.status == "succeeded", "error_code": row.error_code,
                      "activated_at": row.activated_at, "finished_at": row.finished_at, "duration": duration}
        cells[key]["waiting_too_long"] = (row.status == "waiting" and row.wait_started_at is not None
                                           and row.wait_started_at <= warning_before)
    return result


def chain(session, run: WorkflowRun) -> list[dict]:
    """One cell per step of the pinned version in order (component `_workflow_chain.html`): latest attempt,
    or a pending placeholder for steps the run has not reached."""
    cells = _cells(session, [run.id]).get(run.id, {})
    result = []
    for step in sorted(run.version.steps, key=lambda s: s.position):
        cell = cells.get(step.step_key) or {
            "key": step.step_key, "type": step.step_type, "position": step.position, "attempt": 0,
            "status": "pending", "symbol": STEP_SYMBOLS["pending"], "task_nr": None, "task_status": None,
            "failed_before": False, "repaired": False, "error_code": None, "activated_at": None,
            "finished_at": None, "duration": None}
        result.append({**cell, "data_class": roles.data_class(run, step),
                       "current": run.current_position == step.position and run.status not in engine.TERMINAL})
    return result


def task_context(session, task: Task) -> dict | None:
    """Workflow line on the task page: template, run, step position and the chain."""
    if not task.workflow_run_step_id:
        return None
    row = task.workflow_run_step or session.get(WorkflowRunStep, task.workflow_run_step_id)
    if row is None:
        return None
    run = row.run
    return {"run_id": run.id, "seq": run.run_seq, "name": run.template.name, "key": run.template.workflow_key,
            "project": run.project.project_key, "position": row.position, "total": len(run.version.steps),
            "step_key": row.step.step_key, "status": run.status, "chain": chain(session, run)}


def history(session, template: WorkflowTemplate, *, status: str | None = None, trigger: str | None = None,
            limit: int = HISTORY_LIMIT) -> dict:
    """Pipeline view (§8.4): newest runs first, one column per step key. Columns follow the current version;
    keys that only older versions had are appended."""
    query = select(WorkflowRun).where(WorkflowRun.template_id == template.id)
    if status:
        query = query.where(WorkflowRun.status == status)
    if trigger:
        query = query.where(WorkflowRun.trigger_kind == trigger)
    runs = list(session.scalars(query.order_by(WorkflowRun.run_seq.desc()).limit(limit)))
    cells = _cells(session, [r.id for r in runs])
    columns = [s.step_key for s in template.current_version.steps] if template.current_version else []
    for run in runs:
        for step in run.version.steps:
            if step.step_key not in columns:
                columns.append(step.step_key)
    return {"runs": runs, "cells": cells, "columns": columns}


def run_rows(session, me: Actor, *, project: str | None = None, status: str | None = None,
             limit: int = HISTORY_LIMIT) -> list[WorkflowRun]:
    """All runs `me` may read, newest first (Workflows → Runs)."""
    query = select(WorkflowRun).join(Project, Project.id == WorkflowRun.project_id)
    if project:
        query = query.where(Project.project_key == project.strip().upper())
    if status:
        query = query.where(WorkflowRun.status == status)
    if me.role != "admin":
        from app.models import ProjectMember

        member = select(ProjectMember.project_id).where(ProjectMember.actor_id == me.id)
        query = query.where(WorkflowRun.project_id.in_(member) | Project.owned_by(me.id)
                            if me.actor_type == "human" else WorkflowRun.project_id.in_(member))
    return list(session.scalars(query.order_by(WorkflowRun.id.desc()).limit(limit)))


def progress(session, runs: list[WorkflowRun]) -> dict[int, dict]:
    """Succeeded/skipped steps per run for the run list."""
    cells = _cells(session, [r.id for r in runs])
    return {r.id: {"done": sum(c["status"] in ("succeeded", "skipped") for c in cells.get(r.id, {}).values()),
                   "total": len(r.version.steps)} for r in runs}


# --- Editor form -----------------------------------------------------------------------------------------------

def _json_text(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        return json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
    except ValueError:
        return raw


def editor_form(template: WorkflowTemplate, version: WorkflowTemplateVersion | None) -> dict:
    """Form values of the editor from stored rows (strings, as the browser sends them back)."""
    steps, params = [], []
    if version is not None:
        for s in sorted(version.steps, key=lambda x: x.position):
            steps.append({"key": s.step_key, "type": s.step_type, "title": s.title_tpl,
                          "description": s.description_tpl or "", "acceptance": s.acceptance_tpl or "",
                          "assignee": str(s.assignee_id or ""), "role": s.agent_role or "",
                          "review": s.review_mode, "reviewer": str(s.reviewer_id or ""),
                          "data_class": s.data_class or "", "model_tier": s.model_tier or "",
                          "timeout": str(s.timeout_minutes or ""), "retries": str(s.max_retries or 0),
                          "reminder": str(s.wait_reminder_hours or ""), "backoff": str(s.retry_backoff_s),
                          "optional": "1" if s.optional else "",
                          "group": s.parallel_group or "", "condition": s.condition_tpl or "",
                          **{f: _json_text(getattr(s, f + "_json")) for f in JSON_FIELDS}})
        for p in sorted(version.params, key=lambda x: x.id or 0):
            values = ""
            if p.enum_values:
                try:
                    values = ", ".join(json.loads(p.enum_values))
                except (ValueError, TypeError):
                    values = p.enum_values
            params.append({"name": p.name, "type": p.param_type, "required": "1" if p.required else "",
                           "default": p.default_expr or "", "values": values, "label": p.label, "help": p.help or ""})
    return {"name": template.name, "description": template.description or "",
            "change_note": (version.change_note or "") if version is not None and version.state == "draft" else "",
            "steps": steps, "params": params}


STEP_FIELDS = ("key", "type", "title", "description", "acceptance", "assignee", "role", "review", "reviewer",
               "data_class", "model_tier", "timeout", "retries", "reminder", "backoff", "optional", "group",
               "condition", *JSON_FIELDS)
PARAM_FIELDS = ("name", "type", "required", "default", "values", "label", "help")


def parse_form(form) -> dict:
    """Editor form → form values. Every step and parameter row sends each field once (`s_<field>`/`p_<field>`),
    so the n-th value of each list belongs to the n-th row in display order."""
    def rows(prefix: str, fields: tuple[str, ...]) -> list[dict]:
        columns = {f: [str(v) for v in form.getlist(f"{prefix}_{f}")] for f in fields}
        count = max((len(v) for v in columns.values()), default=0)
        return [{f: (columns[f][i] if i < len(columns[f]) else "") for f in fields} for i in range(count)]

    return {"name": str(form.get("name") or ""), "description": str(form.get("description") or ""),
            "change_note": str(form.get("change_note") or ""),
            "steps": rows("s", STEP_FIELDS), "params": [p for p in rows("p", PARAM_FIELDS) if any(
                (p[f] or "").strip() for f in ("name", "label", "default", "values", "help"))]}


def _int(value: str, label: str) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    if not value.lstrip("-").isdigit():
        raise Unprocessable(_("%(label)s must be a whole number", label=label), code="invalid_form")
    return int(value)


def _json(value: str, label: str):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return json.loads(value)
    except ValueError as exc:
        raise Unprocessable(_("%(label)s is not valid JSON", label=label), code="invalid_form") from exc


def to_draft(values: dict):
    """Form values → the API's `Draft` body (same validation as `PUT …/draft`)."""
    from app.api.workflows import Draft

    steps = []
    for i, s in enumerate(values["steps"], 1):
        where = _("Step %(num)d", num=i)
        steps.append({
            "step_key": s["key"].strip(), "step_type": s["type"], "title_tpl": s["title"].strip(),
            "description_tpl": s["description"] or None, "acceptance_tpl": s["acceptance"] or None,
            "assignee_id": _int(s["assignee"], where), "agent_role": s["role"].strip() or None,
            "data_class": s["data_class"] or None, "review_mode": s["review"] or "manual",
            "reviewer_id": _int(s["reviewer"], where), "model_tier": s["model_tier"] or None,
            "timeout_minutes": _int(s["timeout"], where), "max_retries": _int(s["retries"], where) or 0,
            "wait_reminder_hours": _int(s.get("reminder", ""), where),
            "retry_backoff_s": _int(s.get("backoff", ""), where) if s.get("backoff", "").strip() else 300,
            "optional": s["optional"] == "1",
            "parallel_group": s["group"].strip() or None, "condition_tpl": s["condition"].strip() or None,
            **{f: _json(s[f], f"{where}: {f}") for f in JSON_FIELDS}})
    params = []
    for p in values["params"]:
        enum = [v.strip() for v in p["values"].split(",") if v.strip()] if p["values"].strip() else None
        params.append({"name": p["name"].strip(), "param_type": p["type"] or "string", "required": p["required"] == "1",
                       "default_expr": p["default"] or None, "enum_values": enum,
                       "label": p["label"].strip() or p["name"].strip(), "help": p["help"].strip() or None})
    if len({s["step_key"] for s in steps}) != len(steps) or len({p["name"] for p in params}) != len(params):
        raise Unprocessable(_("Workflow keys and parameter names must be unique"), code="invalid_form")
    try:
        return Draft(name=values["name"].strip() or None, description=values["description"] or None,
                     change_note=values["change_note"].strip() or None, steps=steps, params=params)
    except ValidationError as exc:
        first = exc.errors()[0]
        place = ".".join(str(x) for x in first["loc"])
        raise Unprocessable(_("Invalid input in %(field)s: %(error)s", field=place, error=first["msg"]),
                            code="invalid_form") from exc


def _definition(values: dict, template: WorkflowTemplate) -> dict:
    """Form values → the dict `check_publish` reads; unreadable JSON stays text so the check reports it."""
    def loose(text):
        text = (text or "").strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return text

    def number(text):
        text = (text or "").strip()
        return int(text) if text.isdigit() else None

    steps = [{"key": s["key"].strip(), "position": i, "type": s["type"], "title": s["title"],
              "description": s["description"] or None, "acceptance": s["acceptance"] or None,
              "assignee_id": number(s["assignee"]), "role": s["role"].strip() or None,
              "reviewer": number(s["reviewer"]), "review": s["review"] or "manual",
              "data_class": s["data_class"] or None, "parallel_group": s.get("group", "").strip() or None,
              "condition_tpl": s.get("condition", "").strip() or None,
              **{f: loose(s[f]) for f in JSON_FIELDS}}
             for i, s in enumerate(values["steps"], 1)]
    params = [{"name": p["name"].strip(), "type": p["type"] or "string", "required": p["required"] == "1",
               "default": p["default"] or None,
               "values": [v.strip() for v in p["values"].split(",") if v.strip()] or None, "label": p["label"]}
              for p in values["params"]]
    return {"key": template.workflow_key, "name": values["name"], "version": None,
            "project_data_class": template.project.privacy, "params": params, "steps": steps}


def role_candidates(session, template: WorkflowTemplate, steps: list[dict]) -> dict[str, int]:
    """Whether each role currently has an eligible actor, judged per step like `api.publish` does."""
    run = WorkflowRun(template=template, project=template.project, project_id=template.project_id)
    found: dict[str, int] = {}
    with session.no_autoflush:
        for s in steps:
            if not s["role"]:
                continue
            step = WorkflowStep(step_type=s["type"], agent_role=s["role"], assignee_id=None,
                                data_class=s["data_class"], review_mode=s["review"], reviewer_id=None)
            found[s["role"]] = max(found.get(s["role"], 0), int(roles.resolve(session, run, step, lock=False)
                                                                 is not None))
    return found


def checks(session, template: WorkflowTemplate, values: dict) -> list[dict]:
    """Publish checks of WF 3 on the unsaved form, plus the input validation of the draft API."""
    issues = []
    try:
        to_draft(values)
    except AppError as exc:
        issues.append({"code": exc.code or "invalid_form", "message": exc.message, "severity": "error",
                       "step_key": None})
    if not values["steps"]:
        issues.append({"code": "invalid_form", "message": _("Add at least one step"), "severity": "error",
                       "step_key": None})
    definition = _definition(values, template)
    from app.services import datasets

    for issue in check_publish(definition, role_candidates=role_candidates(session, template, definition["steps"]),
                               data_enabled=datasets.enabled(session)):
        issues.append({"code": issue.code, "message": issue.message, "severity": issue.severity,
                       "step_key": issue.step_key})
    for issue in issues:
        issue["label"] = _(ISSUE_LABELS[issue["code"]]) if issue["code"] in ISSUE_LABELS else issue["code"]
    return issues


def placeholder_basics() -> dict:
    """Data for the editor's "Insert placeholder" lists (static/workflow_editor.js builds each step's list from
    the parameters and earlier steps on the page): run/workflow/project fields and step built-ins."""
    return {"run": [f"run.{f}" for f in RUN_FIELDS] + ["workflow.key", "workflow.name", "workflow.version",
                                                       "project.key", "project.name"],
            "builtins": [b for b in BUILTIN_FIELDS if b not in ("result", "profile", "script")],
            "task_types": sorted(STEP_TYPES_WITH_TASK)}


def actor_choices(session, project: Project) -> list[Actor]:
    """Fixed assignee and reviewer choices: active project members (the engine requires membership)."""
    from app.models import ProjectMember

    return list(session.scalars(select(Actor).join(ProjectMember, ProjectMember.actor_id == Actor.id).where(
        ProjectMember.project_id == project.id, Actor.is_active.is_(True)).order_by(Actor.name)))


# --- Versions and diff -----------------------------------------------------------------------------------------

def versions(session, template: WorkflowTemplate) -> list[dict]:
    counts = dict(session.execute(select(WorkflowRun.version_id, func.count()).where(
        WorkflowRun.template_id == template.id).group_by(WorkflowRun.version_id)).all())
    names = dict(session.execute(select(Actor.id, Actor.name)).all())
    return [{"version": v, "runs": counts.get(v.id, 0), "publisher": names.get(v.published_by),
             "current": v.id == template.current_version_id}
            for v in sorted(template.versions, key=lambda x: x.version, reverse=True)]


def definition_lines(info: dict) -> list[str]:
    """Readable text of a definition (`api.template_info`) for the diff: one field per line, multi-line texts
    indented, JSON fields compact."""
    lines = [f"name: {info['name']}"]
    for p in info["params"]:
        flags = "required" if p.get("required") else "optional"
        lines.append(f"param {p['name']}: {p['type']}, {flags}, default={p.get('default')!s}, "
                     f"values={p.get('values')!s}, label={p.get('label')!s}")
    for s in info["steps"]:
        lines.append(f"step {s['position']} {s['key']} [{s['type']}]")
        for field in ("title", "description", "acceptance"):
            text = s.get(field)
            if text:
                parts = str(text).splitlines() or [""]
                lines.append(f"  {field}: {parts[0]}")
                lines += [f"    {part}" for part in parts[1:]]
        for field in ("assignee_id", "role", "review", "reviewer", "data_class", "parallel_group", "condition_tpl"):
            if s.get(field) is not None:
                lines.append(f"  {field}: {s[field]}")
        for field in JSON_FIELDS:
            if s.get(field) not in (None, "", [], {}):
                value = s[field] if isinstance(s[field], str) else json.dumps(s[field], ensure_ascii=False,
                                                                                sort_keys=True)
                lines.append(f"  {field}: {value}")
    return lines


def diff(old: dict | None, new: dict) -> list[dict]:
    """Unified diff rows `{kind, text}` with kind `add`, `del`, `ctx` or `hunk`."""
    rows = []
    for line in difflib.unified_diff(definition_lines(old) if old else [], definition_lines(new), lineterm="", n=3):
        if line.startswith(("---", "+++")):
            continue
        kind = {"+": "add", "-": "del", "@": "hunk"}.get(line[:1], "ctx")
        rows.append({"kind": kind, "text": line[1:] if kind != "hunk" else line})
    return rows


def find_version(session, template: WorkflowTemplate, ref: str | None) -> WorkflowTemplateVersion | None:
    """`draft`, a version number, or empty (= the current version)."""
    if ref == "draft":
        return session.scalar(select(WorkflowTemplateVersion).where(
            WorkflowTemplateVersion.template_id == template.id, WorkflowTemplateVersion.state == "draft"))
    if ref and ref.isdigit():
        return session.scalar(select(WorkflowTemplateVersion).where(
            WorkflowTemplateVersion.template_id == template.id, WorkflowTemplateVersion.version == int(ref)))
    return template.current_version


def draft_of(session, template: WorkflowTemplate) -> WorkflowTemplateVersion | None:
    return find_version(session, template, "draft")


# --- Kanban, backlog and inbox -----------------------------------------------------------------------------------

def badges(session, tasks: list[Task]) -> dict[int, dict]:
    """Card badge `⟳ <key> #<run>` per task id, in one query for all workflow tasks of a page."""
    ids = {t.workflow_run_step_id for t in tasks if t.workflow_run_step_id}
    if not ids:
        return {}
    rows = session.execute(select(WorkflowRunStep.id, WorkflowRun.id, WorkflowRun.run_seq, WorkflowTemplate.workflow_key,
                                  WorkflowTemplate.name)
                           .join(WorkflowRun, WorkflowRun.id == WorkflowRunStep.run_id)
                           .join(WorkflowTemplate, WorkflowTemplate.id == WorkflowRun.template_id)
                           .where(WorkflowRunStep.id.in_(ids)))
    by_step = {sid: {"run_id": rid, "seq": seq, "key": key, "name": name} for sid, rid, seq, key, name in rows}
    return {t.id: by_step[t.workflow_run_step_id] for t in tasks if t.workflow_run_step_id in by_step}


def filter_options(session, me: Actor | None) -> list[dict]:
    """Templates for the kanban/backlog filter `workflow`: those of projects `me` may read."""
    if me is None:
        return []
    return [{"id": t.id, "label": f"{t.project.project_key}/{t.workflow_key}", "name": t.name}
            for t in visible_templates(session, me, include_archived=True)]
