"""Shared parameter validation and rendering for real activation and read-only previews."""
import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import select

from app.errors import Unprocessable
from app.i18n import _
from app.models import WorkflowRunStep
from app.services.workflows.placeholders import Context, PlaceholderError, StepValue, render


def context(run):
    from app.config import get_settings

    return Context(params=json.loads(run.params_json or "{}"), run_id=run.id or "<new-run>", run_seq=run.run_seq,
                   run_key=f"{run.template.workflow_key} #{run.run_seq}",
                   run_url=f"/workflow-runs/{run.id or '<new-run>'}",
                   started_at=run.started_at, scheduled_for=run.scheduled_for,
                   timezone=run.schedule_timezone or get_settings().tz_display,
                   workflow_key=run.template.workflow_key, workflow_name=run.template.name,
                   workflow_version=run.version.version, project_key=run.project.project_key,
                   project_name=run.project.name)


def resolve_params(version, supplied, ctx, *, session=None, project=None, actor=None):
    supplied = supplied or {}
    declared = {p.name: p for p in version.params}
    if supplied.keys() - declared.keys():
        raise Unprocessable(_("Unknown workflow parameter"), code="workflow_params_invalid")
    result = dict(supplied)
    # Defaults can refer to earlier parameters. Retry allows forward references too, without cycles.
    pending = [p for p in version.params if p.name not in result]
    for _pass in range(len(pending) + 1):
        progress = False
        for param in pending[:]:
            ctx.params = result
            try:
                value = render(param.default_expr, ctx).text if param.default_expr is not None else None
            except PlaceholderError:
                continue
            if value is not None:
                result[param.name] = value
            pending.remove(param)
            progress = True
        if not progress:
            break
    if pending:
        raise Unprocessable(_("Workflow parameter defaults could not be resolved"), code="workflow_params_invalid")
    for name, param in declared.items():
        value = result.get(name)
        if value is None or value == "":
            if param.required:
                raise Unprocessable(_("Required workflow parameter: %(name)s", name=name), code="workflow_params_invalid")
            continue
        try:
            kind = param.param_type
            if kind in ("string", "text", "enum", "date", "month", "task"):
                if not isinstance(value, str):
                    raise ValueError
                if kind == "enum" and value not in json.loads(param.enum_values or "[]"):
                    raise ValueError
                if kind == "date":
                    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                        raise ValueError
                    date.fromisoformat(value)
                if kind == "month":
                    if not re.fullmatch(r"\d{4}-\d{2}", value):
                        raise ValueError
                    date.fromisoformat(value + "-01")
                if kind == "task" and not re.fullmatch(r"[A-Z][A-Z0-9]*-\d+", value):
                    raise ValueError
            elif kind in ("int", "dataset"):
                if isinstance(value, bool) or not re.fullmatch(r"-?\d+", str(value)):
                    raise ValueError
                value = int(value)
                if kind == "dataset" and value <= 0:
                    raise ValueError
                if kind == "dataset":
                    from app.models import Dataset
                    from app.services import datasets

                    ds = session.get(Dataset, value) if session is not None else None
                    if (ds is None or project is None or ds.project_id != project.id
                            or ds.status in ("receiving", "deleted", "archived")
                            or not datasets.can_upload(session, project, actor)):
                        raise ValueError
            elif kind == "decimal":
                if isinstance(value, (bool, float)):
                    raise ValueError
                number = Decimal(str(value))
                if not number.is_finite():
                    raise ValueError
                value = str(number)
            elif kind == "bool":
                if value not in (True, False, "true", "false") or isinstance(value, int) and not isinstance(value, bool):
                    raise ValueError
                value = value is True or value == "true"
            else:
                raise ValueError
        except (ValueError, TypeError, InvalidOperation) as exc:
            raise Unprocessable(_("Invalid workflow parameter: %(name)s", name=name),
                                code="workflow_params_invalid") from exc
        result[name] = value
    return result


def step_context(session, run):
    ctx = context(run)
    if run.id:
        rows = session.scalars(select(WorkflowRunStep).where(WorkflowRunStep.run_id == run.id)
                               .order_by(WorkflowRunStep.position, WorkflowRunStep.attempt))
        for row in rows:
            values = json.loads(row.output_json or "{}")
            task = row.task
            ctx.steps[row.step.step_key] = StepValue(
                step_type=row.step.step_type, data_class=row.output_class or row.step.data_class or run.project.privacy,
                status=row.status, task_nr=task.task_nr if task else values.get("task_nr"),
                summary=task.result_summary if task else values.get("summary"),
                branch=task.branch_name if task else values.get("branch"), pr=task.pr_number if task else values.get("pr"),
                commit=task.result_commit_sha if task else values.get("commit"),
                merge_commit=task.merge_commit_sha if task else values.get("merge_commit"),
                open_points=json.loads(task.open_points or "[]") if task else values.get("open_points"),
                output=values.get("output", {}), result=values.get("result"), profile=values.get("profile"),
                script=values.get("script"))
    return ctx


def render_fields(session, run, step):
    ctx = step_context(session, run)
    try:
        from app.services.workflows.contracts import ContractError, resolve_bindings

        resolve_bindings(step.inputs_json, ctx)
        fields = {name: render(value, ctx).text if value is not None else None for name, value in (
            ("title", step.title_tpl), ("description", step.description_tpl),
            ("acceptance_criteria", step.acceptance_tpl))}
        if not fields["title"] or not fields["title"].strip() or len(fields["title"]) > 200:
            raise Unprocessable(_("Workflow title must contain 1 to 200 characters"), code="workflow_title_invalid")
        return fields
    except PlaceholderError as exc:
        raise Unprocessable(_("Workflow placeholders could not be resolved"), code=exc.code) from exc
    except ContractError as exc:
        raise Unprocessable(_("Invalid workflow input bindings"), code="workflow_inputs_invalid") from exc
