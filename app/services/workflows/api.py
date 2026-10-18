"""Workflow access, serialization, preview and draft publication. Callers own the transaction."""
import hashlib
import json
from datetime import timedelta
from dataclasses import asdict

from sqlalchemy import func, select

from app.errors import AppError, Conflict, Forbidden, NotFound, Unprocessable
from app.i18n import _
from app.models import (Actor, ProjectMember, Task, WorkflowParam, WorkflowRun, WorkflowRunStep, WorkflowStep,
                        WorkflowTemplate, WorkflowTemplateVersion)
from app.services import settings_store, zones, state_machine as sm
from app.services.workflows import engine, roles
from app.services.workflows.publish import blocking, check_publish, definition_from_version
from app.services.workflows.rendering import context, render_fields, resolve_params


def can_read(session, actor, project):
    return actor.is_active and (actor.role == "admin" or (
        actor.actor_type == "human" and actor.id == project.owner_id) or
        session.get(ProjectMember, (project.id, actor.id)) is not None)


def require_read(session, actor, project):
    if not can_read(session, actor, project):
        raise Forbidden(_("Workflow history requires project membership"))


def require_edit(actor, project):
    if not actor.is_active or not (actor.role in ("admin", "orchestrator") or (
            actor.actor_type == "human" and actor.id == project.owner_id)):
        raise Forbidden(_("Only admins, orchestrators and the project owner may edit workflows"))


def template_get(session, project, key):
    row = session.scalar(select(WorkflowTemplate).where(WorkflowTemplate.project_id == project.id,
                                                       WorkflowTemplate.workflow_key == key))
    if row is None:
        raise NotFound(_("Workflow not found"))
    return row


def run_get(session, actor, run_id):
    run = session.get(WorkflowRun, run_id)
    if run is None:
        raise NotFound(_("Workflow run not found"))
    require_read(session, actor, run.project)
    return run


def visible_class(session, actor, cls):
    return cls in zones.allowed_classes(actor, zones.enforced(session))


def _usage_reported(session, run) -> bool:
    from app.models import TaskUsage

    return bool(session.scalar(select(func.count()).select_from(TaskUsage).join(
        WorkflowRunStep, WorkflowRunStep.task_id == TaskUsage.task_id).where(
        WorkflowRunStep.run_id == run.id)) or 0)


def template_info(session, actor, template, version=None):
    version = version or template.current_version
    latest = session.scalar(select(WorkflowRun).where(WorkflowRun.template_id == template.id)
                            .order_by(WorkflowRun.run_seq.desc()).limit(1))
    definition = definition_from_version(version, template, template.project) if version else {"steps": [], "params": []}
    steps = definition["steps"]
    for step in steps:
        if not visible_class(session, actor, step["data_class"] or template.project.privacy):
            for field in ("title", "description", "acceptance", "outputs", "inputs", "config", "condition_tpl"):
                step[field] = "***"
            step["masked"] = True
    if not visible_class(session, actor, template.project.privacy):
        for param in definition["params"]:
            for field in ("default", "values", "label"):
                param[field] = "***"
    from app.models import WorkflowSchedule
    from app.services.workflows.schedules import info as schedule_info
    schedule_rows = [schedule_info(session, actor, row) for row in session.scalars(
        select(WorkflowSchedule).where(WorkflowSchedule.template_id == template.id).order_by(WorkflowSchedule.id))]
    next_slot = min((r["next_fire_at"] for r in schedule_rows if r["enabled"] and r["next_fire_at"]), default=None)
    return {"id": template.id, "project": template.project.project_key, "key": template.workflow_key,
            "workflow": f"{template.project.project_key}/{template.workflow_key}", "name": template.name,
            "description": template.description if visible_class(session, actor, template.project.privacy) else "***",
            "status": template.status, "owner": template.owner.name,
            "version": version.version if version else None, "state": version.state if version else None,
            "params": definition["params"], "steps": steps, "schedules": schedule_rows, "next_fire_at": next_slot,
            "last_run_status": latest.status if latest else None,
            "default_budget_tokens": template.default_budget_tokens,
            "default_budget_cents": template.default_budget_cents}


def run_info(session, actor, run, *, detail=True):
    rows = list(session.scalars(select(WorkflowRunStep).where(WorkflowRunStep.run_id == run.id)
                               .order_by(WorkflowRunStep.position, WorkflowRunStep.attempt)))
    visible = visible_class(session, actor, run.project.privacy) and all(
        visible_class(session, actor, s.data_class or run.project.privacy) for s in run.version.steps)
    first = next((r.task for r in rows if r.task_id), None)
    current = next((r.task for r in reversed(rows) if r.position == run.current_position and r.task_id), None)
    result = {"id": run.id, "workflow": f"{run.project.project_key}/{run.template.workflow_key}",
              "project": run.project.project_key, "seq": run.run_seq, "version": run.version.version,
              "trigger_kind": run.trigger_kind, "schedule_id": run.schedule_id,
              "scheduled_for": run.scheduled_for, "schedule_timezone": run.schedule_timezone,
              "status": run.status, "current_position": run.current_position, "total_steps": len(run.version.steps),
              "first_task": first.task_nr if first else None, "current_task": current.task_nr if current else None,
              "current_task_status": current.status if current else None,
              "params": json.loads(run.params_json) if visible else "***", "pause_reason": run.pause_reason,
              "error_detail": run.error_detail if visible else "***", "budget_tokens": run.budget_tokens,
              "budget_cents": run.budget_cents, "used_tokens": run.used_tokens, "used_cents": run.used_cents,
               "created_at": run.created_at, "started_at": run.started_at, "finished_at": run.finished_at,
               "usage_reported": _usage_reported(session, run), "warnings": []}
    max_days = int(settings_store.get(session, "workflow_paused_max_days") or 30)
    if any(r.wait_started_at and sm.utcnow() - r.wait_started_at >= timedelta(days=max_days)
           and r.status == "waiting" for r in rows):
        result["warnings"].append("workflow_waiting_too_long")
    if detail:
        result["steps"] = []
        for row in rows:
            readable = visible_class(session, actor, row.output_class or roles.data_class(run, row.step))
            if row.task_id:
                readable = readable and zones.may_handle(run.project, actor, row.task, session=session)
            result["steps"].append({"key": row.step.step_key, "type": row.step.step_type,
                "position": row.position, "attempt": row.attempt, "status": row.status,
                "task_nr": row.task.task_nr if row.task_id else None,
                "task_kind": row.task.task_kind if row.task_id else None,
                "task_status": row.task.status if row.task_id else None,
                "data_class": row.output_class or roles.data_class(run, row.step), "masked": not readable,
                "outputs": json.loads(row.output_json or "{}") if readable else "***",
                "error_code": row.error_code, "error_detail": row.error_detail if readable else "***",
                "deadline_at": row.deadline_at, "next_retry_at": row.next_retry_at,
                "activated_at": row.activated_at, "finished_at": row.finished_at})
    return result


def preview(session, actor, template, params):
    version = engine.require_start(session, actor, template)
    # Transient object only: never add it to the session, reserve a sequence, or acquire write locks.
    run = WorkflowRun(template=template, project=template.project, version=version,
                      project_id=template.project_id, template_id=template.id, version_id=version.id,
                      run_seq=(session.scalar(select(func.max(WorkflowRun.run_seq)).where(
                          WorkflowRun.template_id == template.id)) or 0) + 1,
                      started_at=sm.utcnow(), params_json="{}")
    run.params_json = json.dumps(resolve_params(version, params, context(run), session=session,
                                               project=run.project, actor=template.owner))
    result = {"dry_run": True, "workflow": f"{template.project.project_key}/{template.workflow_key}",
              "version": version.version, "params": json.loads(run.params_json), "steps": [], "warnings": []}
    for step in version.steps:
        cls = roles.data_class(run, step)
        resolved = roles.resolve(session, run, step, lock=False) if step.step_type in ("agent", "script", "approval", "runner") else None
        item = {"key": step.step_key, "type": step.step_type, "role": step.agent_role,
                "assignee": resolved.name if resolved else None, "data_class": cls, "review": step.review_mode,
                "reviewer_id": step.reviewer_id or template.project.default_reviewer_id,
                "placeholder_state": "resolved" if step.position == 1 else "deferred"}
        try:
            zones.check_class(run.project, Task(task_kind="work"), cls, template.owner)
        except AppError as exc:
            result["warnings"].append({"step": step.step_key, "code": exc.code})
        if step.step_type in ("agent", "script", "approval", "runner") and resolved is None:
            result["warnings"].append({"step": step.step_key, "code": "no_assignee"})
        if step.position == 1:
            item.update(render_fields(session, run, step))
            from app.services.workflows.contracts import resolve_bindings
            from app.services.workflows.placeholders import render

            ctx = context(run)
            item["resolutions"] = [trace for value in (step.title_tpl, step.description_tpl, step.acceptance_tpl)
                                   for trace in render(value, ctx).trace]
            item["inputs"] = resolve_bindings(step.inputs_json, ctx)
            if step.step_type == "notify":
                from app.services.workflows.placeholders import PlaceholderError

                try:
                    config = engine._config(step)
                    item["notification"] = render(config.get("text") or item["description"] or item["title"], ctx).text
                except PlaceholderError as exc:
                    raise Unprocessable(_("Workflow placeholders could not be resolved"), code=exc.code) from exc
        if not visible_class(session, actor, cls):
            for name in ("title", "description", "acceptance_criteria", "notification", "resolutions", "inputs"):
                if name in item:
                    item[name] = "***"
            item["masked"] = True
        result["steps"].append(item)
    # Validator messages may quote defaults or template literals from a restricted class.
    result["warnings"].extend({k: v for k, v in asdict(issue).items() if k != "message"} for issue in check_publish(
        definition_from_version(version, template, template.project),
        data_enabled=settings_store.get_bool(session, "data_enabled")))
    if not visible_class(session, actor, template.project.privacy) or not all(
            visible_class(session, actor, roles.data_class(run, step)) for step in version.steps):
        result["params"] = "***"
    return result


def save_draft(session, actor, project, key, body):
    require_edit(actor, project)
    for step in body.steps:
        for actor_id in (step.assignee_id, step.reviewer_id):
            if actor_id is not None and session.get(Actor, actor_id) is None:
                raise Unprocessable(_("Workflow actor not found"), code="workflow_actor_invalid")
    template = session.scalar(select(WorkflowTemplate).where(WorkflowTemplate.project_id == project.id,
        WorkflowTemplate.workflow_key == key).with_for_update().execution_options(populate_existing=True))
    if template is None:
        # Serializes first creation of the project/key, including when no template row exists yet.
        from app.models import Project

        session.scalar(select(Project).where(Project.id == project.id).with_for_update())
        template = session.scalar(select(WorkflowTemplate).where(WorkflowTemplate.project_id == project.id,
            WorkflowTemplate.workflow_key == key).with_for_update())
        if template is None:
            template = WorkflowTemplate(project=project, workflow_key=key, name=body.name or key,
                                        owner_id=project.owner_id, created_by=actor.id)
            session.add(template)
            session.flush()
    if body.name is not None:
        template.name = body.name
    if body.description is not None:
        template.description = body.description
    versions = list(session.scalars(select(WorkflowTemplateVersion).where(
        WorkflowTemplateVersion.template_id == template.id).with_for_update()))
    draft = next((v for v in versions if v.state == "draft"), None)
    if draft:
        if session.scalar(select(WorkflowRun.id).where(WorkflowRun.version_id == draft.id).limit(1)):
            raise Conflict(_("A workflow version with runs cannot be edited"))
        for row in [*draft.steps, *draft.params]:
            session.delete(row)
        session.flush()
        session.expire(draft, ["steps", "params"])
    else:
        draft = WorkflowTemplateVersion(template_id=template.id,
            version=max((v.version for v in versions), default=0) + 1, state="draft", created_by=actor.id)
        session.add(draft)
        session.flush()
    draft.change_note = body.change_note
    for position, item in enumerate(body.steps, 1):
        values = item.model_dump(exclude={"outputs", "inputs", "config"})
        for field in ("outputs", "inputs", "config"):
            values[field + "_json"] = json.dumps(getattr(item, field)) if getattr(item, field) is not None else None
        session.add(WorkflowStep(version_id=draft.id, position=position, **values))
    for item in body.params:
        values = item.model_dump(exclude={"enum_values"})
        values["enum_values"] = json.dumps(item.enum_values) if item.enum_values is not None else None
        session.add(WorkflowParam(version_id=draft.id, **values))
    session.flush()
    session.expire(draft, ["steps", "params"])
    return template, draft


def discard_draft(session, actor, template):
    """Delete the draft version; published versions and runs stay untouched."""
    require_edit(actor, template.project)
    session.scalar(select(WorkflowTemplate).where(WorkflowTemplate.id == template.id).with_for_update())
    draft = session.scalar(select(WorkflowTemplateVersion).where(
        WorkflowTemplateVersion.template_id == template.id, WorkflowTemplateVersion.state == "draft").with_for_update())
    if draft is None:
        raise NotFound(_("Workflow draft not found"))
    if session.scalar(select(WorkflowRun.id).where(WorkflowRun.version_id == draft.id).limit(1)):
        raise Conflict(_("A workflow version with runs cannot be edited"))
    for row in [*draft.steps, *draft.params]:
        session.delete(row)
    session.flush()
    session.delete(draft)
    session.flush()
    session.expire(template, ["versions"])


def publish(session, actor, template):
    require_edit(actor, template.project)
    session.scalar(select(WorkflowTemplate).where(WorkflowTemplate.id == template.id).with_for_update()
                   .execution_options(populate_existing=True))
    draft = session.scalar(select(WorkflowTemplateVersion).where(
        WorkflowTemplateVersion.template_id == template.id, WorkflowTemplateVersion.state == "draft").with_for_update())
    if draft is None:
        raise NotFound(_("Workflow draft not found"))
    definition = definition_from_version(draft, template, template.project)
    if not draft.steps:
        raise Unprocessable(_("Workflow steps must be consecutive"), code="workflow_steps_invalid")
    run = WorkflowRun(template=template, project=template.project, version=draft, project_id=template.project_id)
    role_candidates = {step.agent_role: int(roles.resolve(session, run, step, lock=False) is not None)
                       for step in draft.steps if step.agent_role}
    from app.services import datasets

    issues = check_publish(definition, role_candidates=role_candidates, data_enabled=datasets.enabled(session))
    if blocking(issues):
        raise Unprocessable(_("Workflow publication failed: %(codes)s", codes=", ".join(i.code for i in blocking(issues))),
                            code="workflow_publish_invalid")
    draft.definition_sha256 = hashlib.sha256(json.dumps(definition, sort_keys=True,
        separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    draft.state, draft.published_at, draft.published_by = "published", sm.utcnow(), actor.id
    template.current_version = draft
    session.flush()
    return draft, [asdict(issue) for issue in issues]


def export_version(session, actor, template, version=None):
    """Canonical YAML of a template version (default: the current one). Read rights on the project apply.

    Refuses when the actor may not read any step of the version: a YAML export cannot mask individual fields
    without breaking the round trip, so a partially readable version is not shipped (like the editor's 403).
    """
    from app.services.workflows import transfer

    require_read(session, actor, template.project)
    chosen = version if version is not None else template.current_version
    if chosen is None:
        raise NotFound(_("Workflow version not found"))
    if not all(visible_class(session, actor, st.data_class or template.project.privacy) for st in chosen.steps):
        raise Forbidden(_("This workflow has steps of a data class you may not read"))
    return transfer.export_yaml(template, chosen)


def import_draft(session, actor, project, key, raw):
    """Read a canonical YAML document and store it as an unpublished draft of `project`/`key`.

    Runs the same publish checks as the editor/publish path; blocking problems are refused, so a broken document
    never ends up as a stored draft. Never publishes – a human publishes the draft afterwards.
    """
    from app.services import datasets
    from app.services.workflows import transfer
    from app.api.workflows import Draft

    require_edit(actor, project)
    parsed = transfer.parse_document(session, raw)
    try:
        body = Draft(**parsed)
    except Exception as exc:  # pydantic ValidationError: surface a readable message, not a schema dump
        from pydantic import ValidationError

        if isinstance(exc, ValidationError):
            first = exc.errors()[0]
            place = ".".join(str(x) for x in first["loc"])
            raise Unprocessable(_("Invalid input in %(field)s: %(error)s", field=place, error=first["msg"]),
                                code="workflow_import_invalid") from exc
        raise
    run = WorkflowRun(template=WorkflowTemplate(project_id=project.id, workflow_key=key, name=body.name or key,
                                                 owner_id=project.owner_id), project=project, project_id=project.id)
    definition = {"key": key, "name": body.name, "version": None, "project_data_class": project.privacy,
                  "params": [p.model_dump() for p in body.params],
                  "steps": [{**s.model_dump(), "position": i} for i, s in enumerate(body.steps, 1)]}
    role_candidates = {s.agent_role: int(roles.resolve(session, run, _step_probe(s), lock=False) is not None)
                       for s in body.steps if s.agent_role}
    issues = check_publish(definition, role_candidates=role_candidates, data_enabled=datasets.enabled(session))
    if blocking(issues):
        raise Unprocessable(_("Workflow import failed: %(codes)s", codes=", ".join(i.code for i in blocking(issues))),
                            code="workflow_import_invalid")
    template, draft = save_draft(session, actor, project, key, body)
    return template, draft, [asdict(issue) for issue in issues]


def _step_probe(step):
    from app.models import WorkflowStep

    return WorkflowStep(step_type=step.step_type, agent_role=step.agent_role, assignee_id=step.assignee_id,
                        data_class=step.data_class, review_mode=step.review_mode or "manual",
                        reviewer_id=step.reviewer_id)
