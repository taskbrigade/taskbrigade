"""Menu "Workflows" (research/konzept-workflows.md §8, guide docs/workflows-ui.md): template list, template page with
versions and diff, editor, run history, run list and run detail.

Rules live in the services: drafts and publication in `workflows.api`, runs and controls in `workflows.engine`,
view data in `workflows.ui`. The editor posts the whole draft each time; the checks panel re-runs the WF 3 publish
checks on the unsaved form after every change (HTMX).
"""
import json
from urllib.parse import quote, quote_plus

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.auth import UiUser, csrf_protect
from app.errors import AppError, Forbidden, NotFound, Unprocessable
from app.i18n import gettext as _
from app.logging_setup import audit
from app.models import DATA_CLASSES, MODEL_TIERS, Actor, WorkflowRun, WorkflowTemplate
from app.services import projects
from app.services.workflows import actions, api, engine
from app.services.workflows import starters, transfer
from app.services.workflows import ui as wf
from app.web.common import render, ui_actor, web_session
from app.web.tasks_ui import require_user

router = APIRouter()
POST = [Depends(csrf_protect)]
RUN_STATUSES = tuple(wf.RUN_STATUS_LABELS)


def _valid_key(key: str) -> str:
    if not key or len(key) > 40 or not all(c.isalnum() or c in "_-" for c in key):
        raise Unprocessable(_("Workflow key: letters, digits, “_” and “-”, at most 40 characters"),
                            code="invalid_workflow_key")
    return key


def _template(s, me, project: str, key: str):
    template = api.template_get(s, projects.get(s, project), _valid_key(key))
    api.require_read(s, me, template.project)
    return template


def _base(project: str, key: str) -> str:
    return f"/workflows/{quote(project)}/{quote(key)}"


# --- Template list ------------------------------------------------------------------------------------------------

@router.get("/workflows")
def listing(request: Request, user: UiUser = Depends(require_user), project: str = "", q: str = "", started: int | None = None):
    q = q.strip()[:80]
    with web_session() as s:
        me = ui_actor(s, user)
        templates = wf.visible_templates(s, me, project or None)
        if q:
            needle = q.casefold()
            templates = [t for t in templates if needle in t.name.casefold() or needle in t.workflow_key.casefold()]
        rows = wf.template_rows(s, templates)
        for row in rows:
            t = row["template"]
            row.update(can_edit=wf.may_edit(me, t.project), can_start=wf.may_start(s, me, t),
                       impact=actions.impact(s, t),
                       start_params=api.template_info(s, me, t)["params"] if t.current_version else [])
            row["start_dialog"] = (any(not p["default"] for p in row["start_params"])
                                   or t.default_budget_tokens is not None or t.default_budget_cents is not None)
        all_projects = sorted({t.project.project_key for t in wf.visible_templates(s, me)} |
                              {p.project_key for p in wf.edit_projects(s, me)})
        can_create = bool(wf.edit_projects(s, me))
        return render(request, "workflows.html", rows=rows, project=project.upper(), projects=all_projects,
                      can_create=can_create, q=q, wf_counts=wf.tab_counts(s, me), starters=starters.STARTERS,
                      filtered=bool(q or project), started_run=started)


@router.post("/workflows/{project}/{key}/manage/{action}", dependencies=POST)
def manage(request: Request, project: str, key: str, action: str, user: UiUser = Depends(require_user),
           name: str = Form("")):
    with web_session() as s:
        me = ui_actor(s, user)
        template = _template(s, me, project, key)
        if action == "rename":
            actions.rename(s, me, template, name)
            message = _("Workflow renamed")
        elif action == "duplicate":
            copied = actions.duplicate(s, me, template)
            path = _base(project, copied.workflow_key) + "/edit"
            audit(me.name, "workflow_duplicated", workflow=f"{project}/{key}", copy=copied.workflow_key, via="ui")
            return RedirectResponse(path, 303)
        elif action == "delete":
            actions.archive(s, me, template)
            message = _("Workflow deleted")
        else:
            raise NotFound(_("Unknown action"))
        audit(me.name, "workflow_" + action, workflow=f"{project}/{key}", via="ui")
    return RedirectResponse("/workflows?msg=" + quote_plus(message), 303)


@router.get("/workflows/new")
def new_form(request: Request, user: UiUser = Depends(require_user), project: str = "", starter: str = ""):
    with web_session() as s:
        choices = [p.project_key for p in wf.edit_projects(s, ui_actor(s, user))]
    if not choices:
        raise Forbidden(_("Only admins, orchestrators and the project owner may edit workflows"))
    chosen = starters.get(starter)
    form = {"project": project.upper(), "key": "", "name": "", "starter": ""}
    if chosen:
        form.update(key=chosen["key"], name=_(chosen["name"]), starter=starter)
    return render(request, "workflow_new.html", choices=choices, form=form, starter=chosen)


@router.post("/workflows/new", dependencies=POST)
def create(request: Request, user: UiUser = Depends(require_user), project: str = Form(""), key: str = Form(""),
           name: str = Form(""), starter: str = Form("")):
    from app.api.workflows import Draft

    chosen = starters.get(starter)
    form = {"project": project, "key": key.strip(), "name": name.strip(), "starter": starter if chosen else ""}
    try:
        with web_session() as s:
            me = ui_actor(s, user)
            target = projects.get(s, project)
            _valid_key(form["key"])
            exists = s.scalar(select(WorkflowTemplate.id).where(
                WorkflowTemplate.project_id == target.id, WorkflowTemplate.workflow_key == form["key"]))
            if exists:
                raise Unprocessable(_("A workflow with this key already exists in %(project)s",
                                      project=target.project_key), code="workflow_exists")
            body = starters.draft_body(chosen, form["name"]) if chosen else {"name": form["name"] or None, "steps": []}
            template, _draft = api.save_draft(s, me, target, form["key"], Draft(**body))
            path = _base(target.project_key, template.workflow_key)
            audit(me.name, "workflow_created", workflow=f"{target.project_key}/{template.workflow_key}", via="ui")
    except AppError as e:
        with web_session() as s:
            choices = [p.project_key for p in wf.edit_projects(s, ui_actor(s, user))]
        return render(request, "workflow_new.html", 422, choices=choices, form=form, starter=chosen,
                      error=e.message)
    if chosen:
        hint = _(chosen["next_step"]) if chosen["next_step"] else _("Draft created from a start template")
        return RedirectResponse(f"{path}/edit?msg={quote_plus(hint)}", 303)
    return RedirectResponse(path + "/edit", 303)


# --- Template page, versions and diff -----------------------------------------------------------------------------

@router.get("/workflows/{project}/{key}")
def detail(request: Request, project: str, key: str, user: UiUser = Depends(require_user), version: str = ""):
    with web_session() as s:
        me = ui_actor(s, user)
        template = _template(s, me, project, key)
        selected = wf.find_version(s, template, version or None)
        if version and selected is None:
            raise NotFound(_("Workflow version not found"))
        if selected is not None and selected.state == "draft" and not wf.may_edit(me, template.project):
            raise Forbidden(_("Only admins, orchestrators and the project owner may edit workflows"))
        info = api.template_info(s, me, template, selected) if selected else None
        # The start form always offers the current version's parameters, whichever version is shown.
        params = api.template_info(s, me, template)["params"] if template.current_version else []
        return render(request, "workflow_detail.html", template=template, info=info, selected=selected,
                      versions=wf.versions(s, template), draft=wf.draft_of(s, template),
                      can_edit=wf.may_edit(me, template.project), can_start=wf.may_start(s, me, template),
                      start_params=params, recent=wf.last_runs(s, [template.id], 10).get(template.id, []),
                      base=_base(template.project.project_key, template.workflow_key))


@router.get("/workflows/{project}/{key}/diff")
def diff(request: Request, project: str, key: str, user: UiUser = Depends(require_user), base: str = "",
         target: str = ""):
    """`base`/`target`: a version number or `draft`; default the published version against the draft (or the
    previous version against the current one)."""
    with web_session() as s:
        me = ui_actor(s, user)
        template = _template(s, me, project, key)
        draft = wf.draft_of(s, template)
        new = wf.find_version(s, template, target or ("draft" if draft else None))
        if new is None:
            raise NotFound(_("Workflow version not found"))
        if base:
            old = wf.find_version(s, template, base)
        else:
            old = template.current_version if new.state == "draft" else next(
                (v for v in sorted(template.versions, key=lambda v: -v.version)
                 if v.version < new.version and v.state != "draft"), None)
        if (new.state == "draft" or (old is not None and old.state == "draft")) and not wf.may_edit(me, template.project):
            raise Forbidden(_("Only admins, orchestrators and the project owner may edit workflows"))
        rows = wf.diff(api.template_info(s, me, template, old) if old else None, api.template_info(s, me, template, new))
        choices = [("draft" if v.state == "draft" else str(v.version)) for v in
                   sorted(template.versions, key=lambda v: -v.version)
                   if v.state != "draft" or wf.may_edit(me, template.project)]
        return render(request, "workflow_diff.html", template=template, rows=rows, old=old, new=new,
                      choices=choices, base=_base(template.project.project_key, template.workflow_key))


# --- Export / import (WF 14) ------------------------------------------------------------------------------------

@router.get("/workflows/{project}/{key}/export")
def export_yaml(request: Request, project: str, key: str, user: UiUser = Depends(require_user), version: str = ""):
    """Canonical YAML of a version (default: current) as a download; same read rights as the template page."""
    from fastapi.responses import Response

    with web_session() as s:
        me = ui_actor(s, user)
        template = _template(s, me, project, key)
        selected = wf.find_version(s, template, version or None) if version else template.current_version
        if selected is None:
            raise NotFound(_("Workflow version not found"))
        body = api.export_version(s, me, template, selected)
        name = transfer.export_filename(template, selected)
    return Response(body, media_type="application/yaml",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post("/workflows/{project}/{key}/import", dependencies=POST)
async def import_yaml(request: Request, project: str, key: str, user: UiUser = Depends(require_user)):
    """Store an uploaded YAML document as an unpublished draft; blocking checks are refused (no auto-publish)."""
    form = await request.form()
    upload = form.get("file")
    action = str(form.get("action") or "save")
    try:
        if upload is None or not getattr(upload, "filename", ""):
            raise Unprocessable(_("Choose a YAML file"), code="workflow_import_invalid")
        raw = await upload.read()
        document = transfer.parse_yaml(raw)
        with web_session() as s:
            me = ui_actor(s, user)
            template = _template(s, me, project, key)
            if not wf.may_edit(me, template.project):
                raise Forbidden(_("Only admins, orchestrators and the project owner may edit workflows"))
            _template_row, draft, _warnings = api.import_draft(s, me, template.project, template.workflow_key,
                                                               document)
            number = draft.version
            base = _base(template.project.project_key, template.workflow_key)
            audit(me.name, "workflow_imported", workflow=f"{template.project.project_key}/{template.workflow_key}",
                  version=number, via="ui")
    except AppError as e:
        with web_session() as s:
            me = ui_actor(s, user)
            template = _template(s, me, project, key)
            return _editor(request, s, me, template, status_code=422, error=e.message,
                           import_error=True)
    if action == "publish":
        try:
            with web_session() as s:
                me = ui_actor(s, user)
                template = _template(s, me, project, key)
                version_row, _warnings = api.publish(s, me, template)
                number = version_row.version
                audit(me.name, "workflow_published",
                      workflow=f"{template.project.project_key}/{template.workflow_key}", version=number, via="ui")
        except AppError as e:
            with web_session() as s:
                me = ui_actor(s, user)
                template = _template(s, me, project, key)
                msg = _("Imported, but not published: %(error)s", error=e.message)
                return _editor(request, s, me, template, status_code=422, error=msg)
        return RedirectResponse(f"{base}?msg={quote_plus(_('Version %(version)s published', version=number))}", 303)
    return RedirectResponse(f"{base}/edit?msg={quote_plus(_('Draft imported from YAML'))}", 303)


# --- Editor ---------------------------------------------------------------------------------------------------------

def _editor(request: Request, s, me, template, values: dict | None = None, status_code: int = 200, **ctx):
    from app.services import datasets

    if not wf.may_edit(me, template.project):
        raise Forbidden(_("Only admins, orchestrators and the project owner may edit workflows"))
    draft = wf.draft_of(s, template)
    source = draft or template.current_version
    if source is not None and not all(api.visible_class(s, me, st.data_class or template.project.privacy)
                                      for st in source.steps):
        raise Forbidden(_("This workflow has steps of a data class you may not read"))
    if values is None:
        values = wf.editor_form(template, source)
    actors = wf.actor_choices(s, template.project)
    return render(request, "workflow_editor.html", status_code, template=template, values=values, draft=draft,
                  issues=wf.checks(s, template, values), actors=actors, data_classes=DATA_CLASSES,
                  model_tiers=MODEL_TIERS,
                  step_types=tuple(t for t in wf.STEP_TYPES if t != "script" or datasets.enabled(s)),
                  param_types=wf.PARAM_TYPES,
                  placeholder_data=json.dumps(wf.placeholder_basics()),
                  base=_base(template.project.project_key, template.workflow_key), **ctx)


@router.get("/workflows/{project}/{key}/edit")
def edit(request: Request, project: str, key: str, user: UiUser = Depends(require_user)):
    with web_session() as s:
        me = ui_actor(s, user)
        return _editor(request, s, me, _template(s, me, project, key))


@router.post("/workflows/{project}/{key}/edit", dependencies=POST)
async def save(request: Request, project: str, key: str, user: UiUser = Depends(require_user)):
    form = await request.form()
    values = wf.parse_form(form)
    action = str(form.get("action") or "save")
    try:
        with web_session() as s:
            me = ui_actor(s, user)
            template = _template(s, me, project, key)
            api.save_draft(s, me, template.project, template.workflow_key, wf.to_draft(values))
            base = _base(template.project.project_key, template.workflow_key)
    except AppError as e:
        with web_session() as s:
            me = ui_actor(s, user)
            return _editor(request, s, me, _template(s, me, project, key), values, 422, error=e.message)
    if action != "publish":
        return RedirectResponse(f"{base}/edit?msg={quote_plus(_('Draft saved'))}", 303)
    try:
        with web_session() as s:
            me = ui_actor(s, user)
            template = _template(s, me, project, key)
            version, _warnings = api.publish(s, me, template)
            number = version.version
            audit(me.name, "workflow_published", workflow=f"{template.project.project_key}/{template.workflow_key}",
                  version=number, via="ui")
    except AppError as e:
        with web_session() as s:
            me = ui_actor(s, user)
            msg = _("Draft saved, but not published: %(error)s", error=e.message)
            return _editor(request, s, me, _template(s, me, project, key), status_code=422, error=msg)
    return RedirectResponse(f"{base}?msg={quote_plus(_('Version %(version)s published', version=number))}", 303)


@router.post("/workflows/{project}/{key}/check", dependencies=POST)
async def check(request: Request, project: str, key: str, user: UiUser = Depends(require_user)):
    """Checks panel (HTMX): publish checks on the unsaved editor form."""
    form = await request.form()
    with web_session() as s:
        me = ui_actor(s, user)
        template = _template(s, me, project, key)
        if not wf.may_edit(me, template.project):
            raise Forbidden(_("Only admins, orchestrators and the project owner may edit workflows"))
        issues = wf.checks(s, template, wf.parse_form(form))
    return render(request, "_workflow_checks.html", issues=issues)


@router.post("/workflows/{project}/{key}/discard", dependencies=POST)
def discard(request: Request, project: str, key: str, user: UiUser = Depends(require_user)):
    with web_session() as s:
        me = ui_actor(s, user)
        template = _template(s, me, project, key)
        api.discard_draft(s, me, template)
        base = _base(template.project.project_key, template.workflow_key)
    return RedirectResponse(f"{base}?msg={quote_plus(_('Draft discarded'))}", 303)


@router.post("/workflows/{project}/{key}/scripts/{step_key}/recreate", dependencies=POST)
def recreate_script(request: Request, project: str, key: str, step_key: str,
                    user: UiUser = Depends(require_user)):
    from app.services.workflows.scripts import retire

    with web_session() as s:
        me = ui_actor(s, user)
        template = _template(s, me, project, key)
        retire(s, template, step_key, me)
        base = _base(template.project.project_key, template.workflow_key)
    return RedirectResponse(f"{base}?msg={quote_plus(_('Script pin retired'))}", 303)


# --- Runs -----------------------------------------------------------------------------------------------------------

def _params_from_form(form, template) -> dict:
    """Start form: `param_<name>` fields; empty values are left out so defaults apply."""
    values = {}
    for p in template.current_version.params:
        raw = str(form.get(f"param_{p.name}") or "").strip()
        if raw:
            values[p.name] = raw
    return values


@router.post("/workflows/{project}/{key}/start", dependencies=POST)
async def start(request: Request, project: str, key: str, user: UiUser = Depends(require_user)):
    form = await request.form()
    try:
        with web_session() as s:
            me = ui_actor(s, user)
            template = _template(s, me, project, key)
            params = _params_from_form(form, template) if template.current_version else {}
            budgets = {}
            for field in ("budget_tokens", "budget_cents"):
                value = _int_form(str(form.get(field) or ""))
                if value is not None and not 0 <= value <= 4294967295:
                    raise Unprocessable(_("Budget must be between 0 and 4294967295"))
                budgets[field] = value
            run = engine.start(s, me, template, params=params, trigger_kind="manual", **budgets)
            run_id = run.id
            audit(me.name, "workflow_started", run=run_id, via="ui")
    except AppError as e:
        return RedirectResponse(f"/workflows/{quote(project)}/{quote(key)}?err={quote_plus(e.message)}", 303)
    if form.get("overview") == "1":
        return RedirectResponse(f"/workflows?started={run_id}&msg={quote_plus(_('Run started'))}", 303)
    return RedirectResponse(f"/workflow-runs/{run_id}?msg={quote_plus(_('Run started'))}", 303)


@router.get("/workflows/{project}/{key}/runs")
def runs(request: Request, project: str, key: str, user: UiUser = Depends(require_user), status: str = "",
         trigger: str = ""):
    status = status if status in RUN_STATUSES else ""
    trigger = trigger if trigger in wf.TRIGGER_LABELS else ""
    with web_session() as s:
        me = ui_actor(s, user)
        template = _template(s, me, project, key)
        data = wf.history(s, template, status=status or None, trigger=trigger or None)
        return render(request, "workflow_history.html", template=template, **data, status=status, trigger=trigger,
                      statuses=RUN_STATUSES, base=_base(template.project.project_key, template.workflow_key),
                      params={r.id: _visible_params(s, me, r) for r in data["runs"]})


def _visible_params(s, me, run) -> dict | None:
    """Run parameters, or None when the caller may not read the run's data class (masked like the API)."""
    info = api.run_info(s, me, run, detail=False)
    return info["params"] if isinstance(info["params"], dict) else None


@router.get("/workflow-runs")
def run_list(request: Request, user: UiUser = Depends(require_user), project: str = "", status: str = ""):
    status = status if status in RUN_STATUSES else ""
    with web_session() as s:
        me = ui_actor(s, user)
        rows = wf.run_rows(s, me, project=project or None, status=status or None)
        projects_ = sorted({t.project.project_key for t in wf.visible_templates(s, me, include_archived=True)})
        return render(request, "workflow_runs.html", runs=rows, progress=wf.progress(s, rows), project=project.upper(),
                      status=status, statuses=RUN_STATUSES, projects=projects_,
                      wf_counts=wf.tab_counts(s, me))


@router.get("/workflow-runs/{run_id}")
def run_detail(request: Request, run_id: int, user: UiUser = Depends(require_user)):
    with web_session() as s:
        me = ui_actor(s, user)
        run = api.run_get(s, me, run_id)
        info = api.run_info(s, me, run)
        names = dict(s.execute(select(Actor.id, Actor.name).where(
            Actor.id.in_([x for x in (run.started_by, run.template.owner_id) if x]))).all())
        attempts = [{**step, "row": row} for step, row in zip(info["steps"], sorted(
            run.steps, key=lambda r: (r.position, r.attempt)), strict=False)]
        return render(request, "workflow_run.html", run=run, info=info, chain=wf.chain(s, run), attempts=attempts,
                      can_control=wf.may_control(me, run), names=names,
                      steps=[st.step_key for st in sorted(run.version.steps, key=lambda x: x.position)],
                      base=_base(run.project.project_key, run.template.workflow_key))


@router.post("/workflow-runs/{run_id}/{action}", dependencies=POST)
def control(request: Request, run_id: int, action: str, user: UiUser = Depends(require_user),
            reason: str = Form(""), from_step: str = Form(""), skip: bool = Form(False),
            budget_tokens: str = Form(""), budget_cents: str = Form("")):
    labels = {"pause": _("Run paused"), "resume": _("Run resumed"), "cancel": _("Run cancelled"),
              "repair": _("Script repair requested")}
    if action not in labels:
        raise AppError(_("Unknown action"))
    try:
        with web_session() as s:
            me = ui_actor(s, user)
            run = s.get(WorkflowRun, run_id)
            if run is None:
                raise NotFound(_("Workflow run not found"))
            if action == "repair":
                from app.services.workflows.scripts import repair

                run = repair(s, me, run)
            elif action == "resume":
                run = engine.resume(s, me, run, from_step=from_step or None, skip=skip,
                                    budget_tokens=_int_form(budget_tokens), budget_cents=_int_form(budget_cents))
            else:
                run = getattr(engine, action)(s, me, run)
            if reason.strip():
                run.error_detail = reason.strip()[:500]
            audit(me.name, f"workflow_run_{action}", run=run_id, via="ui")
    except AppError as e:
        return RedirectResponse(f"/workflow-runs/{run_id}?err={quote_plus(e.message)}", 303)
    return RedirectResponse(f"/workflow-runs/{run_id}?msg={quote_plus(labels[action])}", 303)


def _int_form(value: str) -> int | None:
    try:
        return int(value) if value.strip() else None
    except ValueError:
        raise AppError(_("Budget must be a number"))
