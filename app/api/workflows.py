"""Stage-1 workflow templates, execution and controls."""
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import SessionDep, current_actor, require_admin
from app.errors import Forbidden, NotFound, Unprocessable
from app.i18n import _
from app.models import Actor, WorkflowRun, WorkflowTemplate, WorkflowTemplateVersion
from app.services import projects
from app.services.workflows import engine
from app.services.workflows import api as service

router = APIRouter(tags=["workflows"])


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunStart(StrictBody):
    params: dict = Field(default_factory=dict)
    budget_tokens: int | None = Field(None, ge=0, le=4294967295)
    budget_cents: int | None = Field(None, ge=0, le=4294967295)
    dry_run: bool = False
    queue: bool = False
    force_parallel: bool = False


class RunControl(StrictBody):
    reason: str | None = Field(None, max_length=500)
    from_step: str | None = Field(None, max_length=40)
    skip_failed: bool = False
    budget_tokens: int | None = Field(None, ge=0, le=4294967295)
    budget_cents: int | None = Field(None, ge=0, le=4294967295)


class StepDraft(StrictBody):
    step_key: str = Field(pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$", max_length=40)
    step_type: Literal["agent", "script", "runner", "approval", "gate", "notify"]
    title_tpl: str = Field(min_length=1, max_length=200)
    description_tpl: str | None = None
    acceptance_tpl: str | None = None
    assignee_id: int | None = Field(None, gt=0, le=4294967295)
    agent_role: str | None = Field(None, max_length=40)
    data_class: Literal["cloud_ok", "local_only", "sealed"] | None = None
    review_mode: Literal["manual", "agent", "auto"] = "manual"
    reviewer_id: int | None = Field(None, gt=0, le=4294967295)
    model_tier: Literal["simple", "standard", "complex"] | None = None
    timeout_minutes: int | None = Field(None, gt=0, le=4294967295)
    wait_reminder_hours: int | None = Field(None, gt=0, le=65535)
    max_retries: int = Field(default_factory=lambda data: 2 if data.get("step_type") == "runner" else 0,
                             ge=0, le=255)
    retry_backoff_s: int = Field(300, ge=0, le=4294967295)
    optional: bool = False
    parallel_group: str | None = Field(None, pattern=r"^[a-zA-Z_][a-zA-Z0-9_-]*$", max_length=40)
    condition_tpl: str | None = Field(None, max_length=2000)
    outputs: dict | None = None
    inputs: list[dict] | None = None
    config: dict | None = None


class ParamDraft(StrictBody):
    name: str = Field(pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$", max_length=40)
    param_type: Literal["string", "text", "int", "decimal", "bool", "date", "month", "enum", "dataset", "task"]
    required: bool = True
    default_expr: str | None = Field(None, max_length=200)
    enum_values: list[str] | None = None
    label: str = Field(min_length=1, max_length=120)
    help: str | None = Field(None, max_length=300)


class Draft(StrictBody):
    name: str | None = Field(None, min_length=1, max_length=120)
    description: str | None = None
    change_note: str | None = Field(None, max_length=300)
    steps: list[StepDraft] = Field(max_length=65535)
    params: list[ParamDraft] = Field(default_factory=list)


def _template(session, project, key):
    if not key or len(key) > 40 or not all(c.isalnum() or c in "_-" for c in key):
        raise Unprocessable(_("Invalid workflow key"))
    return service.template_get(session, projects.get(session, project), key)


@router.get("/workflows")
def list_workflows(project: str | None = None, session: Session = SessionDep, me: Actor = Depends(current_actor)):
    query = select(WorkflowTemplate).order_by(WorkflowTemplate.id)
    if project:
        query = query.where(WorkflowTemplate.project_id == projects.get(session, project).id)
    return [service.template_info(session, me, t) for t in session.scalars(query)
            if service.can_read(session, me, t.project)]


@router.post("/workflows/tick")
def tick(request: Request, session: Session = SessionDep, me: Actor = Depends(require_admin)):
    import ipaddress

    try:
        local = ipaddress.ip_address(request.client.host).is_loopback
    except (ValueError, AttributeError):
        local = False
    if not local:
        raise Forbidden(_("Workflow tick requires loopback and an admin token"))
    from app.services.workflows.tick import tick as process

    return process(session)


@router.get("/workflows/{project}/{key}")
def show_workflow(project: str, key: str, version: int | None = Query(None, gt=0),
                  session: Session = SessionDep, me: Actor = Depends(current_actor)):
    template = _template(session, project, key)
    service.require_read(session, me, template.project)
    selected = None
    if version is not None:
        selected = session.scalar(select(WorkflowTemplateVersion).where(
            WorkflowTemplateVersion.template_id == template.id, WorkflowTemplateVersion.version == version))
        if selected is None:
            raise NotFound(_("Workflow version not found"))
    return service.template_info(session, me, template, selected)


@router.get("/workflows/{project}/{key}/draft")
def get_draft(project: str, key: str, session: Session = SessionDep, me: Actor = Depends(current_actor)):
    template = _template(session, project, key)
    service.require_edit(me, template.project)
    draft = session.scalar(select(WorkflowTemplateVersion).where(
        WorkflowTemplateVersion.template_id == template.id, WorkflowTemplateVersion.state == "draft"))
    if draft is None:
        raise NotFound(_("Workflow draft not found"))
    return service.template_info(session, me, template, draft)


@router.put("/workflows/{project}/{key}/draft")
def put_draft(project: str, key: str, body: Draft, session: Session = SessionDep, me: Actor = Depends(current_actor)):
    if not key or len(key) > 40 or not all(c.isalnum() or c in "_-" for c in key):
        raise Unprocessable(_("Invalid workflow key"))
    if len({s.step_key for s in body.steps}) != len(body.steps) or len({p.name for p in body.params}) != len(body.params):
        raise Unprocessable(_("Workflow keys and parameter names must be unique"))
    template, draft = service.save_draft(session, me, projects.get(session, project), key, body)
    return service.template_info(session, me, template, draft)


@router.get("/workflows/{project}/{key}/export")
def export_version(project: str, key: str, version: int | None = Query(None, gt=0),
                   session: Session = SessionDep, me: Actor = Depends(current_actor)):
    """Canonical YAML of a version (default: current). Same read rights as the template page."""
    from fastapi.responses import Response

    from app.services.workflows import transfer

    template = _template(session, project, key)
    selected = None
    if version is not None:
        selected = session.scalar(select(WorkflowTemplateVersion).where(
            WorkflowTemplateVersion.template_id == template.id, WorkflowTemplateVersion.version == version))
        if selected is None:
            raise NotFound(_("Workflow version not found"))
    else:
        selected = template.current_version
    service.require_read(session, me, template.project)
    if selected is None:
        raise NotFound(_("Workflow version not found"))
    body = service.export_version(session, me, template, selected)
    name = transfer.export_filename(template, selected)
    return Response(body, media_type="application/yaml",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.put("/workflows/{project}/{key}/import")
async def import_draft(project: str, key: str, request: Request, session: Session = SessionDep,
                       me: Actor = Depends(current_actor)):
    """Store a canonical YAML document as an unpublished draft. Body = raw document (text/yaml).

    Refuses blocking check failures (never stores a broken draft) and never publishes; a human publishes later.
    """
    from app.services.workflows import transfer

    if not key or len(key) > 40 or not all(c.isalnum() or c in "_-" for c in key):
        raise Unprocessable(_("Invalid workflow key"))
    raw = await request.body()
    if not raw:
        raise Unprocessable(_("The file is empty"), code="workflow_import_invalid")
    document = transfer.parse_yaml(raw)
    template, draft, _warnings = service.import_draft(session, me, projects.get(session, project), key, document)
    return service.template_info(session, me, template, draft)


@router.post("/workflows/{project}/{key}/publish")
def publish(project: str, key: str, session: Session = SessionDep, me: Actor = Depends(current_actor)):
    template = _template(session, project, key)
    version, warnings = service.publish(session, me, template)
    return {**service.template_info(session, me, template, version), "warnings": warnings}


@router.post("/workflows/{project}/{key}/runs")
def start_run(project: str, key: str, body: RunStart, session: Session = SessionDep, me: Actor = Depends(current_actor)):
    template = _template(session, project, key)
    if body.dry_run:
        with session.no_autoflush:
            return {**service.preview(session, me, template, body.params),
                    "budget_tokens": body.budget_tokens if body.budget_tokens is not None else template.default_budget_tokens,
                    "budget_cents": body.budget_cents if body.budget_cents is not None else template.default_budget_cents}
    run = engine.start(session, me, template, params=body.params, trigger_kind="api",
                       budget_tokens=body.budget_tokens, budget_cents=body.budget_cents,
                       queue=body.queue, force_parallel=body.force_parallel)
    return service.run_info(session, me, run)


@router.get("/workflow-runs")
def list_runs(project: str | None = None, workflow: str | None = None, status: str | None = None,
              limit: int = Query(20, ge=1, le=200), session: Session = SessionDep, me: Actor = Depends(current_actor)):
    query = select(WorkflowRun).order_by(WorkflowRun.id.desc())
    if project:
        query = query.where(WorkflowRun.project_id == projects.get(session, project).id)
    if workflow:
        if "/" not in workflow:
            if not project:
                raise Unprocessable(_("Workflow must be PROJ/key"))
            workflow = f"{project}/{workflow}"
        p, key = workflow.split("/", 1)
        query = query.where(WorkflowRun.template_id == _template(session, p, key).id)
    if status:
        if status not in ("queued", "running", "waiting", "paused", "succeeded", "failed", "cancelled"):
            raise Unprocessable(_("Invalid workflow run status"))
        query = query.where(WorkflowRun.status == status)
    # Filter access in SQL before applying the limit.
    if me.role != "admin":
        from app.models import Project, ProjectMember

        allowed = select(ProjectMember.project_id).where(ProjectMember.actor_id == me.id)
        own = select(Project.id).where(Project.owner_id == me.id) if me.actor_type == "human" else allowed
        query = query.where(WorkflowRun.project_id.in_(allowed) | WorkflowRun.project_id.in_(own))
    return [service.run_info(session, me, run, detail=False) for run in session.scalars(query.limit(limit))]


@router.get("/workflow-runs/{run_id}")
def show_run(run_id: int, session: Session = SessionDep, me: Actor = Depends(current_actor)):
    return service.run_info(session, me, service.run_get(session, me, run_id))


@router.post("/workflows/{project}/{key}/scripts/{step_key}/recreate")
def recreate_script(project: str, key: str, step_key: str, session: Session = SessionDep,
                    me: Actor = Depends(current_actor)):
    from app.services.workflows.scripts import retire

    retire(session, _template(session, project, key), step_key, me)
    return {"retired": True}


@router.post("/workflow-runs/{run_id}/{action}")
def control_run(run_id: int, action: Literal["pause", "resume", "cancel", "repair"], body: RunControl = RunControl(),
                session: Session = SessionDep, me: Actor = Depends(current_actor)):
    # Control rights also cover a human starter/template owner who is no longer a project member.
    run = session.get(WorkflowRun, run_id)
    if run is None:
        raise NotFound(_("Workflow run not found"))
    if action == "repair":
        from app.services.workflows.scripts import repair

        run = repair(session, me, run)
    elif action == "resume":
        run = engine.resume(session, me, run, from_step=body.from_step, skip=body.skip_failed,
                            budget_tokens=body.budget_tokens, budget_cents=body.budget_cents)
    else:
        run = getattr(engine, action)(session, me, run)
    if body.reason:
        run.error_detail = body.reason
    return service.run_info(session, me, run)
