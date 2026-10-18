"""Overview mutations. Callers own the transaction; template locks serialize starts and deletion."""
import json

from sqlalchemy import func, select

from app.errors import Conflict, Forbidden, Unprocessable
from app.i18n import _
from app.models import (Project, WorkflowParam, WorkflowRun, WorkflowSchedule, WorkflowStep,
                        WorkflowTemplate, WorkflowTemplateVersion, WorkflowTrigger)
from app.services.workflows import api, engine


def locked(session, actor, template):
    api.require_edit(actor, template.project)
    template = session.scalar(select(WorkflowTemplate).where(WorkflowTemplate.id == template.id)
                              .with_for_update().execution_options(populate_existing=True))
    if template.status == "archived":
        raise Conflict(_("Workflow is archived"))
    return template


def rename(session, actor, template, name):
    template = locked(session, actor, template)
    name = name.strip()
    if not name or len(name) > 120:
        raise Unprocessable(_("Name is required and must be at most 120 characters"))
    template.name = name


def duplicate(session, actor, template):
    api.require_edit(actor, template.project)
    # Reserve a unique key even when two requests copy the same template concurrently.
    session.scalar(select(Project).where(Project.id == template.project_id).with_for_update())
    template = locked(session, actor, template)
    source = session.scalar(select(WorkflowTemplateVersion).where(
        WorkflowTemplateVersion.template_id == template.id).order_by(WorkflowTemplateVersion.version.desc())
        .limit(1).with_for_update().execution_options(populate_existing=True))
    source_steps = list(session.scalars(select(WorkflowStep).where(WorkflowStep.version_id == source.id)
        .order_by(WorkflowStep.position).with_for_update().execution_options(populate_existing=True))) if source else []
    source_params = list(session.scalars(select(WorkflowParam).where(WorkflowParam.version_id == source.id)
        .order_by(WorkflowParam.id).with_for_update().execution_options(populate_existing=True))) if source else []
    if source and not all(api.visible_class(session, actor, st.data_class or template.project.privacy)
                          for st in source_steps):
        raise Forbidden(_("This workflow has steps of a data class you may not read"))
    number = 1
    while True:
        suffix = "Copy" + (str(number) if number > 1 else "")
        key = template.workflow_key[:40 - len(suffix)] + suffix
        # Use the database's collation for uniqueness (including case/accent equivalence).
        exists = session.scalar(select(WorkflowTemplate.id).where(
            WorkflowTemplate.project_id == template.project_id, WorkflowTemplate.workflow_key == key).with_for_update())
        if exists is None:
            break
        number += 1
    from app.api.workflows import Draft, ParamDraft, StepDraft

    steps, params = [], []
    if source:
        for step in source_steps:
            values = {field: getattr(step, field) for field in StepDraft.model_fields
                      if field not in ("outputs", "inputs", "config")}
            for field in ("outputs", "inputs", "config"):
                raw = getattr(step, field + "_json")
                values[field] = json.loads(raw) if raw is not None else None
            steps.append(StepDraft(**values))
        for param in source_params:
            values = {field: getattr(param, field) for field in ParamDraft.model_fields if field != "enum_values"}
            values["enum_values"] = json.loads(param.enum_values) if param.enum_values is not None else None
            params.append(ParamDraft(**values))
    body = Draft(name=_("Copy of %(name)s", name=template.name)[:120], description=template.description,
                 steps=steps, params=params)
    copied, _draft = api.save_draft(session, actor, template.project, key, body)
    copied.default_budget_tokens = template.default_budget_tokens
    copied.default_budget_cents = template.default_budget_cents
    return copied


def impact(session, template):
    def count(model, *conditions):
        return session.scalar(select(func.count()).select_from(model).where(
            model.template_id == template.id, *conditions)) or 0
    return {"runs": count(WorkflowRun),
            "active": count(WorkflowRun, WorkflowRun.status.not_in(engine.TERMINAL)),
            "schedules": count(WorkflowSchedule, WorkflowSchedule.enabled.is_(True)),
            "triggers": count(WorkflowTrigger, WorkflowTrigger.enabled.is_(True))}


def archive(session, actor, template):
    template = locked(session, actor, template)
    active = session.scalar(select(WorkflowRun.id).where(
        WorkflowRun.template_id == template.id, WorkflowRun.status.not_in(engine.TERMINAL))
        .with_for_update().limit(1))
    if active:
        raise Conflict(_("Cancel active runs first"))
    template.status = "archived"
    for model in (WorkflowSchedule, WorkflowTrigger):
        for row in session.scalars(select(model).where(model.template_id == template.id).with_for_update()):
            row.enabled = False
            if isinstance(row, WorkflowSchedule):
                row.next_fire_at = None
